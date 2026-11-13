"""Bounded CI compilation and verified full-tree checkpoints (Python 3.12)."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import time
from contextlib import suppress
from pathlib import Path

KIT = Path(__file__).resolve().parent
PARTIAL = 75


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(root: Path) -> dict:
    return {
        "format": 1,
        "workspace": str(root.resolve()),
        "host": [platform.system(), platform.machine()],
        "python": platform.python_version(),
        "ninja": subprocess.check_output(["ninja", "--version"], text=True).strip(),
        "xcode": subprocess.check_output(["xcodebuild", "-version"], text=True).strip(),
        "sdk": subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True
        ).strip(),
        "source": json.loads((root / "source-provenance.json").read_text()),
        "kit": {
            name: digest(KIT / name)
            for name in ("build.sh", "prepare.py", "verify.py", "ci-build.gn", "checkpoint.py")
        },
    }


def validate(root: Path) -> None:
    if json.loads((root / "native-ready.json").read_text()) != identity(root):
        raise ValueError("Checkpoint build identity changed; refusing stale objects")
    expected = b"".join(
        path.read_bytes()
        for path in (
            root / "ungoogled-chromium/flags.gn",
            root / "flags.macos.gn",
            KIT / "ci-build.gn",
        )
    )
    if (root / "build/src/out/Default/args.gn").read_bytes() != expected:
        raise ValueError("Checkpoint GN flags changed")
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    if memory < 7 * 1024**3 or shutil.disk_usage(root).free < 8 * 1024**3:
        raise ValueError("Resumed build requires >=7 GiB RAM and >=8 GiB free disk")


def pack(source: Path, destination: Path, context: dict) -> None:
    source = source.resolve()
    if destination.resolve().is_relative_to(source):
        raise ValueError("Checkpoint destination must be outside the source tree")
    destination.mkdir(parents=True, exist_ok=False)

    def safe_link(info):
        # Upstream toolchain setup creates absolute links back into build/src.
        if info.issym():
            parent = (source / Path(info.name).relative_to("src")).parent
            target = (parent / info.linkname).resolve()
            if not target.is_relative_to(source):
                raise ValueError(f"External checkpoint symlink: {info.name}")
            info.linkname = os.path.relpath(target, parent)
        return info

    archive = destination / "source.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=1) as bundle:
        bundle.add(source, arcname="src", filter=safe_link)
    manifest = {"identity": context, "sha256": digest(archive), "bytes": archive.stat().st_size}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Checkpoint complete: {manifest['bytes']} compressed bytes", flush=True)


def unpack(build: Path, checkpoint: Path, context: dict) -> None:
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    archive = checkpoint / "source.tar.gz"
    if manifest["identity"] != context or manifest["sha256"] != digest(archive):
        raise ValueError("Checkpoint identity or archive checksum mismatch")
    if build.exists():
        raise ValueError("Restore requires a fresh build directory")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if any(Path(m.name).parts[:1] != ("src",) for m in members):
            raise ValueError("Unexpected checkpoint member outside src")
        required = sum(m.size for m in members if m.isfile()) + 8 * 1024**3
        if shutil.disk_usage(build.parent).free < required:
            raise ValueError("Not enough disk to restore checkpoint plus 8 GiB reserve")
        build.mkdir()
        try:
            bundle.extractall(build, members=members, filter="data")
        except BaseException:
            shutil.rmtree(build)
            raise


def process_table() -> list[tuple[int, int, int]]:
    rows = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="], text=True)
    # Zombies cannot write; excluding them also handles orphan reaping delays.
    return [
        tuple(map(int, parts[:3]))
        for row in rows.splitlines()
        if len(parts := row.split()) == 4 and not parts[3].startswith("Z")
    ]


def session_groups(session: int, rows: list[tuple[int, int, int]]) -> set[int]:
    groups = set()
    for pid, _, group in rows:
        try:
            if os.getsid(pid) == session:
                groups.add(group)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Other users' sessions are not part of this build.
            continue
    return groups


def signal_groups(groups: set[int], sig: int) -> None:
    for group in groups:
        if group <= 1 or group == os.getpgrp():
            raise RuntimeError("Refusing to signal a non-build process group")
        with suppress(ProcessLookupError):
            os.killpg(group, sig)


def run_bounded(command: list[str], seconds: float, grace: float = 120) -> int:
    if seconds <= 0:
        raise ValueError("Job preparation exceeded the four-hour compile window")
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        if process.poll() is not None:
            return process.returncode
        rows = process_table()
        descendants = {process.pid}
        while True:
            expanded = descendants | {pid for pid, parent, _ in rows if parent in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        owned = session_groups(process.pid, rows)
        extra = {group for pid, _, group in rows if pid in descendants} - owned
        # Ninja creates a separate pgrp per command, within this owned session.
        signal_groups(owned | extra, signal.SIGINT)
        deadline = time.monotonic() + grace
        while True:
            process.poll()
            rows = process_table()
            active = session_groups(process.pid, rows) | {
                group for _, _, group in rows if group in extra
            }
            if not active:
                code = process.wait()
                if extra:
                    raise RuntimeError(
                        "Compiler escaped its session; refusing checkpoint"
                    ) from None
                # Ninja reports interruption as130; preserve genuine errors.
                return PARTIAL if code in (-signal.SIGINT, 128 + signal.SIGINT, 2) else code
            if time.monotonic() >= deadline:
                signal_groups(active, signal.SIGKILL)
                process.wait()
                raise RuntimeError(
                    "Compiler would not stop gracefully; no checkpoint is safe"
                ) from None
            time.sleep(0.1)


def probe(root: Path, restoring: bool) -> None:
    context = {"probe": os.environ["GITHUB_SHA"]}
    if not restoring:
        source = root / "src"
        source.mkdir(parents=True)
        marker = source / ".ninja_log"
        marker.write_text("checkpoint-probe\n")
        marker.chmod(0o755)
        os.utime(marker, (1700000000, 1700000000))
        (source / "tool-link").symlink_to(marker.resolve())
        pack(source, root / "checkpoint", context)
    else:
        unpack(root / "restored", root / "checkpoint", context)
        marker = root / "restored/src/.ninja_log"
        if (
            marker.read_text() != "checkpoint-probe\n"
            or marker.stat().st_mtime != 1700000000
            or marker.stat().st_mode & 0o777 != 0o755
            or not (marker.parent / "tool-link").is_symlink()
            or (marker.parent / "tool-link").read_text() != marker.read_text()
        ):
            raise ValueError("Checkpoint probe did not preserve bytes/mode/time/symlink")
        print("CHECKPOINT_PROBE_PASS", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=["mark", "validate", "save", "restore", "run", "probe-save", "probe-restore"],
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("checkpoint", type=Path, nargs="?")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode.startswith("probe-"):
        probe(root, args.mode == "probe-restore")
    elif args.mode == "mark":
        (root / "native-ready.json").write_text(json.dumps(identity(root), indent=2) + "\n")
        validate(root)
    elif args.mode == "restore":
        context = identity(root)
        unpack(root / "build", args.checkpoint, context)
        (root / "native-ready.json").write_text(json.dumps(context, indent=2) + "\n")
        validate(root)
    else:
        validate(root)
        if args.mode == "save":
            pack(root / "build/src", args.checkpoint, identity(root))
        elif args.mode == "run":
            deadline = int(os.environ["NATIVE_CHUNK_STARTED_AT"]) + 4 * 3600
            return run_bounded(
                [
                    "/usr/bin/time",
                    "-l",
                    "ninja",
                    "-j",
                    "2",
                    "-C",
                    str(root / "build/src/out/Default"),
                    "chrome",
                    "chromedriver",
                ],
                deadline - time.time(),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
