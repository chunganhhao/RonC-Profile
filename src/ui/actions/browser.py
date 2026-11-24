import threading
from collections.abc import Callable

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager
from ...services.browser.launcher import ProfileBusyError
from ..state import AppState


def launch_or_stop(
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    state: AppState,
    log: Callable[[str], None],
) -> None:
    profile = pm.profiles.get(name)
    if not profile or state.is_stopping(name):
        return

    if bl.is_running(name):
        state.set_stopping(name, True)
        log(get_string("stopping_profile", name=name))
        state.schedule_refresh()

        def do_stop() -> None:
            try:
                bl.stop_profile(name)
            finally:
                state.set_stopping(name, False)
                state.schedule_refresh()

        threading.Thread(target=do_stop, daemon=True).start()
        return

    state.set_loading(name, True)
    state.failed.pop(name, None)
    log(get_string("launching_profile", name=name))
    state.schedule_refresh()
    ready = threading.Event()

    def _on_ready() -> None:
        ready.set()
        state.failed.pop(name, None)
        state.set_loading(name, False)
        state.schedule_refresh()

    def _on_stop() -> None:
        if not ready.is_set():
            state.failed.setdefault(name, get_string("stamp_failed"))
        state.set_loading(name, False)
        state.schedule_refresh()

    def _launch_log(message: str) -> None:
        if "LAUNCH_FAILED:" in message:
            reason = message.split("LAUNCH_FAILED:", 1)[1].strip()
            state.failed[name] = reason or get_string("stamp_failed")
        log(message)

    try:
        started = bl.start_thread(
            profile,
            _launch_log,
            None,
            on_ready=_on_ready,
            on_stop=_on_stop,
        )
    except ProfileBusyError:
        state.set_loading(name, False)
        log(get_string("profile_busy", name=name))
        state.schedule_refresh()
        return
    if not started:
        # Launched elsewhere (API, MCP) since the check above; none of the
        # callbacks will fire for this click.
        state.set_loading(name, False)
        state.schedule_refresh()
