"""Prepare pinned upstream build inputs; never install/activate a browser core."""

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PLATFORM_REV = "6a00dfe08591a15cfb0aed0edd292ba789d9a0d7"
FINGERPRINT_REV = "13b89eae304123f0710f2d33fd0816a2f61d7ffc"
PATCH_SHA256 = "df190001a4ab19a97acad9758a0c0dd03a800584e9b96c81c33484a5bedd6ce2"
# Official static.rust-lang.org dated archive's .sha256, checked 2026-11-12.
RUST_SHA256 = "a7cfb30246204096d1d77481e0aa274b98762c529f85bd5a2064fcb68b777d97"
PATCH = Path(__file__).with_name("patches") / "109-canvas-pixel-addressing.patch"
NATIVE_PATCH = Path(__file__).with_name("patches") / "022-native-gpu-realms.patch"
NATIVE_PATCH_SHA256 = "e197fd91382ccf39dd1f43b36b9b89042331f09021344ee7d661133e1bc3420c"
NATIVE_SERIES_ENTRY = "extra/fingerprint/022-native-gpu-realms.patch"
MACOS_ANTIDETECT_PATCH = Path(__file__).with_name("patches") / "023-macos-antidetect-complete.patch"
MACOS_ANTIDETECT_PATCH_SHA256 = "98e2131461e38c290d67f929e0729cc187c741b31838c3515da1194ddbd57e4d"
MACOS_ANTIDETECT_SERIES_ENTRY = "extra/fingerprint/023-macos-antidetect-complete.patch"
READBACK_PARITY_PATCH = Path(__file__).with_name("patches") / "024-webgl-readback-parity.patch"
READBACK_PARITY_PATCH_SHA256 = "78ff843b4586c5452b8987dc9ecea0b52b16e1ccbcdc5e472597dd77d36d21c3"
READBACK_PARITY_SERIES_ENTRY = "extra/fingerprint/024-webgl-readback-parity.patch"
AUDIO_CLIENTRECTS_PATCH = Path(__file__).with_name("patches") / "025-audio-clientrects-parity.patch"
AUDIO_CLIENTRECTS_PATCH_SHA256 = "9a369414029f7b3b7811d01a31d8cbfb915b381ad08876e4b39e87cf7128f881"
AUDIO_CLIENTRECTS_SERIES_ENTRY = "extra/fingerprint/025-audio-clientrects-parity.patch"
APPLE_SILICON_PATCH = (
    Path(__file__).with_name("patches") / "026-apple-silicon-device-coherence.patch"
)
APPLE_SILICON_PATCH_SHA256 = "558b84bbec096583a5253889a06b4c31f5c5efa31a79fde5d42e29fce908d75f"
APPLE_SILICON_SERIES_ENTRY = "extra/fingerprint/026-apple-silicon-device-coherence.patch"
LOCALE_SPEECH_FONT_PATCH = (
    Path(__file__).with_name("patches") / "027-macos-locale-speech-font-coherence.patch"
)
LOCALE_SPEECH_FONT_PATCH_SHA256 = "ec47709cbe9a96390cb78f76c329ec6d9e3636bf31f5a44812d1e0c69c2aa726"
LOCALE_SPEECH_FONT_SERIES_ENTRY = "extra/fingerprint/027-macos-locale-speech-font-coherence.patch"
PIXELSCAN_COHERENCE_PATCH = (
    Path(__file__).with_name("patches") / "028-pixelscan-noise-font-coherence.patch"
)
PIXELSCAN_COHERENCE_PATCH_SHA256 = (
    "20a3147b46a11a8e27b8c912ba64bf2457ed5aa5b9c571c403a5b4a01a84de05"
)
PIXELSCAN_COHERENCE_SERIES_ENTRY = "extra/fingerprint/028-pixelscan-noise-font-coherence.patch"


def verify_checkout(path: Path, revision: str) -> None:
    actual = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != revision:
        raise ValueError(f"Unexpected source revision in {path}: {actual}")
    for cached in ([], ["--cached"]):
        subprocess.run(
            ["git", "-C", str(path), "diff", *cached, "--quiet", "--ignore-submodules=all"],
            check=True,
        )


def prepare(root: Path) -> None:
    fork = root / "ungoogled-chromium"
    if (root / "build").exists() or (root / "source-provenance.json").exists():
        raise ValueError("Use a fresh dedicated checkout; refusing to reuse a prepared/build tree")
    verify_checkout(root, PLATFORM_REV)
    verify_checkout(fork, FINGERPRINT_REV)
    if hashlib.sha256(PATCH.read_bytes()).hexdigest() != PATCH_SHA256:
        raise ValueError("Canvas overlay checksum mismatch")
    if hashlib.sha256(NATIVE_PATCH.read_bytes()).hexdigest() != NATIVE_PATCH_SHA256:
        raise ValueError("Native realm patch checksum mismatch")
    if (
        hashlib.sha256(MACOS_ANTIDETECT_PATCH.read_bytes()).hexdigest()
        != MACOS_ANTIDETECT_PATCH_SHA256
    ):
        raise ValueError("macOS antidetect patch checksum mismatch")
    if (
        hashlib.sha256(READBACK_PARITY_PATCH.read_bytes()).hexdigest()
        != READBACK_PARITY_PATCH_SHA256
    ):
        raise ValueError("WebGL readback parity patch checksum mismatch")
    if (
        hashlib.sha256(AUDIO_CLIENTRECTS_PATCH.read_bytes()).hexdigest()
        != AUDIO_CLIENTRECTS_PATCH_SHA256
    ):
        raise ValueError("Audio/ClientRects parity patch checksum mismatch")
    if hashlib.sha256(APPLE_SILICON_PATCH.read_bytes()).hexdigest() != APPLE_SILICON_PATCH_SHA256:
        raise ValueError("Apple Silicon coherence patch checksum mismatch")
    if (
        hashlib.sha256(LOCALE_SPEECH_FONT_PATCH.read_bytes()).hexdigest()
        != LOCALE_SPEECH_FONT_PATCH_SHA256
    ):
        raise ValueError("Locale/speech/font coherence patch checksum mismatch")
    if (
        hashlib.sha256(PIXELSCAN_COHERENCE_PATCH.read_bytes()).hexdigest()
        != PIXELSCAN_COHERENCE_PATCH_SHA256
    ):
        raise ValueError("Pixelscan coherence patch checksum mismatch")
    # Check every input before modifying either checkout.
    rust_ini = root / "downloads-arm64.ini"
    before = rust_ini.read_text()
    anchor = "[rust]\nversion = 2026-02-27\n"
    if before.count(anchor) != 1:
        raise ValueError("Unexpected Rust toolchain configuration")
    series_path = fork / "patches/series"
    series = series_path.read_text()
    native_target = fork / "patches" / NATIVE_SERIES_ENTRY
    macos_target = fork / "patches" / MACOS_ANTIDETECT_SERIES_ENTRY
    readback_target = fork / "patches" / READBACK_PARITY_SERIES_ENTRY
    audio_rects_target = fork / "patches" / AUDIO_CLIENTRECTS_SERIES_ENTRY
    apple_silicon_target = fork / "patches" / APPLE_SILICON_SERIES_ENTRY
    locale_speech_font_target = fork / "patches" / LOCALE_SPEECH_FONT_SERIES_ENTRY
    pixelscan_coherence_target = fork / "patches" / PIXELSCAN_COHERENCE_SERIES_ENTRY
    if NATIVE_SERIES_ENTRY in series or native_target.exists():
        raise ValueError("Native patch already staged; use a fresh checkout")
    if MACOS_ANTIDETECT_SERIES_ENTRY in series or macos_target.exists():
        raise ValueError("macOS antidetect patch already staged; use a fresh checkout")
    if READBACK_PARITY_SERIES_ENTRY in series or readback_target.exists():
        raise ValueError("WebGL readback parity patch already staged; use a fresh checkout")
    if AUDIO_CLIENTRECTS_SERIES_ENTRY in series or audio_rects_target.exists():
        raise ValueError("Audio/ClientRects parity patch already staged; use a fresh checkout")
    if APPLE_SILICON_SERIES_ENTRY in series or apple_silicon_target.exists():
        raise ValueError("Apple Silicon coherence patch already staged; use a fresh checkout")
    if LOCALE_SPEECH_FONT_SERIES_ENTRY in series or locale_speech_font_target.exists():
        raise ValueError("Locale/speech/font coherence patch already staged; use a fresh checkout")
    if PIXELSCAN_COHERENCE_SERIES_ENTRY in series or pixelscan_coherence_target.exists():
        raise ValueError("Pixelscan coherence patch already staged; use a fresh checkout")
    if not series.rstrip().endswith("extra/fingerprint/021-device-memory-cap.patch"):
        raise ValueError("Unexpected native patch-series order")
    subprocess.run(["git", "-C", str(fork), "apply", "--check", str(PATCH.resolve())], check=True)
    subprocess.run(["git", "-C", str(fork), "apply", str(PATCH.resolve())], check=True)
    # Upstream leaves this archive unhashed; enforce the reviewed digest on unpack.
    rust_ini.write_text(before.replace(anchor, anchor + f"sha256 = {RUST_SHA256}\n"))
    native_target.parent.mkdir(parents=True, exist_ok=True)
    native_target.write_bytes(NATIVE_PATCH.read_bytes())
    macos_target.write_bytes(MACOS_ANTIDETECT_PATCH.read_bytes())
    readback_target.write_bytes(READBACK_PARITY_PATCH.read_bytes())
    audio_rects_target.write_bytes(AUDIO_CLIENTRECTS_PATCH.read_bytes())
    apple_silicon_target.write_bytes(APPLE_SILICON_PATCH.read_bytes())
    locale_speech_font_target.write_bytes(LOCALE_SPEECH_FONT_PATCH.read_bytes())
    pixelscan_coherence_target.write_bytes(PIXELSCAN_COHERENCE_PATCH.read_bytes())
    series_path.write_text(
        series.rstrip()
        + "\n"
        + NATIVE_SERIES_ENTRY
        + "\n"
        + MACOS_ANTIDETECT_SERIES_ENTRY
        + "\n"
        + READBACK_PARITY_SERIES_ENTRY
        + "\n"
        + AUDIO_CLIENTRECTS_SERIES_ENTRY
        + "\n"
        + APPLE_SILICON_SERIES_ENTRY
        + "\n"
        + LOCALE_SPEECH_FONT_SERIES_ENTRY
        + "\n"
        + PIXELSCAN_COHERENCE_SERIES_ENTRY
        + "\n"
    )
    provenance = {
        "platform_revision": PLATFORM_REV,
        "fingerprint_revision": FINGERPRINT_REV,
        "canvas_overlay_sha256": PATCH_SHA256,
        "native_realm_patch_sha256": NATIVE_PATCH_SHA256,
        "macos_antidetect_patch_sha256": MACOS_ANTIDETECT_PATCH_SHA256,
        "webgl_readback_patch_sha256": READBACK_PARITY_PATCH_SHA256,
        "audio_clientrects_patch_sha256": AUDIO_CLIENTRECTS_PATCH_SHA256,
        "apple_silicon_coherence_patch_sha256": APPLE_SILICON_PATCH_SHA256,
        "locale_speech_font_patch_sha256": LOCALE_SPEECH_FONT_PATCH_SHA256,
        "pixelscan_coherence_patch_sha256": PIXELSCAN_COHERENCE_PATCH_SHA256,
        "rust_archive_sha256": RUST_SHA256,
        "acceptance": "EXPERIMENTAL_SOURCE_ONLY",
        "open_findings": ["native WebGPU realm coverage", "strict-CSP Blob worker compatibility"],
    }
    (root / "source-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def _patch_check(
    src_dir: Path, patch_file: str, *, reverse: bool = False
) -> subprocess.CompletedProcess:
    args = ["/usr/bin/patch", "-p1"]
    if reverse:
        args.append("-R")
    args.extend(["-f", "-C", "-d", str(src_dir), "-i", patch_file])
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def _verify_skipped_patches(src_dir: Path, patches: list[tuple[str, str]]) -> None:
    parts = []
    for patch_file, label in patches:
        for block in re.split(r"(?m)(?=^diff --git |^--- )", Path(patch_file).read_text()):
            if not block.startswith("--- "):
                continue
            target = block.splitlines()[1].removeprefix("+++ b/")
            parts.append((label, target, block))

    # Undo only touched files in a disposable tree. Peeling later patches first
    # restores overwritten contexts without mistaking repeatable insertions for missing patches.
    with tempfile.TemporaryDirectory(prefix="native-patch-guard-") as directory:
        scratch = Path(directory)
        source = scratch / "src"
        source.mkdir()
        for label, target, _ in parts:
            original = src_dir / target
            if not original.is_file():
                raise RuntimeError(f"Incremental patch {label} missing file: {target}")
            destination = source / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        patch = scratch / "part.patch"
        for label, target, block in reversed(parts):
            patch.write_text(block)
            result = subprocess.run(
                ["/usr/bin/patch", "-p1", "-R", "-f", "-d", str(source), "-i", str(patch)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Incremental patch {label} missing or inconsistent in {target}:\n"
                    + (result.stderr or result.stdout)
                )


def apply_incremental_patches(src_dir: Path, saved_source: dict | None = None) -> bool:
    """Apply pending incremental patches (023..028) onto an unpacked src tree."""
    patches = (
        (
            MACOS_ANTIDETECT_PATCH,
            MACOS_ANTIDETECT_PATCH_SHA256,
            "023",
            "macos_antidetect_patch_sha256",
        ),
        (READBACK_PARITY_PATCH, READBACK_PARITY_PATCH_SHA256, "024", "webgl_readback_patch_sha256"),
        (
            AUDIO_CLIENTRECTS_PATCH,
            AUDIO_CLIENTRECTS_PATCH_SHA256,
            "025",
            "audio_clientrects_patch_sha256",
        ),
        (
            APPLE_SILICON_PATCH,
            APPLE_SILICON_PATCH_SHA256,
            "026",
            "apple_silicon_coherence_patch_sha256",
        ),
        (
            LOCALE_SPEECH_FONT_PATCH,
            LOCALE_SPEECH_FONT_PATCH_SHA256,
            "027",
            "locale_speech_font_patch_sha256",
        ),
        (
            PIXELSCAN_COHERENCE_PATCH,
            PIXELSCAN_COHERENCE_PATCH_SHA256,
            "028",
            "pixelscan_coherence_patch_sha256",
        ),
    )
    resolved: list[tuple[str, str]] = []
    for patch_path, expected_sha, label, prov_key in patches:
        if hashlib.sha256(patch_path.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f"Incremental patch {label} checksum mismatch")
        if saved_source is not None and prov_key in saved_source:
            continue
        resolved.append((str(patch_path.resolve()), label))

    # Later patches (e.g., 024/028) replace hunks from earlier ones (023/024/027), so only the
    # latest already-applied patch cleanly passes `patch -R -C`; skip everything up to it.
    skip_until = -1
    for idx in range(len(resolved) - 1, -1, -1):
        if _patch_check(src_dir, resolved[idx][0], reverse=True).returncode == 0:
            skip_until = idx
            break

    if skip_until >= 0:
        _verify_skipped_patches(src_dir, resolved[: skip_until + 1])

    applied_any = False
    for patch_file, label in resolved[skip_until + 1 :]:
        forward = _patch_check(src_dir, patch_file, reverse=False)
        if forward.returncode == 0:
            subprocess.run(
                ["/usr/bin/patch", "-p1", "-f", "-d", str(src_dir), "-i", patch_file],
                stdin=subprocess.DEVNULL,
                check=True,
            )
            applied_any = True
            continue
        reverse = _patch_check(src_dir, patch_file, reverse=True)
        if reverse.returncode == 0:
            continue
        raise RuntimeError(
            f"Incremental patch {label} failed check:\n"
            + (forward.stderr or forward.stdout or reverse.stderr or reverse.stdout)
        )
    return applied_any


def check_resources(system: str, machine: str, free_bytes: int, memory_bytes: int) -> None:
    if system != "Darwin" or machine != "arm64":
        raise ValueError("This build targets a macOS arm64 runner only")
    # Conservative job budgets, not a promise a full build fits the runner/time limit.
    if free_bytes < 90 * 1024**3 or memory_bytes < 7 * 1024**3:
        raise ValueError(
            "Experimental low-memory build budget: >=90 GiB free disk and >=7 GiB RAM; "
            f"got {free_bytes / 1024**3:.1f} GiB free and {memory_bytes / 1024**3:.1f} GiB RAM"
        )


def preflight(root: Path) -> None:
    system, machine = platform.system(), platform.machine()
    if system != "Darwin" or machine != "arm64":
        raise ValueError("This build targets a macOS arm64 runner only")
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    check_resources(system, machine, shutil.disk_usage(root).free, memory)
    print(f"Preflight RAM: {memory / 1024**3:.1f} GiB (two-worker, no-LTO build)")
    version = subprocess.check_output(["xcodebuild", "-version"], text=True)
    if not version.startswith("Xcode 26."):
        raise ValueError("Select a full Xcode26 installation on the CI runner before dispatch")
    subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"], check=True)


def inspect_runner(root: Path) -> dict:
    """Capacity only: no source/toolchain downloads, installs or compiler invocation."""
    report = {
        "system": platform.system(),
        "machine": platform.machine(),
        "free_bytes": shutil.disk_usage(root).free,
    }
    if report["system"] == "Darwin":
        report["memory_bytes"] = int(
            subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        )
        xcode = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True)
        report["xcode"] = xcode.stdout.strip() if xcode.returncode == 0 else "UNAVAILABLE"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(inspect_runner(args.root), indent=2))
    elif args.preflight:
        preflight(args.root)
    else:
        prepare(args.root)
