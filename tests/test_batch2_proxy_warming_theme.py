import datetime
from types import SimpleNamespace

import flet as ft
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.container import Container
from src.services.profile import manager as pm_mod
from src.services.proxy.pool import ProxyPoolService
from src.services.warming.scheduler import WarmingSchedulerService
from src.ui.components.content_area import build_content_area
from src.ui.components.profile_card import build_header_row, build_profile_card
from src.ui.components.sidebar import build_sidebar
from src.ui.dialogs.profile import open_profile_dialog
from src.ui.dialogs.proxy_pool import open_proxy_pool_dialog
from src.ui.dialogs.warming import open_warming_dialog
from src.ui.theme import (
    COLORS,
    DLG_FIELD_KWARGS,
    current_theme_mode,
    refresh_styles,
    set_theme_mode,
    toggle_theme_mode,
)


def _walk_controls(ctrl):
    yield ctrl
    for attr in ("content", "controls", "actions", "title"):
        child = getattr(ctrl, attr, None)
        for item in child if isinstance(child, list) else [child]:
            if isinstance(item, ft.Control):
                yield from _walk_controls(item)


class _DummyPage:
    def __init__(self):
        self.dialog = None
        self.closed = False

    def show_dialog(self, dlg):
        self.dialog = dlg

    def pop_dialog(self):
        self.closed = True

    def update(self):
        pass


def test_theme_switching_updates_colors_and_styles():
    orig = current_theme_mode()
    try:
        set_theme_mode("dark")
        refresh_styles()
        assert current_theme_mode() == "dark"
        assert COLORS["bg"] == "#0B0C0E"
        assert DLG_FIELD_KWARGS["bgcolor"] == "#16191F"

        toggle_theme_mode()
        refresh_styles()
        assert current_theme_mode() == "light"
        assert COLORS["bg"] == "#F8FAFC"
        assert DLG_FIELD_KWARGS["bgcolor"] == "#FFFFFF"
    finally:
        set_theme_mode(orig)
        refresh_styles()


def test_proxy_pool_crud_check_and_round_robin(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("acc-1", "", "macos")
    pm.add_profile("acc-2", "", "windows")
    pm.add_profile("acc-3", "", "linux")

    pool_file = str(tmp_path / "proxies.json")
    pool = ProxyPoolService(file_path=pool_file)

    raw_text = """
    198.51.100.10:8080:user1:pass1|US-Resi
    socks5://user2:pass2@203.0.113.20:1080 #VN-4G
    invalid_proxy_line
    """
    added, invalid = pool.bulk_import(raw_text)
    assert added == 2
    assert invalid == 1

    # Reload from disk
    pool2 = ProxyPoolService(file_path=pool_file)
    entries = pool2.list_entries()
    assert len(entries) == 2
    assert entries[0].label == "US-Resi"
    assert entries[1].label == "VN-4G"

    # Health check simulation
    class FakeChecker:
        def check_proxy_sync(self, proxy_str: str, timeout: int = 10):
            if "198.51.100.10" in proxy_str:
                return True, "198.51.100.10"
            return False, "Connection refused"

    pool2.check_all(FakeChecker())
    statuses = {e.label: e.status for e in pool2.list_entries()}
    assert statuses == {"US-Resi": "working", "VN-4G": "dead"}

    # Assign only healthy proxies across 3 profiles
    updated = pool2.assign_round_robin(["acc-1", "acc-2", "acc-3"], pm, only_working=True)
    assert updated == 3
    for name in ("acc-1", "acc-2", "acc-3"):
        assert "198.51.100.10:8080" in pm.profiles[name].proxy

    usage = pool2.usage_counts(pm)
    working_id = next(e.id for e in pool2.list_entries() if e.status == "working")
    assert usage[working_id] == 3

    removed_dead = pool2.clear_dead()
    assert removed_dead == 1
    assert len(pool2.list_entries()) == 1


def test_warming_scheduler_execution_and_due_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("warm-1", "", "macos")
    pm.add_profile("warm-already-open", "", "macos")

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    # Reject invalid schedule time
    assert ws.add_task("Bad", ["warm-1"], schedule_time="25:99") is None

    task = ws.add_task(
        name="Morning Trust",
        profile_names=["warm-1", "warm-already-open"],
        preset="youtube_trust",
        duration_minutes=2,
        schedule_time="09:30",
        repeat_daily=False,
    )
    assert task is not None

    actions_log = []
    stopped_profiles = []

    class FakeLauncher:
        def __init__(self):
            self.running = {"warm-already-open"}

        def is_running(self, name: str) -> bool:
            return name in self.running

        def start_thread(self, profile, log_callback, **kwargs) -> bool:
            self.running.add(profile.name)
            return True

        def wait_for_launch(self, name: str, timeout: float):
            return True, None

        def control(self, name: str, action: str, params=None, timeout: float = 60):
            actions_log.append((name, action, params))
            return {"ok": True}

        def stop_profile(self, name: str, timeout: int = 2) -> bool:
            self.running.discard(name)
            stopped_profiles.append(name)
            return True

    bl = FakeLauncher()
    ok = ws.execute_task_sync(task.id, pm, bl, step_dwell_seconds=0.0)
    assert ok is True
    updated_task = ws.get_task(task.id)
    assert updated_task.status == "completed"
    assert updated_task.enabled is False  # one-shot schedule disables after run
    # Only the profile launched by warming should be stopped; already-open stays open
    assert stopped_profiles == ["warm-1"]
    assert "warm-already-open" in bl.running
    assert any(a[1] == "navigate" for a in actions_log)

    # Test due task slot deduplication
    daily = ws.add_task(
        name="Daily Social",
        profile_names=["warm-already-open"],
        preset="social_news",
        schedule_time="14:15",
        repeat_daily=True,
    )
    monkeypatch.setattr(ws, "run_task_async", lambda tid, *a, **kw: True)
    dt = datetime.datetime(2026, 11, 8, 14, 15, 10)
    assert ws.check_due_tasks(pm, bl, now=dt) == [daily.id]
    # Calling again in the same minute must not re-trigger
    assert ws.check_due_tasks(pm, bl, now=dt) == []


def test_batch2_api_endpoints(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("api-prof", "", "macos")

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        res = client.post(
            "/api/v1/proxy/pool/bulk",
            json={"text": "198.51.100.99:8080:u:p|PoolTag"},
        )
        assert res.status_code == 200
        assert res.json()["added"] == 1

        pool_res = client.get("/api/v1/proxy/pool")
        assert pool_res.status_code == 200
        assert pool_res.json()["total"] == 1
        pid = pool_res.json()["proxies"][0]["id"]

        assign_res = client.post(
            "/api/v1/proxy/pool/assign",
            json={"profile_names": ["api-prof"]},
        )
        assert assign_res.status_code == 200
        assert assign_res.json()["updated"] == 1

        del_res = client.delete(f"/api/v1/proxy/pool/{pid}")
        assert del_res.status_code == 200

        w_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "API Warm",
                "profile_names": ["api-prof"],
                "preset": "youtube_trust",
                "duration_minutes": 2,
                "schedule_time": "10:00",
            },
        )
        assert w_res.status_code == 201
        tid = w_res.json()["id"]

        list_w = client.get("/api/v1/warming/tasks")
        assert list_w.status_code == 200
        assert len(list_w.json()["tasks"]) == 1

        del_w = client.delete(f"/api/v1/warming/tasks/{tid}")
        assert del_w.status_code == 200


def test_pionlogin_ui_components_and_dialogs(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile(
        "pion-1",
        "socks5://u:p@198.51.100.1:1080",
        "macos",
        group="Google / Gmail",
        notes="Fam AGY Pixel",
    )
    prof = pm.profiles["pion-1"]

    header = build_header_row(False, False, lambda _: None)
    card = build_profile_card(
        prof,
        is_loading=False,
        is_running=True,
        on_launch=lambda _: None,
        on_edit=lambda _: None,
        on_delete=lambda _: None,
        row_index=5,
    )
    assert isinstance(header, ft.Container)
    assert isinstance(card, ft.Container)

    sidebar = build_sidebar(
        ft.Text("Total: 1"),
        ft.Text("Running: 1"),
        ft.Text(""),
        ft.Container(),
        ft.TextButton("Log"),
        on_new_profile=lambda _: None,
        on_import=lambda _: None,
        on_export=lambda _: None,
        on_toggle_log=lambda _: None,
        on_mcp=lambda _: None,
        on_fullscreen_log=lambda _: None,
        on_open_proxy_pool=lambda _: None,
        on_open_warming=lambda _: None,
        on_toggle_theme=lambda _: None,
        proxy_pool_count=3,
        warming_task_count=2,
    )
    content = build_content_area(
        ft.Text("1 profile"),
        ft.Column(controls=[card]),
        ft.IconButton(icon=ft.Icons.CHEVRON_LEFT),
        ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT),
        ft.Text("Page 1 of 1"),
        on_open_warming=lambda _: None,
        on_open_proxy_pool=lambda _: None,
        on_toggle_theme=lambda _: None,
    )
    assert isinstance(sidebar, ft.Container)
    assert isinstance(content, ft.Container)

    # Test Proxy Pool Dialog & Profile Dialog Pick from Pool button
    pool = ProxyPoolService()
    pool.add_proxy("198.51.100.77:8080:user:pass", label="Resi")

    page = _DummyPage()
    open_proxy_pool_dialog(
        page,
        pool,
        SimpleNamespace(check_proxy_sync=lambda p, timeout=10: (True, "198.51.100.77")),
        pm,
    )
    assert page.dialog is not None

    page_w = _DummyPage()
    ws = WarmingSchedulerService()
    open_warming_dialog(page_w, ws, pm, SimpleNamespace())
    assert page_w.dialog is not None

    # Test Pick from Pool inside open_profile_dialog
    page_p = _DummyPage()
    open_profile_dialog(page_p, None, lambda *a, **kw: None, prof)
    try:
        ctrls = list(_walk_controls(page_p.dialog))
        pick_btn = next(
            c
            for c in ctrls
            if isinstance(c, ft.OutlinedButton) and c.icon == ft.Icons.STORAGE_OUTLINED
        )
        pick_btn.on_click(None)
        proxy_input = next(
            c
            for c in ctrls
            if isinstance(c, ft.TextField) and c.hint_text == "socks5://user:pass@host:port"
        )
        assert "198.51.100.77:8080" in proxy_input.value
    finally:
        page_p.dialog.on_dismiss(None)


def test_full_app_e2e_lifecycle_theme_and_dialogs(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.core.strings import get_string
    from src.ui.app import App

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile(
        "haindl8799@gmail.com",
        "66.93.164.242:50100:u:p",
        "macos",
        group="Google / Gmail",
        notes="Fam Gem Pixel conimmyeudoi@gmail.com",
    )
    container.proxy_pool.add_proxy("66.93.164.242:50100:u:p", label="US-Pixel")
    container.warming_scheduler.add_task(
        name="Warm Gmail",
        profile_names=["haindl8799@gmail.com"],
        preset="youtube_trust",
        schedule_time="08:00",
    )

    app = App(container)

    class FullDummyPage:
        def __init__(self):
            self.title = ""
            self.window = SimpleNamespace(width=0, height=0, min_width=0, min_height=0, icon="")
            self.padding = 0
            self.spacing = 0
            self.fonts = {}
            self.theme_mode = None
            self.bgcolor = ""
            self.theme = None
            self.services = []
            self.controls = []
            self.dialog = None
            self.tasks = []

        def add(self, ctrl):
            self.controls.append(ctrl)

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            self.dialog = None

        def run_task(self, coro, *args):
            self.tasks.append(coro)

    page = FullDummyPage()
    try:
        app._main(page)
        assert len(page.controls) == 1
        assert "1" in app._total_count_text.value
        assert "1" in app._footer_proxy_text.value
        assert "1" in app._footer_warming_text.value

        # Switch theme Light <-> Dark
        app._set_theme("light")
        assert current_theme_mode() == "light"
        assert page.theme_mode == ft.ThemeMode.LIGHT

        app._set_theme("dark")
        assert current_theme_mode() == "dark"
        assert page.theme_mode == ft.ThemeMode.DARK

        # Switch language VI <-> EN
        app._set_language("vi")
        assert "Tổng cộng: 1" in app._total_count_text.value
        app._set_language("en")
        assert "Total: 1" in app._total_count_text.value

        # Switch Hallmark accent presets & open Appearance dialog
        app._set_accent("green")
        app._set_accent("orange")
        app._set_accent("blue")
        app._open_appearance()
        assert page.dialog is not None
        page.pop_dialog()

        # Verify Copy Email / Password / 2FA visual feedback & toast banner
        prof = app.pm.profiles["haindl8799@gmail.com"]
        prof.account_uid = "haindl8799@gmail.com"
        prof.account_password = "secret"
        prof.two_fa_secret = "JBSWY3DPEHPK3PXP"
        app._on_copy_user("haindl8799@gmail.com")
        assert ("haindl8799@gmail.com", "copy_user") in app._feedback_until
        assert app._toast_banner.visible is True
        assert "{name}" not in app._toast_text.value
        assert prof.account_uid in app._toast_text.value
        app._on_copy_pass("haindl8799@gmail.com")
        assert ("haindl8799@gmail.com", "copy_pass") in app._feedback_until
        app._on_copy_2fa("haindl8799@gmail.com")
        assert ("haindl8799@gmail.com", "copy_2fa") in app._feedback_until
        app._log(
            get_string("updated_profile", old="haindl8799@gmail.com", new="haindl8799@gmail.com")
        )
        assert ("haindl8799@gmail.com", "edit_profile") in app._feedback_until

        # Open Proxy Pool & Warming Dialogs from App
        app._open_proxy_pool()
        assert page.dialog is not None
        page.pop_dialog()

        app._open_warming()
        assert page.dialog is not None
        page.pop_dialog()
    finally:
        app.warming.stop_daemon()


def test_b2_f1_to_f4_warming_concurrency_duration_and_api_lifespan(tmp_path, monkeypatch):
    import threading

    from src.core import config as app_config
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("shared-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    # B2-F3: duration_minutes is honored across multi-pass loop
    t_dur = ws.add_task("60m", ["shared-prof"], duration_minutes=60)
    waits = []

    class FakeEvent:
        def is_set(self):
            return False

        def wait(self, sec):
            waits.append(sec)
            return False

    class FastLauncher:
        def is_running(self, name):
            return True

        def control(self, *a, **kw):
            return {}

    orig_threading = sched_mod.threading
    monkeypatch.setattr(sched_mod, "threading", SimpleNamespace(Event=FakeEvent))
    assert ws.execute_task_sync(t_dur.id, pm, FastLauncher())
    assert sum(waits) >= 3600
    monkeypatch.setattr(sched_mod, "threading", orig_threading)

    # B2-F1 & B2-F2: stop-then-rerun token isolation & shared profile reference counting
    class GateLauncher:
        def __init__(self):
            self.running = set()
            self.stops = []
            self.entered = [threading.Event(), threading.Event()]
            self.release = [threading.Event(), threading.Event()]
            self.lock = threading.Lock()
            self.idx = 0

        def is_running(self, name):
            return name in self.running

        def start_thread(self, profile, log):
            self.running.add(profile.name)
            return True

        def wait_for_launch(self, name, timeout):
            return True, None

        def control(self, name, action, params=None, timeout=None):
            if action == "navigate":
                with self.lock:
                    i = self.idx
                    self.idx += 1
                self.entered[i].set()
                self.release[i].wait(2)
            return {}

        def stop_profile(self, name, timeout=2):
            self.stops.append(name)
            self.running.discard(name)
            return True

    t_a = ws.add_task(
        "TaskA", ["shared-prof"], preset="custom", custom_urls=["https://example.test/a"]
    )
    t_b = ws.add_task(
        "TaskB", ["shared-prof"], preset="custom", custom_urls=["https://example.test/b"]
    )
    bl = GateLauncher()
    th_a = threading.Thread(
        target=ws.execute_task_sync,
        args=(t_a.id, pm, bl),
        kwargs={"step_dwell_seconds": 0},
        daemon=True,
    )
    th_b = threading.Thread(
        target=ws.execute_task_sync,
        args=(t_b.id, pm, bl),
        kwargs={"step_dwell_seconds": 0},
        daemon=True,
    )
    th_a.start()
    assert bl.entered[0].wait(2)
    th_b.start()
    assert bl.entered[1].wait(2)
    bl.release[0].set()
    th_a.join(2)
    # Task A finished while Task B is still navigating: shared-prof must remain running!
    assert bl.is_running("shared-prof")
    bl.release[1].set()
    th_b.join(2)
    # Once Task B (the last user) finishes, the warming-launched profile is stopped cleanly
    assert not bl.is_running("shared-prof")
    assert bl.stops == ["shared-prof"]

    # R2-B2-F2: last-user cleanup racing against new acquisition preserves browser & ownership
    cleanup_gate, release_cleanup = threading.Event(), threading.Event()
    b_nav_gate, release_b_nav = threading.Event(), threading.Event()

    class RaceLauncher:
        running = False
        a_checks = 0
        stops = []

        def is_running(self, name):
            if threading.current_thread().name == "race-A":
                self.a_checks += 1
                if self.a_checks == 2:
                    cleanup_gate.set()
                    release_cleanup.wait(2)
            return self.running

        def start_thread(self, profile, log):
            self.running = True
            return True

        def wait_for_launch(self, name, timeout):
            return True, None

        def control(self, name, action, params=None, timeout=None):
            if threading.current_thread().name == "race-B" and action == "navigate":
                b_nav_gate.set()
                release_b_nav.wait(2)
            return {}

        def stop_profile(self, name, timeout=2):
            self.stops.append(name)
            self.running = False
            return True

    r_bl = RaceLauncher()
    r_a = threading.Thread(
        target=ws.execute_task_sync,
        args=(t_a.id, pm, r_bl),
        kwargs={"step_dwell_seconds": 0},
        name="race-A",
        daemon=True,
    )
    r_b = threading.Thread(
        target=ws.execute_task_sync,
        args=(t_b.id, pm, r_bl),
        kwargs={"step_dwell_seconds": 0},
        name="race-B",
        daemon=True,
    )
    r_a.start()
    assert cleanup_gate.wait(2)
    r_b.start()
    assert b_nav_gate.wait(2)
    release_cleanup.set()
    r_a.join(2)
    assert r_bl.running is True
    assert r_bl.stops == []
    release_b_nav.set()
    r_b.join(2)
    assert r_bl.running is False
    assert r_bl.stops == ["shared-prof"]

    # R2-B2-F4: daemon restart while old thread is stopping spawns a live replacement generation
    wait_entered, stop_observed, allow_exit = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class GatedStop(threading.Event):
        def wait(self, timeout=None):
            wait_entered.set()
            res = super().wait(timeout)
            if res:
                stop_observed.set()
                allow_exit.wait(2)
            return res

    ws._daemon_stop = GatedStop()
    try:
        ws.start_daemon(pm, r_bl)
        assert wait_entered.wait(2)
        old_thread = ws._daemon_thread
        ws.stop_daemon()
        assert stop_observed.wait(2)
        ws.start_daemon(pm, r_bl)
        allow_exit.set()
        old_thread.join(2)
        assert ws._daemon_thread.is_alive()
    finally:
        allow_exit.set()
        ws.stop_daemon()
        if ws._daemon_thread:
            ws._daemon_thread.join(2)

    # R3-B2-F2: busy profile (exclusive lock) on first target does not escape, leaks 0 leases, and still warms remaining targets
    from src.services.browser.launcher import BrowserLauncher

    real_bl = BrowserLauncher()
    busy_task = ws.add_task(
        "Busy Launch Check",
        ["shared-prof"],
        preset="custom",
        custom_urls=["https://example.org"],
    )
    with real_bl.exclusive("shared-prof"):
        ok_busy = ws.execute_task_sync(busy_task.id, pm, real_bl, step_dwell_seconds=0)
    assert ok_busy is False
    assert busy_task.status == "failed"
    assert ws._profile_active_users == {}
    assert ws._cancel_flags == {}

    # Ponytail Finding #2: Pick from Pool selects least-used working proxy
    pool = ProxyPoolService(file_path=str(tmp_path / "proxies_least.json"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies_least.json"))
    e_busy = pool.add_proxy("198.51.100.10:8080", label="Busy")
    e_free = pool.add_proxy("198.51.100.20:8080", label="Free")
    e_free.status = "working"
    pool._save()
    pm.add_profile("uses-10", e_busy.proxy, "macos")
    page_pick = _DummyPage()
    open_profile_dialog(page_pick, None, lambda *a, **kw: None, profile_manager=pm)
    try:
        ctrls_pick = list(_walk_controls(page_pick.dialog))
        btn_pick = next(
            c
            for c in ctrls_pick
            if isinstance(c, ft.OutlinedButton) and c.icon == ft.Icons.STORAGE_OUTLINED
        )
        btn_pick.on_click(None)
        p_field = next(
            c
            for c in ctrls_pick
            if isinstance(c, ft.TextField) and c.hint_text == "socks5://user:pass@host:port"
        )
        assert p_field.value == e_free.proxy
    finally:
        page_pick.dialog.on_dismiss(None)

    # Ponytail Finding #5 (and Codex Re-Review #3): normalize_warming_urls splits by whitespace/newlines
    # and never splits commas/semicolons inside URL queries/paths (even when domain-looking)
    from src.services.warming.scheduler import normalize_warming_urls

    parsed_urls = normalize_warming_urls(
        "https://www.google.com/search?q=coffee,tea.example\nhttps://en.wikipedia.org/wiki/A;next.example https://example.org/path"
    )
    assert parsed_urls == [
        "https://www.google.com/search?q=coffee,tea.example",
        "https://en.wikipedia.org/wiki/A;next.example",
        "https://example.org/path",
    ]

    # Ponytail Finding #6: _lease_lock decouples task CRUD from blocking browser launch/stop
    launch_entered = threading.Event()
    release_launch = threading.Event()

    class SlowStartLauncher:
        def is_running(self, name):
            return False

        def start_thread(self, profile, log):
            launch_entered.set()
            release_launch.wait(2)
            return False

    slow_task = ws.add_task(
        "Slow Launch", ["shared-prof"], preset="custom", custom_urls=["https://example.org"]
    )
    t_slow = threading.Thread(
        target=ws.execute_task_sync,
        args=(slow_task.id, pm, SlowStartLauncher()),
        kwargs={"step_dwell_seconds": 0},
        daemon=True,
    )
    t_slow.start()
    assert launch_entered.wait(2)
    # Must complete immediately without blocking on _lease_lock
    assert len(ws.list_tasks()) >= 1
    release_launch.set()
    t_slow.join(2)

    # Ponytail Finding #7: assign_round_robin persists once for N profiles
    save_calls = 0
    orig_save = pm.save_profiles

    def counted_save():
        nonlocal save_calls
        save_calls += 1
        orig_save()

    monkeypatch.setattr(pm, "save_profiles", counted_save)
    assert pool.assign_round_robin(["shared-prof", "uses-10"], pm) == 2
    assert save_calls == 1
