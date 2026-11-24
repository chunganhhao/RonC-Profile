import asyncio
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress

from ...core.logging import get_logger
from ...models.profile import Profile
from . import display

logger = get_logger("browser.process")


_PROJECT_ROOT = str(pathlib.Path(__file__).parents[3])
_LAUNCHER_PIPE = "RONC_RUNNER_LAUNCHER_PIPE"


def runner_command(profile: Profile) -> list[str]:
    """The runner for the profile's engine; both speak the same stdout protocol."""
    if profile.engine == "chrome":
        runner = ["-m", "chrome_engine.runner"]
    else:
        runner = [os.path.join(pathlib.Path(__file__).parent, "runner.py")]
    command = [
        sys.executable,
        *runner,
        profile.name,
        str(profile.proxy),
        profile.os_type,
        profile.timezone or "",
        profile.locale or "",
    ]
    if profile.core_version:
        command.append(profile.core_version)
    return command


def spawn_browser(profile: Profile) -> subprocess.Popen:
    """Spawn the browser subprocess for the given profile."""
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PROJECT_ROOT + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    env[_LAUNCHER_PIPE] = "1"
    return subprocess.Popen(
        runner_command(profile),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=pathlib.Path.cwd(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def watch_launcher(shutdown: asyncio.Event) -> bool:
    """Use the launcher-owned pipe's EOF to detect even an uncatchable exit."""
    if os.environ.pop(_LAUNCHER_PIPE, None) != "1":
        return False
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()

    def wait_for_eof() -> None:
        with suppress(OSError):
            while os.read(fd, 4096):
                pass
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(shutdown.set)

    threading.Thread(target=wait_for_eof, daemon=True).start()
    return True


def terminate(proc: subprocess.Popen, name: str, timeout: int = 5) -> None:
    """Gracefully terminate a browser process, force-kill on timeout."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
            logger.info("Browser %s terminated gracefully", name)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
            logger.warning("Browser %s force killed after timeout", name)
    except Exception as e:
        logger.exception("Error terminating browser %s: %s", name, e)
    finally:
        cleanup(proc)


def cleanup(proc: subprocess.Popen) -> None:
    """Release the launcher pipe and any leftover Xvfb after runner exit."""
    if proc.poll() is not None:
        if proc.stdin is not None:
            proc.stdin.close()
        display.stop_for_runner(proc.pid)


def wait_for_exit(
    proc: subprocess.Popen,
    name: str,
    notify_stopped: Callable[[], None],
) -> None:
    """Block until the process exits, then fire the callback."""
    try:
        proc.wait()
    except Exception as e:
        logger.exception("Wait error for profile %s: %s", name, e)
    finally:
        # A runner that died on its own (crash, OOM kill) never ran its cleanup.
        cleanup(proc)
        notify_stopped()
