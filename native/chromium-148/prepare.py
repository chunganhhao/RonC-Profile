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
    # Check every input before modifying either checkout.
    rust_ini = root / "downloads-arm64.ini"
    before = rust_ini.read_text()
    anchor = "[rust]\nversion = 2026-02-27\n"
    if before.count(anchor) != 1:
        raise ValueError("Unexpected Rust toolchain configuration")
    subprocess.run(["git", "-C", str(fork), "apply", "--check", str(PATCH.resolve())], check=True)
    subprocess.run(["git", "-C", str(fork), "apply", str(PATCH.resolve())], check=True)
    # Upstream leaves this archive unhashed; enforce the reviewed digest on unpack.
    rust_ini.write_text(before.replace(anchor, anchor + f"sha256 = {RUST_SHA256}\n"))
    provenance = {
        "platform_revision": PLATFORM_REV,
        "fingerprint_revision": FINGERPRINT_REV,
        "canvas_overlay_sha256": PATCH_SHA256,
        "rust_archive_sha256": RUST_SHA256,
        "acceptance": "EXPERIMENTAL_SOURCE_ONLY",
        "open_findings": ["native WebGPU realm coverage", "strict-CSP Blob worker compatibility"],
    }
    (root / "source-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def check_resources(system: str, machine: str, free_bytes: int, memory_bytes: int) -> None:
    if system != "Darwin" or machine != "arm64":
        raise ValueError("This build targets a macOS arm64 runner only")
    # Conservative job budgets, not a promise a full build fits the runner/time limit.
    if free_bytes < 200 * 1024**3 or memory_bytes < 16 * 1024**3:
        raise ValueError("Dedicated runner needs >=200 GiB free disk and >=16 GiB RAM")


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if args.preflight:
        preflight(args.root)
    else:
        prepare(args.root)
