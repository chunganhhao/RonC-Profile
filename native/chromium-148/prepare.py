"""Prepare pinned upstream build inputs; never install/activate a browser core."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path

PLATFORM_REV = "6a00dfe08591a15cfb0aed0edd292ba789d9a0d7"
FINGERPRINT_REV = "13b89eae304123f0710f2d33fd0816a2f61d7ffc"
PATCH_SHA256 = "df190001a4ab19a97acad9758a0c0dd03a800584e9b96c81c33484a5bedd6ce2"
# Official static.rust-lang.org dated archive's .sha256, checked 2026-11-12.
RUST_SHA256 = "a7cfb30246204096d1d77481e0aa274b98762c529f85bd5a2064fcb68b777d97"
PATCH = Path(__file__).with_name("patches") / "109-canvas-pixel-addressing.patch"
NATIVE_PATCH = Path(__file__).with_name("patches") / "022-native-gpu-realms.patch"
NATIVE_PATCH_SHA256 = "d550bc6a18121153b2050f84d6edf1528dcd2265504406340fbfe7a7562cf1bd"
NATIVE_SERIES_ENTRY = "extra/fingerprint/022-native-gpu-realms.patch"


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
    # Check every input before modifying either checkout.
    rust_ini = root / "downloads-arm64.ini"
    before = rust_ini.read_text()
    anchor = "[rust]\nversion = 2026-02-27\n"
    if before.count(anchor) != 1:
        raise ValueError("Unexpected Rust toolchain configuration")
    series_path = fork / "patches/series"
    series = series_path.read_text()
    native_target = fork / "patches" / NATIVE_SERIES_ENTRY
    if NATIVE_SERIES_ENTRY in series or native_target.exists():
        raise ValueError("Native patch already staged; use a fresh checkout")
    if not series.rstrip().endswith("extra/fingerprint/021-device-memory-cap.patch"):
        raise ValueError("Unexpected native patch-series order")
    subprocess.run(["git", "-C", str(fork), "apply", "--check", str(PATCH.resolve())], check=True)
    subprocess.run(["git", "-C", str(fork), "apply", str(PATCH.resolve())], check=True)
    # Upstream leaves this archive unhashed; enforce the reviewed digest on unpack.
    rust_ini.write_text(before.replace(anchor, anchor + f"sha256 = {RUST_SHA256}\n"))
    native_target.parent.mkdir(parents=True, exist_ok=True)
    native_target.write_bytes(NATIVE_PATCH.read_bytes())
    series_path.write_text(series.rstrip() + "\n" + NATIVE_SERIES_ENTRY + "\n")
    provenance = {
        "platform_revision": PLATFORM_REV,
        "fingerprint_revision": FINGERPRINT_REV,
        "canvas_overlay_sha256": PATCH_SHA256,
        "native_realm_patch_sha256": NATIVE_PATCH_SHA256,
        "rust_archive_sha256": RUST_SHA256,
        "acceptance": "EXPERIMENTAL_SOURCE_ONLY",
        "open_findings": ["native WebGPU realm coverage", "strict-CSP Blob worker compatibility"],
    }
    (root / "source-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def check_resources(system: str, machine: str, free_bytes: int, memory_bytes: int) -> None:
    if system != "Darwin" or machine != "arm64":
        raise ValueError("This build targets a macOS arm64 runner only")
    # Conservative job budgets, not a promise a full build fits the runner/time limit.
    if free_bytes < 100 * 1024**3 or memory_bytes < 7 * 1024**3:
        raise ValueError(
            "Experimental single-worker build budget: >=100 GiB free disk and >=7 GiB RAM"
        )


def preflight(root: Path) -> None:
    system, machine = platform.system(), platform.machine()
    if system != "Darwin" or machine != "arm64":
        raise ValueError("This build targets a macOS arm64 runner only")
    memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    check_resources(system, machine, shutil.disk_usage(root).free, memory)
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
