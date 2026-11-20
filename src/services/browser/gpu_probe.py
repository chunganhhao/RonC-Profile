"""Runner-only workaround for Camoufox's visible temporary GPU browser."""

import hashlib
import importlib.metadata
import platform
import subprocess
import sys

from camoufox import utils
from camoufox.gpudisplay import GPU_PREFS
from camoufox.host_rendering import host_gpu

from .timing import launch_stage

# shortcut: only the tested M4/macOS/core pair; re-measure before widening.
# Upstream probe work: https://github.com/daijro/camoufox/pull/985
_TESTED_BINARY_SHA256 = "cd9c9cab6a0c95d4887c11b54365051db1abc998cf1a89940ef879e76635d3e7"
_TESTED_GPU = ("Apple", "Apple M1, or similar")


def _probe(executable_path, headless, env, prefs):
    if not headless and not any(key in prefs for key in GPU_PREFS):
        with launch_stage("gpu_binary_hash"):
            digest = hashlib.sha256()
            with open(executable_path, "rb") as binary:
                while chunk := binary.read(1024 * 1024):
                    digest.update(chunk)
        if digest.hexdigest() == _TESTED_BINARY_SHA256:
            with launch_stage("gpu_probe_headless"):
                gpu = host_gpu(executable_path, True, env, prefs)
            if gpu == _TESTED_GPU:
                return gpu
    # Unknown core/settings/GPU keep upstream behavior, never invent an identity.
    with launch_stage("gpu_probe_upstream"):
        return host_gpu(executable_path, headless, env, prefs)


def install_headless_gpu_probe() -> bool:
    """Install only in the one-profile runner process; never patch site-packages."""
    if utils.host_gpu is _probe:
        return True
    if sys.platform != "darwin" or platform.mac_ver()[0] != "15.8":
        return False
    if utils.host_gpu is not host_gpu:
        return False  # Preserve another adapter, rather than stacking wrappers.
    try:
        if (
            importlib.metadata.version("camoufox") != "0.5.9"
            or importlib.metadata.version("playwright") != "1.62.0"
            or subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], timeout=2
            ).strip()
            != b"Apple M4"
        ):
            return False
    except (OSError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError):
        return False
    utils.host_gpu = _probe
    return True
