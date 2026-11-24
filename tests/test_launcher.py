import sys
import threading

import pytest

from src.core.strings import get_string
from src.models.profile import Profile
from src.services.browser import launcher as launcher_mod
from src.services.browser.launcher import BrowserLauncher, ProfileBusyError
from src.ui.actions.browser import launch_or_stop
from src.ui.state import AppState


class FakeProc:
    def poll(self):
        return None


@pytest.mark.parametrize("blocked_log", [False, True])
@pytest.mark.parametrize("error_type", ["EngineError", "RuntimeError"])
def test_chrome_traceback_stays_in_file_not_launcher_callback(
    monkeypatch, tmp_path, error_type, blocked_log
):
    from src.services.browser import process

    monkeypatch.chdir(tmp_path)
    if blocked_log:
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs").chmod(0o500)
    monkeypatch.setenv("FOXPROFILE_LOG_DIR", str(tmp_path / "logs"))
    script = f"""
import sys
from chrome_engine import runner
from chrome_engine.engine import EngineError

class Engine:
    def __init__(self, *args, **kwargs):
        pass
    async def start(self):
        raise {error_type}("fixture launch failure")
    async def stop(self):
        pass

runner.ChromeEngine = Engine
runner._read_profile_extra = lambda _: {{}}
async def installed():
    pass
runner.ensure_installed = installed
runner._async_main = lambda _: runner.run_browser("shop-01", "None", "windows")
sys.argv = ["runner", "shop-01", "None", "windows"]
sys.exit(runner.main())
"""
    monkeypatch.setattr(process, "runner_command", lambda _: [sys.executable, "-c", script])
    proc = process.spawn_browser(Profile("shop-01", engine="chrome"))
    messages = []
    launcher = BrowserLauncher()
    result = launcher_mod._LaunchResult()
    try:
        launcher._monitor_process(proc, "shop-01", messages.append, None, lambda: None, result)
    finally:
        process.terminate(proc, "shop-01")
    assert result.error == (
        "fixture launch failure"
        if error_type == "EngineError"
        else "RuntimeError: fixture launch failure"
    )
    assert any("LAUNCH_FAILED:" in message for message in messages)
    assert not any("Traceback" in message or 'File "' in message for message in messages), messages
    logs = list((tmp_path / "logs").glob("foxprofile_*.log"))
    if blocked_log:
        assert logs == []
        return
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "Chrome launch failed\nTraceback (most recent call last):" in text
    assert f"{error_type}: fixture launch failure" in text


def test_chrome_unwritable_log_does_not_block_launch(monkeypatch, tmp_path):
    from src.services.browser import process

    monkeypatch.chdir(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o500)
    monkeypatch.setenv("FOXPROFILE_LOG_DIR", str(log_dir))
    script = """
import sys
from chrome_engine import runner
async def ready(_):
    print("BROWSER_STARTED", flush=True)
    return 0
runner._async_main = ready
sys.argv = ["runner", "shop-01", "None", "linux"]
sys.exit(runner.main())
"""
    monkeypatch.setattr(process, "runner_command", lambda _: [sys.executable, "-c", script])
    proc = process.spawn_browser(Profile("shop-01", engine="chrome"))
    output, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0, output
    assert output == "BROWSER_STARTED\n"


def test_store_busy_failure_uses_current_language_after_spawn(monkeypatch):
    from src.core import strings
    from src.services.browser import process

    monkeypatch.setattr(strings, "_ACTIVE", strings.STRINGS["en"])
    script = (
        "import errno, asyncio;"
        "from chrome_engine import runner;"
        "from src.services.browser import lease;"
        "lease.store_lease = lambda *a, **kw: (_ for _ in ()).throw(lease.StoreLeaseTimeout());"
        "raise SystemExit(asyncio.run(runner._async_main(['qa', 'None', 'macos'])))"
    )
    monkeypatch.setattr(process, "runner_command", lambda _: [sys.executable, "-c", script])
    proc = process.spawn_browser(Profile("qa", engine="chrome"))
    strings.set_language("vi")
    launcher = BrowserLauncher()
    messages = []
    result = launcher_mod._LaunchResult()
    try:
        launcher._monitor_process(proc, "qa", messages.append, None, lambda: None, result)
    finally:
        process.terminate(proc, "qa")
    assert result.error == "Kho profile đang bận. Hãy thử lại sau giây lát."
    assert messages[-1] == f"[qa] LAUNCH_FAILED: {result.error}"
    launcher._launch_results["qa"] = result
    strings.set_language("en")
    assert launcher.wait_for_launch("qa", 0) == (
        False,
        "The profile store is busy. Try again in a moment.",
    )
    assert strings.localize_launch_error(messages[-1]) == (
        "[qa] LAUNCH_FAILED: The profile store is busy. Try again in a moment."
    )


@pytest.fixture
def bl(monkeypatch):
    spawned = []

    def spawn(profile):
        spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", spawn)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    b = BrowserLauncher()
    monkeypatch.setattr(b, "_monitor_process", lambda *a: None)
    b.spawned = spawned
    yield b
    # Fake processes must not reach terminate() at interpreter exit.
    b._active_sessions.clear()


def test_second_launch_returns_false_without_callbacks(bl):
    calls = []
    assert bl.start_thread(Profile(name="p"), lambda m: None) is True
    started = bl.start_thread(
        Profile(name="p"),
        lambda m: None,
        on_start=lambda: calls.append("start"),
        on_stop=lambda: calls.append("stop"),
    )
    assert started is False
    assert calls == []
    assert bl.spawned == ["p"]


def test_concurrent_launches_spawn_one_browser(bl, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(profile):
        entered.set()
        release.wait(5)
        bl.spawned.append(profile.name)
        return FakeProc()

    monkeypatch.setattr(launcher_mod, "spawn_browser", slow_spawn)
    first = threading.Thread(target=bl.start_thread, args=(Profile(name="p"), lambda m: None))
    first.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ProfileBusyError):
            bl.start_thread(Profile(name="p"), lambda m: None)
    finally:
        release.set()
        first.join(5)
    assert bl.spawned == ["p"]
    assert bl.is_running("p")


def test_failed_spawn_releases_the_profile(bl, monkeypatch):
    def broken(profile):
        raise OSError("no runner")

    monkeypatch.setattr(launcher_mod, "spawn_browser", broken)
    stopped = []
    assert bl.start_thread(Profile(name="p"), lambda m: None, on_stop=lambda: stopped.append(1))
    assert stopped == [1]
    with bl.exclusive("p"):
        pass


class _Profiles:
    def __init__(self, *names):
        self.profiles = {n: Profile(name=n) for n in names}


def test_ui_keeps_launch_failure_and_clears_on_success():
    from types import SimpleNamespace

    state = AppState()
    logs = []

    def start(profile, log, _, on_ready, on_stop):
        log("[demo] LAUNCH_FAILED: Could not start browser")
        on_stop()
        return True

    browser = SimpleNamespace(is_running=lambda _: False, start_thread=start)
    launch_or_stop("demo", _Profiles("demo"), browser, state, logs.append)
    assert state.failed["demo"] == "Could not start browser"
    assert logs == [
        get_string("launching_profile", name="demo"),
        "[demo] LAUNCH_FAILED: Could not start browser",
    ]

    def ready(profile, log, _, on_ready, on_stop):
        assert "demo" not in state.failed
        on_ready()
        on_stop()
        return True

    browser.start_thread = ready
    launch_or_stop("demo", _Profiles("demo"), browser, state, logs.append)
    assert "demo" not in state.failed


def test_ui_failure_without_protocol_has_fallback():
    from types import SimpleNamespace

    state = AppState()

    def start(profile, log, _, on_ready, on_stop):
        on_stop()
        return True

    browser = SimpleNamespace(is_running=lambda _: False, start_thread=start)
    launch_or_stop("demo", _Profiles("demo"), browser, state, lambda _: None)
    assert state.failed["demo"] == get_string("stamp_failed")


def test_ui_click_does_not_stay_loading_when_launched_elsewhere(bl, monkeypatch):
    state = AppState()
    # The API launched the profile between the UI's is_running check and its
    # start_thread call.
    monkeypatch.setattr(bl, "is_running", lambda name: False)
    bl.start_thread(Profile(name="p"), lambda m: None)

    launch_or_stop("p", _Profiles("p"), bl, state, lambda m: None)

    assert not state.is_loading("p")
    assert "p" not in state.failed
    assert bl.spawned == ["p"]


def test_a_raising_callback_does_not_leave_the_profile_busy(bl):
    def boom():
        raise RuntimeError("UI gone")

    with pytest.raises(RuntimeError):
        bl.start_thread(Profile(name="p"), lambda m: None, on_start=boom)
    with bl.exclusive("p"):
        pass
    assert bl.spawned == []


def test_an_old_session_ending_keeps_the_newer_launch(bl, monkeypatch):
    exits = []
    monkeypatch.setattr(
        launcher_mod, "wait_for_exit", lambda proc, name, notify: exits.append(notify)
    )
    bl.start_thread(Profile(name="p"), lambda m: None)
    # The first browser is gone but its exit callback has not run yet.
    bl._active_sessions.clear()
    bl._stop_notifiers.clear()
    bl.start_thread(Profile(name="p"), lambda m: None)

    exits[0]()

    assert bl.is_running("p")
    assert bl.spawned == ["p", "p"]


def test_api_launch_answers_409_when_the_browser_appeared_meanwhile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container

    monkeypatch.chdir(tmp_path)
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.post("/api/v1/profiles", json={"name": "p"}).status_code in (200, 201)
    monkeypatch.setattr(container.browser_launcher, "is_running", lambda name: False)
    monkeypatch.setattr(container.browser_launcher, "start_thread", lambda *a, **k: False)

    r = client.post("/api/v1/browser/p/launch")

    assert r.status_code == 409
    assert r.json()["detail"] == "Browser already running"
