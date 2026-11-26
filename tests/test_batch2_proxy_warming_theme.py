import asyncio
import datetime
import errno
import json
import threading
from types import SimpleNamespace
from weakref import ref

import flet as ft
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.container import Container
from src.core.strings import get_string
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
    ACCENT_STYLE,
    COLORS,
    DLG_FIELD_KWARGS,
    OUTLINE_STYLE,
    current_theme_mode,
    refresh_styles,
    set_theme_mode,
    toggle_theme_mode,
)


def _assert_disabled_button_styles():
    for style, background, enabled_color, enabled_background in (
        (ACCENT_STYLE, "rule_strong", "#FFFFFF", COLORS["accent"]),
        (OUTLINE_STYLE, "paper_sunk", COLORS["text_main"], COLORS["card_bg"]),
    ):
        assert style.color[ft.ControlState.DISABLED] == COLORS["text_dim"]
        assert style.bgcolor[ft.ControlState.DISABLED] == COLORS[background]
        assert style.color[ft.ControlState.DEFAULT] == enabled_color
        assert style.bgcolor[ft.ControlState.DEFAULT] == enabled_background
        assert style.color[ft.ControlState.DISABLED] != enabled_color
        assert style.bgcolor[ft.ControlState.DISABLED] != enabled_background


def test_initial_disabled_button_styles():
    _assert_disabled_button_styles()


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
        self.dialog_stack = ft.Column(controls=[dlg])
        dlg._parent = ref(self.dialog_stack)

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
        _assert_disabled_button_styles()

        toggle_theme_mode()
        refresh_styles()
        assert current_theme_mode() == "light"
        assert COLORS["bg"] == "#F8FAFC"
        assert DLG_FIELD_KWARGS["bgcolor"] == "#FFFFFF"
        _assert_disabled_button_styles()
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
    monkeypatch.setattr(ws, "_execute_task", lambda *_args: None)
    dt = datetime.datetime(2026, 11, 8, 14, 15, 10)
    assert ws.check_due_tasks(pm, bl, now=dt) == [daily.id]
    # Calling again in the same minute must not re-trigger
    assert ws.check_due_tasks(pm, bl, now=dt) == []


def test_due_warming_task_retries_in_same_minute_after_store_contention(tmp_path, monkeypatch):
    path = str(tmp_path / "warming.json")
    ws = WarmingSchedulerService(file_path=path)
    due = ws.add_task("Due", ["Profile"], schedule_time="14:15")
    peer = WarmingSchedulerService(file_path=path)
    started = threading.Event()
    monkeypatch.setattr(ws, "_execute_task", lambda *_args: started.set())
    dt = datetime.datetime(2026, 11, 8, 14, 15, 10)

    with peer._store_lease(), pytest.raises(OSError):
        ws.check_due_tasks(None, None, now=dt)

    assert not started.is_set()
    assert ws.get_task(due.id).last_triggered_slot == ""
    assert ws.check_due_tasks(None, None, now=dt) == [due.id]
    assert started.wait(5)
    assert ws.check_due_tasks(None, None, now=dt) == []
    assert WarmingSchedulerService(file_path=path).get_task(due.id).last_triggered_slot == (
        "2026-11-08 14:15"
    )


def test_due_warming_uses_shared_store_after_peer_deletes_task(tmp_path, monkeypatch):
    path = str(tmp_path / "tasks.json")
    deleting = WarmingSchedulerService(file_path=path)
    deleted = deleting.add_task("Deleted", ["Profile"], schedule_time="14:15")
    dispatching = WarmingSchedulerService(file_path=path)
    writer = WarmingSchedulerService(file_path=path)
    assert dispatching.get_task(deleted.id) is not None
    added = writer.add_task("Kept", ["Profile"], schedule_time="14:15")
    assert deleting.delete_task(deleted.id)
    assert [t.id for t in WarmingSchedulerService(file_path=path).list_tasks()] == [added.id]
    executed = []
    finished = threading.Event()

    def execute(task, *_args):
        executed.append(task.id)
        finished.set()

    monkeypatch.setattr(dispatching, "_execute_task", execute)
    monkeypatch.setattr(writer, "_execute_task", execute)
    dt = datetime.datetime(2026, 11, 8, 14, 15, 10)

    assert dispatching.check_due_tasks(None, None, now=dt) == []
    assert not executed
    assert writer.check_due_tasks(None, None, now=dt) == [added.id]
    assert finished.wait(5)
    assert executed == [added.id]
    assert writer.check_due_tasks(None, None, now=dt) == []
    assert [t.id for t in WarmingSchedulerService(file_path=path).list_tasks()] == [added.id]


def test_manual_warming_run_of_peer_deleted_task_returns_not_found(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "tasks.json"))
    deleting = WarmingSchedulerService()
    deleted = deleting.add_task("Deleted", ["Profile"])
    container = Container()
    dispatching = container.warming_scheduler
    assert dispatching.get_task(deleted.id) is not None
    assert deleting.delete_task(deleted.id)
    added = deleting.add_task("Peer only", ["Profile"])
    executed = []
    monkeypatch.setattr(dispatching, "_execute_task", lambda task, *_: executed.append(task.id))

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        for task in (deleted, added):
            res = client.post(f"/api/v1/warming/tasks/{task.id}/run")
            assert res.status_code == 404
            assert res.json() == {"detail": "Warming task not found"}
    assert not executed


def test_profile_rename_retargets_saved_warming_task(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    container = Container()
    pm, ws = container.profile_manager, container.warming_scheduler
    for name in ("Before", "Other"):
        assert pm.add_profile(name, "", "linux", engine="chrome")
    task = ws.add_task("Warm", ["Other", "Before"])
    untouched = ws.add_task("Unrelated", ["Other"])

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        assert client.patch("/api/v1/profiles/Before", json={"name": "After"}).status_code == 200
        assert client.get("/api/v1/profiles/Before").status_code == 404
        tasks = client.get("/api/v1/warming/tasks").json()["tasks"]
    assert {t["id"]: t["profile_names"] for t in tasks} == {
        task.id: ["Other", "After"],
        untouched.id: ["Other"],
    }
    reloaded = WarmingSchedulerService()
    assert reloaded.get_task(task.id).profile_names == ["Other", "After"]
    visited = []
    launcher = SimpleNamespace(
        is_running=lambda _: True, control=lambda name, *_args, **_kwargs: visited.append(name)
    )
    assert reloaded.execute_task_sync(task.id, pm, launcher, step_dwell_seconds=0)
    assert set(visited) == {"Other", "After"}


def test_profile_rename_preserves_warming_tasks_written_by_another_scheduler(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    writer = Container()
    assert writer.profile_manager.add_profile("Before", "", "linux", engine="chrome")
    existing = writer.warming_scheduler.add_task("Existing", ["Before"])
    deleted = writer.warming_scheduler.add_task("Deleted", ["Before"])

    renamer = Container()
    ws = renamer.warming_scheduler
    assert ws.get_task(deleted.id) is not None
    added = writer.warming_scheduler.add_task("New target", ["Other", "Before"])
    unrelated = writer.warming_scheduler.add_task("Unrelated", ["Other"])
    assert writer.warming_scheduler.delete_task(deleted.id)
    assert ws.get_task(added.id) is None

    with TestClient(create_app(renamer), base_url="http://127.0.0.1") as client:
        assert client.patch("/api/v1/profiles/Before", json={"name": "After"}).status_code == 200

    expected = {
        existing.id: ["After"],
        added.id: ["Other", "After"],
        unrelated.id: ["Other"],
    }
    assert {
        task.id: task.profile_names for task in WarmingSchedulerService().list_tasks()
    } == expected
    assert {task.id: task.profile_names for task in ws.list_tasks()} == expected

    follow_up = writer.warming_scheduler.add_task("Follow-up", ["Other"])
    assert writer.warming_scheduler.delete_task(unrelated.id)
    del expected[unrelated.id]
    expected[follow_up.id] = ["Other"]
    assert {
        task.id: task.profile_names for task in WarmingSchedulerService().list_tasks()
    } == expected


def test_warming_retarget_keeps_unowned_running_status_on_disk_only(tmp_path):
    path = tmp_path / "warming.json"
    owner = WarmingSchedulerService(file_path=str(path))
    task = owner.add_task("Existing", ["Before"])
    task.status = "running"
    owner._save()

    renamer = WarmingSchedulerService(file_path=str(path))
    assert renamer.get_task(task.id).status == "idle"
    with renamer.retarget_profile("Before", "After"):
        pass

    assert renamer.get_task(task.id).status == "idle"
    saved = json.loads(path.read_text(encoding="utf-8"))[0]
    assert saved["profile_names"] == ["After"]
    assert saved["status"] == "running"


def test_warming_run_save_error_preserves_state_and_allows_retry(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    container = Container()
    ws = container.warming_scheduler
    task = ws.add_task("Warm", ["Missing profile"])
    snapshot = task.to_dict()
    saved = (tmp_path / "warming.json").read_bytes()
    done = threading.Event()
    container.event_bus.subscribe(
        lambda: done.set() if ws.get_task(task.id).status == "failed" else None
    )

    def fail_write():
        raise PermissionError(errno.EACCES, "read-only store")

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        url = f"/api/v1/warming/tasks/{task.id}/run"
        with monkeypatch.context() as patch:
            patch.setattr(ws, "_save", fail_write)
            response = client.post(url)
        assert response.status_code == 500
        assert task.to_dict() == snapshot
        assert task.id not in ws._cancel_flags
        assert (tmp_path / "warming.json").read_bytes() == saved
        assert client.post(url).json()["started"] is True
        assert done.wait(5)
        assert ws.get_task(task.id).last_result == "Failed (0/1 profiles)"


def test_profile_rename_rolls_back_when_warming_targets_cannot_be_saved(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    container = Container()
    pm, ws = container.profile_manager, container.warming_scheduler
    assert pm.add_profile("Before", "", "linux", engine="chrome")
    task = ws.add_task("Warm", ["Before"])
    profiles_file = tmp_path / "profiles.json"
    warming_file = tmp_path / "warming.json"
    old_profiles, old_tasks = profiles_file.read_bytes(), warming_file.read_bytes()

    def fail_write():
        raise PermissionError(errno.EACCES, "read-only store")

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        monkeypatch.setattr(ws, "_save", fail_write)
        assert client.patch("/api/v1/profiles/Before", json={"name": "After"}).status_code == 500
        assert client.get("/api/v1/profiles/Before").status_code == 200
        assert client.get("/api/v1/profiles/After").status_code == 404
    assert profiles_file.read_bytes() == old_profiles
    assert warming_file.read_bytes() == old_tasks
    assert task.profile_names == ["Before"]
    assert (tmp_path / "data" / "Before").is_dir()
    assert not (tmp_path / "data" / "After").exists()


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


@pytest.mark.parametrize("names", [["first", "missing", "second", "first"], [], ["missing"]])
def test_proxy_assign_strict_save_rollback(tmp_path, monkeypatch, names):
    from src.core import config as app_config
    from src.core import container as app_container

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    monkeypatch.setattr(app_config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(app_container, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("src.api.app.API_TOKEN", "")
    monkeypatch.setattr("src.api.auth.API_TOKEN", "")
    container = Container()
    pm = container.profile_manager
    for name in ("first", "second"):
        pm.add_profile(name, "192.0.2.1:8080", "windows", engine="chrome")
    originals = pm.profiles.copy()
    before = {name: p.to_dict() for name, p in pm.profiles.items()}
    with TestClient(
        create_app(container), base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as client:
        client.post("/api/v1/proxy/pool/bulk", json={"text": "198.51.100.99:8080"})
        with monkeypatch.context() as failing:

            def fail_replace(*args):
                raise OSError(errno.ENOSPC, "No space left on device")

            failing.setattr(pm_mod, "_replace", fail_replace)
            response = client.post("/api/v1/proxy/pool/assign", json={"profile_names": names})
        assert {name: p.to_dict() for name, p in pm.profiles.items()} == before
        assert all(pm.profiles[name] is original for name, original in originals.items())
        assert {name: p.to_dict() for name, p in pm_mod.ProfileManager().profiles.items()} == before
        if "first" in names:
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
        else:
            assert response.status_code == 200
            assert response.json() == {"updated": 0}
        response = client.post("/api/v1/proxy/pool/assign", json={"profile_names": names})
        assert response.status_code == 200
        assert response.json() == {"updated": 3 if "first" in names else 0}
        persisted = pm_mod.ProfileManager()
        for name in originals:
            expected = "198.51.100.99:8080" if name in names else "192.0.2.1:8080"
            assert pm.profiles[name].proxy == expected
            assert persisted.profiles[name].proxy == expected


def test_proxy_pool_ui_auto_assign_handles_save_error(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.core.strings import get_string
    from src.services.proxy.service import ProxyService

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("ui-prof", "192.0.2.1:8080", "macos", engine="chrome")
    pool = ProxyPoolService(file_path=str(tmp_path / "proxies.json"))
    pool.bulk_import("198.51.100.99:8080")

    page = _DummyPage()
    logs: list[str] = []
    refreshed: list[bool] = []
    open_proxy_pool_dialog(
        page,
        pool,
        ProxyService(),
        pm,
        target_profile_names=["ui-prof"],
        log_fn=logs.append,
        refresh_fn=lambda: refreshed.append(True),
    )
    assign_label = get_string("proxy_pool_assign_btn", count=1)
    assign_btn = next(
        c for c in _walk_controls(page.dialog) if getattr(c, "content", None) == assign_label
    )
    feedback = next(
        c
        for c in _walk_controls(page.dialog)
        if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )

    def fail_replace(*_args):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(pm_mod, "_replace", fail_replace)
    assign_btn.on_click(None)

    assert feedback.visible is True
    assert feedback.color == COLORS["error"]
    assert "disk full" in feedback.value
    assert pm.profiles["ui-prof"].proxy == "192.0.2.1:8080"
    assert logs and "disk full" in logs[-1]
    assert refreshed == [True]


def _fail_pool_save(monkeypatch):
    import src.services.proxy.pool as pool_mod

    def fail_mkstemp(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", "proxies.json")

    monkeypatch.setattr(pool_mod.tempfile, "mkstemp", fail_mkstemp)


def test_proxy_pool_merges_imports_from_stale_instances(tmp_path):
    path = str(tmp_path / "proxies.json")
    first = ProxyPoolService(path)
    second = ProxyPoolService(path)
    assert first.list_entries() == second.list_entries() == []

    assert first.bulk_import("192.0.2.10:8080|Alpha") == (1, 0)
    assert first.bulk_import("198.51.100.20:8080|Beta") == (1, 0)
    assert second.bulk_import("203.0.113.30:8080|Gamma") == (1, 0)

    for pool in (first, second, ProxyPoolService(path)):
        assert [entry.label for entry in pool.list_entries()] == ["Alpha", "Beta", "Gamma"]


def test_proxy_pool_health_check_merges_changes_while_probe_runs(tmp_path):
    path = str(tmp_path / "proxies.json")
    first = ProxyPoolService(path)
    entry = first.add_proxy("192.0.2.10:8080", "Original")
    second = ProxyPoolService(path)

    class ConcurrentWriter:
        def check_proxy_sync(self, proxy, timeout):
            assert proxy == entry.proxy
            second.add_proxy(proxy, "Relabelled")
            second.add_proxy("198.51.100.20:8080", "New")
            return True, "192.0.2.10"

    first.check_entry(entry.id, ConcurrentWriter())
    assert [(e.label, e.status) for e in ProxyPoolService(path).list_entries()] == [
        ("Relabelled", "working"),
        ("New", "untested"),
    ]


def test_proxy_pool_bulk_import_save_error_keeps_pool(tmp_path, monkeypatch):
    path = tmp_path / "proxies.json"
    pool = ProxyPoolService(file_path=str(path))
    pool.bulk_import("192.0.2.10:8080|Old")
    before = [e.to_dict() for e in pool.list_entries()]
    saved = path.read_text(encoding="utf-8")

    with monkeypatch.context() as failing:
        _fail_pool_save(failing)
        with pytest.raises(PermissionError):
            pool.bulk_import("192.0.2.10:8080|New\n198.51.100.20:3128")

    assert [e.to_dict() for e in pool.list_entries()] == before
    assert path.read_text(encoding="utf-8") == saved
    assert pool.bulk_import("198.51.100.20:3128") == (1, 0)
    assert {e.proxy for e in ProxyPoolService(file_path=str(path)).list_entries()} == {
        "192.0.2.10:8080",
        "198.51.100.20:3128",
    }


def test_proxy_pool_ui_import_handles_save_error(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.core.strings import get_string
    from src.services.proxy.service import ProxyService

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))

    pm = pm_mod.ProfileManager()
    pool = ProxyPoolService(file_path=str(tmp_path / "proxies.json"))
    page = _DummyPage()
    logs: list[str] = []
    refreshed: list[bool] = []
    open_proxy_pool_dialog(
        page,
        pool,
        ProxyService(),
        pm,
        log_fn=logs.append,
        refresh_fn=lambda: refreshed.append(True),
    )
    controls = list(_walk_controls(page.dialog))
    import_label = get_string("proxy_pool_import_btn")
    import_btn = next(c for c in controls if getattr(c, "content", None) == import_label)
    bulk_input = next(c for c in controls if isinstance(c, ft.TextField) and c.multiline)
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )

    bulk_input.value = "198.51.100.20:3128"
    _fail_pool_save(monkeypatch)
    import_btn.on_click(None)

    assert feedback.visible is True
    assert feedback.color == COLORS["error"]
    assert "Permission denied" in feedback.value
    assert logs and "Permission denied" in logs[-1]
    assert bulk_input.value == "198.51.100.20:3128"
    assert pool.list_entries() == []
    assert refreshed == []

    monkeypatch.undo()
    import_btn.on_click(None)

    assert feedback.color == COLORS["success"]
    assert feedback.value == get_string("proxy_pool_imported_msg", added=1, updated=0, invalid=0)
    assert bulk_input.value == ""
    assert [e.proxy for e in pool.list_entries()] == ["198.51.100.20:3128"]
    assert refreshed == [True]


@pytest.mark.parametrize(
    "initial_label,labels,counts",
    [
        (None, ("A", "B"), (1, 0, 1)),
        ("A", ("A",), (0, 0, 1)),
        ("A", ("B", "C"), (0, 1, 1)),
        ("A", ("B", "A"), (0, 0, 1)),
    ],
)
def test_proxy_pool_bulk_counts_distinct_net_changes(tmp_path, initial_label, labels, counts):
    endpoint = "192.0.2.10:8001"
    text = "\n".join([*(f"{endpoint}|{label}" for label in labels), "not a proxy"])
    for name in ("desktop", "api"):
        path = str(tmp_path / f"{name}.json")
        pool = ProxyPoolService(file_path=path)
        if initial_label is not None:
            pool.add_proxy(endpoint, label=initial_label)
        if name == "desktop":
            assert pool.bulk_import_counts(text) == counts
        else:
            assert pool.bulk_import(text) == (len(labels), 1)
        assert [entry.label for entry in ProxyPoolService(file_path=path).list_entries()] == [
            labels[-1]
        ]


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en", "[Proxy Pool] 4 added, 0 updated, 2 invalid lines skipped"),
        ("vi", "[Kho Proxy] Thêm 4 proxy, cập nhật 0 proxy, bỏ qua 2 dòng lỗi"),
    ],
)
def test_proxy_pool_ui_reports_added_and_updated(tmp_path, lang, expected):
    from src.core.strings import current_language, set_language

    pool = ProxyPoolService(file_path=str(tmp_path / "proxies.json"))
    page = _DummyPage()
    original = current_language()
    set_language(lang)
    try:
        open_proxy_pool_dialog(page, pool, None, SimpleNamespace(list_profiles=list))
        controls = list(_walk_controls(page.dialog))
        bulk_input = next(c for c in controls if isinstance(c, ft.TextField) and c.multiline)
        bulk_input.value = (
            "192.0.2.10:8001|First\n192.0.2.11:8002|Second\n"
            "198.51.100.12:8003|Third\n203.0.113.13:8004|Fourth\n"
            "192.0.2.10:8001|Updated first\nnot a proxy\n192.0.2.15:99999"
        )
        import_button = next(
            c
            for c in controls
            if getattr(c, "content", None) == get_string("proxy_pool_import_btn")
        )
        import_button.on_click(None)
        assert len(pool.list_entries()) == 4
        assert pool.list_entries()[0].label == "Updated first"
        assert any(c.visible and c.value == expected for c in controls if isinstance(c, ft.Text))
    finally:
        set_language(original)


def test_proxy_pool_long_text_has_bounded_space_and_masked_tooltips(tmp_path):
    endpoint = "socks5://user:pass@long-test-branch-proxy-endpoint.example.test:1080"
    label = "Nhãn kho proxy cho bộ phận vận hành chi nhánh Nguyễn Thị Minh Khai"
    pool = ProxyPoolService(file_path=str(tmp_path / "proxies.json"))
    pool.add_proxy(endpoint, label=label)
    entry = pool.list_entries()[0]
    page = _DummyPage()
    open_proxy_pool_dialog(page, pool, SimpleNamespace(), SimpleNamespace(list_profiles=list))

    controls = list(_walk_controls(page.dialog))
    endpoint_text = next(c for c in controls if isinstance(c, ft.Text) and c.value == entry.masked)
    label_text = next(c for c in controls if isinstance(c, ft.Text) and c.value == label)
    label_chip = next(c for c in controls if getattr(c, "content", None) is label_text)
    assert label_chip.expand and endpoint_text.expand
    assert label_text.no_wrap and endpoint_text.no_wrap
    assert label_text.overflow == endpoint_text.overflow == ft.TextOverflow.ELLIPSIS
    assert label_chip.tooltip == label
    assert endpoint_text.tooltip == entry.masked and "pass" not in entry.masked


def test_toast_visibility_does_not_change_toolbar_layout():
    from src.ui.app import App

    subtitle = ft.Text("Profiles")
    toast = ft.Container(visible=False)
    content = build_content_area(
        subtitle,
        ft.Column(),
        ft.IconButton(),
        ft.IconButton(),
        ft.Text("1"),
        toast_banner=toast,
    )
    body = next(
        c
        for c in _walk_controls(content)
        if any(ctrl.key == "profiles_toolbar" for ctrl in getattr(c, "controls", []))
    )
    header, spacing, toolbar = body.controls[:3]
    assert spacing.height == 12
    assert toolbar.key == "profiles_toolbar"
    overlay = next(c for c in _walk_controls(header) if isinstance(c, ft.Stack))
    assert overlay.controls[0] is subtitle
    positioned = overlay.controls[1]
    assert positioned.content is toast
    assert positioned.left == positioned.right == 0
    app = App.__new__(App)
    app.refs = SimpleNamespace(content_subtitle=subtitle)
    app._toast_banner = toast
    for visible in (True, False):
        app._set_toast_visible(visible)
        assert toast.visible is visible
        assert subtitle.visible is not visible
        assert body.controls[2] is toolbar


def test_proxy_pool_sidebar_tracks_import_and_delete(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.core.strings import get_string
    from src.ui.app import App

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    app = App()
    app.proxy_pool.add_proxy("198.51.100.10:8080")
    page = _DummyPage()
    page.controls = []
    page.add = page.controls.append
    app.page = page
    app._file_picker = None
    app._build_ui()
    sidebar = page.controls[0].controls[0]
    nav = next(
        c for c in _walk_controls(sidebar) if getattr(c, "on_click", None) == app._open_proxy_pool
    )
    badge = nav.content.controls[-1].content
    assert badge.value == "1"

    nav.on_click(None)
    controls = list(_walk_controls(page.dialog))
    bulk_input = next(c for c in controls if isinstance(c, ft.TextField) and c.multiline)
    import_button = next(
        c for c in controls if getattr(c, "content", None) == get_string("proxy_pool_import_btn")
    )
    bulk_input.value = "192.0.2.10:8080|Short\n192.0.2.20:8080|Other"
    import_button.on_click(None)
    assert app._footer_proxy_text.value == get_string("footer_proxy_pool", count=3)
    assert badge.value == "3"

    delete_button = next(
        c
        for c in _walk_controls(page.dialog)
        if isinstance(c, ft.IconButton) and c.icon == ft.Icons.DELETE_OUTLINE
    )
    delete_button.on_click(None)
    assert app._footer_proxy_text.value == get_string("footer_proxy_pool", count=2)
    assert badge.value == "2"


@pytest.mark.parametrize("operation,initial,expected", [("save", 0, 1), ("delete", 1, 0)])
def test_warming_sidebar_tracks_schedule_changes(
    tmp_path, monkeypatch, operation, initial, expected
):
    from src.core import config
    from src.ui.app import App

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(config, "WARMING_FILE", str(tmp_path / "warming.json"))
    app = App()
    app.pm.add_profile("Scout Chromium", "", "macos", engine="chrome")
    if initial:
        app.warming.add_task("Existing schedule", ["Scout Chromium"], schedule_time="23:53")
    page = _DummyPage()
    page.controls = []
    page.add = page.controls.append
    app.page = page
    app._file_picker = None
    app._build_ui()
    sidebar = page.controls[0].controls[0]
    nav = next(
        c for c in _walk_controls(sidebar) if getattr(c, "on_click", None) == app._open_warming
    )
    badge = nav.content.controls[-1].content
    assert badge.value == str(initial)

    nav.on_click(None)
    controls = list(_walk_controls(page.dialog))
    if operation == "save":
        schedule = next(
            c
            for c in controls
            if isinstance(c, ft.TextField) and c.hint_text == get_string("warming_schedule_hint")
        )
        schedule.value = "23:53"
        button = next(
            c
            for c in controls
            if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
        )
    else:
        button = next(
            c
            for c in controls
            if isinstance(c, ft.IconButton) and c.icon == ft.Icons.DELETE_OUTLINE
        )
    button.on_click(None)
    assert app._footer_warming_text.value == get_string("footer_warming_tasks", count=expected)
    assert badge.value == str(expected)


@pytest.mark.parametrize("sheet_width", [514, 700, 1400])
def test_desktop_selection_uses_the_same_header_height(tmp_path, monkeypatch, sheet_width):
    from src.core import config
    from src.ui.app import App

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(config, "WARMING_FILE", str(tmp_path / "warming.json"))
    app = App()
    for name in ("Atlas", "Lotus"):
        assert app.pm.add_profile(name, "", "macos", engine="chrome")
    page = _DummyPage()
    page.controls = []
    page.add = page.controls.append
    app.page = page
    app._file_picker = None
    app._profile_table_width = sheet_width
    app._build_ui()

    table = next(c for c in _walk_controls(page.controls[0]) if c.key == "profiles_table_container")
    rows = app.refs.profile_list_area
    header_height = app._header_slot.content.height
    checkbox = next(c for c in _walk_controls(rows) if isinstance(c, ft.Checkbox))
    checkbox.on_change(None)
    app._refresh_profiles()

    assert table.content.controls == [app._header_slot, rows]
    assert app._header_slot.content.height == header_height
    assert any(c is app.refs.bulk_bar for c in _walk_controls(app._header_slot))
    assert sum(isinstance(c, ft.Checkbox) for c in _walk_controls(app._header_slot)) == 1

    app._on_search("Lotus")
    bar = app.refs.bulk_bar
    layout_buttons = [c for c in bar.controls if isinstance(c, (ft.Button, ft.OutlinedButton))]
    clear = next(c for c in bar.controls if isinstance(c, ft.TextButton))
    labels = [get_string(key) for key in ("launch", "stop", "delete")]
    icons = [ft.Icons.PLAY_ARROW_ROUNDED, ft.Icons.STOP_ROUNDED, ft.Icons.DELETE_OUTLINE]

    def action_contents():
        return [
            c.content.icon if isinstance(c.content, ft.Icon) else c.content for c in layout_buttons
        ]

    assert [button.tooltip for button in layout_buttons] == labels
    assert action_contents() == (icons if sheet_width == 514 else labels)
    assert clear.content == get_string("clear")
    assert bar.controls[0].controls[1].value == get_string("selected_hidden_count", count=1)
    assert bar.controls[0].controls[1].visible

    sheet = next(c for c in _walk_controls(page.controls[0]) if c.key == "profiles_sheet_container")
    monkeypatch.setattr(ft.context, "disable_auto_update", lambda: None)
    monkeypatch.setattr(ft.Container, "update", lambda _: None)
    for width, expected in ((700, labels), (514, icons)):
        sheet.on_size_change(SimpleNamespace(width=width + 2))
        assert action_contents() == expected
        assert app._header_slot.content.height == header_height
        assert clear in bar.controls

    clear.on_click(None)
    app._refresh_profiles()
    assert table.content.controls == [app._header_slot, rows]
    assert app._header_slot.content.height == header_height
    assert not any(c is app.refs.bulk_bar for c in _walk_controls(app._header_slot))


def test_desktop_bulk_stop_shows_stopping_until_stop_finishes(tmp_path, monkeypatch):
    from src.core import config, strings
    from src.ui.actions import browser
    from src.ui.app import App

    monkeypatch.setattr(strings, "_ACTIVE", strings.STRINGS["vi"])
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(config, "WARMING_FILE", str(tmp_path / "warming.json"))
    app = App()
    name = "Chromium demo"
    assert app.pm.add_profile(name, "", "macos", engine="chrome")

    pending = []
    monkeypatch.setattr(
        browser,
        "threading",
        SimpleNamespace(
            Thread=lambda target, daemon: SimpleNamespace(start=lambda: pending.append(target))
        ),
    )
    running = {name}
    monkeypatch.setattr(app.bl, "is_running", running.__contains__)
    monkeypatch.setattr(app.bl, "running_profile_names", lambda: running.copy())
    monkeypatch.setattr(app.bl, "running_count", lambda: len(running))
    monkeypatch.setattr(app.bl, "stop_profile", running.remove)
    page = _DummyPage()
    page.controls = []
    page.add = page.controls.append
    app.page = page
    app._file_picker = None
    app._build_ui()
    app.state.select_all([name])

    def labels():
        app._refresh_profiles()
        return {
            c.value for c in _walk_controls(app.refs.profile_list_area) if isinstance(c, ft.Text)
        }

    assert "Đang chạy" in labels()
    app.state.failed[name] = "stale failure from an earlier launch"
    app.state.set_loading(name, True)
    app.h.on_bulk_stop()
    stopping_labels = labels()
    assert "Đang dừng" in stopping_labels
    assert "Đang mở" not in stopping_labels
    (stop_button,) = [
        c
        for c in _walk_controls(app.refs.profile_list_area)
        if isinstance(c, ft.Button) and c.icon == ft.Icons.CLOSE_ROUNDED
    ]
    assert stop_button.disabled and stop_button.on_click is None
    assert not any(
        isinstance(c, ft.ProgressRing) for c in _walk_controls(app.refs.profile_list_area)
    )
    (stop,) = pending
    stop()
    assert "Đã dừng" in labels()
    assert name not in app.state.failed
    assert not app.state.is_loading(name)


@pytest.mark.parametrize("from_worker", [False, True])
def test_stop_clears_stopping_after_exception(monkeypatch, from_worker):
    from src.ui.actions import browser
    from src.ui.state import AppState

    name = "Chromium demo"
    state = AppState()
    pm = SimpleNamespace(profiles={name: object()})

    def fail(*_):
        raise RuntimeError("stop failed")

    bl = SimpleNamespace(is_running=lambda _: True, stop_profile=fail)
    pending = []

    def start(target):
        if not from_worker:
            fail()
        pending.append(target)

    monkeypatch.setattr(
        browser,
        "threading",
        SimpleNamespace(Thread=lambda target, daemon: SimpleNamespace(start=lambda: start(target))),
    )
    if from_worker:
        browser.launch_or_stop(name, pm, bl, state, lambda _: None)
        assert state.is_stopping(name)
        assert state.consume_refresh()
        (stop,) = pending
        with pytest.raises(RuntimeError, match="stop failed"):
            stop()
    else:
        with pytest.raises(RuntimeError, match="stop failed"):
            browser.launch_or_stop(name, pm, bl, state, lambda _: None)
        assert not pending
    assert not state.is_stopping(name)
    assert state.consume_refresh()


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
        "demo1@example.test",
        "203.0.113.42:50100:u:p",
        "macos",
        group="Google / Gmail",
        notes="Demo team demo2@example.test",
    )
    container.proxy_pool.add_proxy("203.0.113.42:50100:u:p", label="US-Demo")
    container.warming_scheduler.add_task(
        name="Warm Gmail",
        profile_names=["demo1@example.test"],
        preset="youtube_trust",
        schedule_time="08:00",
    )

    app = App(container)

    class FullDummyPage:
        def __init__(self):
            self.title = ""
            self.window = ft.Window()
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
        prof = app.pm.profiles["demo1@example.test"]
        prof.account_uid = "demo1@example.test"
        prof.account_password = "secret"
        prof.two_fa_secret = "JBSWY3DPEHPK3PXP"
        app._on_copy_user("demo1@example.test")
        assert ("demo1@example.test", "copy_user") in app._feedback_until
        assert app._toast_banner.visible is True
        assert "{name}" not in app._toast_text.value
        assert prof.account_uid in app._toast_text.value
        app._on_copy_pass("demo1@example.test")
        assert ("demo1@example.test", "copy_pass") in app._feedback_until
        app._on_copy_2fa("demo1@example.test")
        assert ("demo1@example.test", "copy_2fa") in app._feedback_until
        app._log(get_string("updated_profile", old="demo1@example.test", new="demo1@example.test"))
        assert ("demo1@example.test", "edit_profile") in app._feedback_until

        # Open Proxy Pool & Warming Dialogs from App
        app._open_proxy_pool()
        assert page.dialog is not None
        page.pop_dialog()

        app._open_warming()
        assert page.dialog is not None
        page.pop_dialog()
        assert app._refresh_warming_dialog is not None
        monkeypatch.setattr(app.warming, "start_daemon", lambda *args, **kwargs: None)
        app._main(FullDummyPage())
        assert app._refresh_warming_dialog is None
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
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
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

    def counted_save(**kwargs):
        nonlocal save_calls
        save_calls += 1
        orig_save(**kwargs)

    monkeypatch.setattr(pm, "save_profiles", counted_save)
    assert pool.assign_round_robin(["shared-prof", "uses-10"], pm) == 2
    assert save_calls == 1


@pytest.mark.parametrize("failure_site", ["mkstemp", "replace"])
def test_warming_add_task_rolls_back_save_error(tmp_path, monkeypatch, failure_site):
    from src.services.warming import scheduler

    store = tmp_path / "warming.json"
    ws = WarmingSchedulerService(file_path=str(store))
    existing = ws.add_task("Existing", ["profile"])
    saved = store.read_bytes()

    def fail_write(*args, **kwargs):
        raise PermissionError(errno.EACCES, "read-only store")

    target = scheduler.tempfile if failure_site == "mkstemp" else scheduler.os
    with monkeypatch.context() as patch:
        patch.setattr(target, failure_site, fail_write)
        with pytest.raises(PermissionError, match="read-only store"):
            ws.add_task("Unsaved", ["profile"])

    assert ws.list_tasks() == [existing]
    assert store.read_bytes() == saved
    assert not list(tmp_path.glob(".warming_*.tmp"))
    task = ws.add_task("Retry", ["profile"])
    reloaded = WarmingSchedulerService(file_path=str(store))
    assert reloaded.get_task(task.id).name == "Retry"


@pytest.mark.parametrize("operation", ["delete_task", "stop_task"])
@pytest.mark.parametrize("failure_site", ["mkstemp", "replace"])
def test_warming_mutation_rolls_back_save_error(tmp_path, monkeypatch, operation, failure_site):
    from src.services.warming import scheduler

    store = tmp_path / "warming.json"
    ws = WarmingSchedulerService(file_path=str(store))
    first = ws.add_task("First", ["profile"])
    task = ws.add_task("Running", ["profile"])
    ws.add_task("Last", ["profile"])
    task.status = "running"
    ws._save()
    cancel = threading.Event()
    ws._cancel_flags[task.id] = cancel
    before = [t.to_dict() for t in ws.list_tasks()]
    saved = store.read_bytes()

    def fail_write(*args, **kwargs):
        raise PermissionError(errno.EACCES, "read-only store")

    target = scheduler.tempfile if failure_site == "mkstemp" else scheduler.os
    with monkeypatch.context() as patch:
        patch.setattr(target, failure_site, fail_write)
        with pytest.raises(PermissionError, match="read-only store"):
            getattr(ws, operation)(task.id)

    assert [t.to_dict() for t in ws.list_tasks()] == before
    assert ws.get_task(task.id) is task
    assert ws._cancel_flags[task.id] is cancel
    assert not cancel.is_set()
    assert store.read_bytes() == saved
    assert not list(tmp_path.glob(".warming_*.tmp"))
    assert getattr(ws, operation)(task.id)
    assert cancel.is_set()
    assert ws.get_task(first.id) is first
    persisted = json.loads(store.read_text())
    if operation == "delete_task":
        assert task.id not in ws._cancel_flags
        assert task.id not in [t["id"] for t in persisted]
    else:
        assert task.status == "idle"
        assert next(t for t in persisted if t["id"] == task.id)["status"] == "idle"
    assert getattr(ws, operation)("missing") is False


@pytest.mark.parametrize("operation", ["delete_task", "stop_task"])
@pytest.mark.parametrize("outcome", ["error", "success"])
def test_warming_ui_mutation_save_feedback(tmp_path, monkeypatch, operation, outcome):
    from src.core.strings import get_string
    from src.services.warming import scheduler

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    task = ws.add_task("Running", ["profile"])
    task.status = "running"
    ws._save()
    page = _DummyPage()
    logs, refreshed = [], []
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: []),
        SimpleNamespace(),
        log_fn=logs.append,
        refresh_fn=lambda: refreshed.append(True),
    )
    controls = list(_walk_controls(page.dialog))
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    tooltip = get_string("delete" if operation == "delete_task" else "stop")
    button = next(c for c in controls if isinstance(c, ft.IconButton) and c.tooltip == tooltip)
    if outcome == "error":

        def fail_write(*args, **kwargs):
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(scheduler.os, "replace", fail_write)
    button.on_click(None)
    if outcome == "error":
        assert feedback.visible
        assert feedback.color == COLORS["error"]
        assert "disk full" in feedback.value
        assert logs == [feedback.value]
        assert refreshed == []
        assert ws.list_tasks() == [task]
        assert task.status == "running"
    else:
        assert not feedback.visible
        assert logs == []
        assert refreshed == ([True] if operation == "delete_task" else [])
        assert (ws.get_task(task.id) is None) == (operation == "delete_task")


def test_open_warming_dialog_follows_background_status(tmp_path, monkeypatch):
    from src.ui.app import App
    from src.ui.state import AppState

    app = App.__new__(App)
    app.state = AppState()
    app.pm = SimpleNamespace(list_profiles=lambda: [])
    app.bl = SimpleNamespace(running_profile_names=set)
    app.warming = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    app._feedback_until = {}
    app._toast_banner = ft.Container(visible=False)
    app._refresh_profiles = lambda: None
    app._refresh_profiles_and_extensions = lambda: None
    app._log = lambda _: None
    page = _DummyPage()
    app.page = page
    task = app.warming.add_task("Scheduled", ["profile"], schedule_time="08:00")
    app._open_warming()
    page.dialog.open = True
    dialog = page.dialog

    def task_action():
        return next(
            c.icon
            for c in _walk_controls(dialog)
            if isinstance(c, ft.IconButton)
            and c.icon in (ft.Icons.PLAY_ARROW_ROUNDED, ft.Icons.STOP_ROUNDED)
        )

    async def run_once(_):
        app.page = None

    monkeypatch.setattr("src.ui.app.asyncio.sleep", run_once)
    assert task_action() == ft.Icons.PLAY_ARROW_ROUNDED
    for status, icon in (
        ("running", ft.Icons.STOP_ROUNDED),
        ("completed", ft.Icons.PLAY_ARROW_ROUNDED),
    ):
        task.status = status
        app.page = page
        app.state.schedule_refresh()
        asyncio.run(app._ui_reconcile_loop())
        assert page.dialog is dialog
        assert task_action() == icon

    app.page = page
    dialog.actions[0].content.on_click(None)
    assert page.closed
    assert app._refresh_warming_dialog is None


@pytest.mark.parametrize("run_immediately", [False, True])
def test_warming_ui_displays_start_save_error(tmp_path, monkeypatch, run_immediately):
    from src.core.strings import get_string

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    if not run_immediately:
        ws.add_task("Saved task", ["profile"])

    def fail_start(*args, **kwargs):
        raise PermissionError(errno.EACCES, "read-only store")

    monkeypatch.setattr(ws, "run_task_async", fail_start)
    page = _DummyPage()
    logs = []
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="profile")]),
        SimpleNamespace(),
        log_fn=logs.append,
    )
    controls = list(_walk_controls(page.dialog))
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    if run_immediately:
        button = next(
            c for c in controls if getattr(c, "content", None) == get_string("warming_run_now_btn")
        )
    else:
        button = next(
            c
            for c in controls
            if isinstance(c, ft.IconButton) and c.tooltip == get_string("launch")
        )
    button.on_click(None)
    assert feedback.visible
    assert feedback.color == COLORS["error"]
    assert feedback.value == get_string("warming_start_save_error")
    assert logs == [feedback.value]
    assert [task.status for task in ws.list_tasks()] == ["idle"]


@pytest.mark.parametrize("run_immediately", [False, True])
@pytest.mark.parametrize("outcome", ["error", "success", "invalid"])
def test_warming_ui_create_save_feedback(tmp_path, monkeypatch, run_immediately, outcome):
    from src.core.strings import get_string
    from src.services.warming import scheduler

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    page = _DummyPage()
    logs, runs, refreshed = [], [], []
    pm = SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="profile")])
    monkeypatch.setattr(ws, "run_task_async", lambda tid, *a, **kw: runs.append(tid) or True)
    open_warming_dialog(
        page,
        ws,
        pm,
        SimpleNamespace(),
        log_fn=logs.append,
        refresh_fn=lambda: refreshed.append(True),
    )
    controls = list(_walk_controls(page.dialog))
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    key = "warming_run_now_btn" if run_immediately else "warming_save_schedule_btn"
    button = next(c for c in controls if getattr(c, "content", None) == get_string(key))
    if outcome == "error":

        def fail_write(*args, **kwargs):
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(scheduler.os, "replace", fail_write)
    elif outcome == "invalid":
        next(c for c in controls if isinstance(c, ft.TextField)).value = ""

    button.on_click(None)

    assert feedback.visible is True
    if outcome == "success":
        assert feedback.color == COLORS["success"]
        assert len(ws.list_tasks()) == 1
        assert bool(runs) == run_immediately
        assert logs == [feedback.value]
        assert refreshed == [True]
    else:
        assert feedback.color == COLORS["error"]
        assert ws.list_tasks() == []
        assert runs == []
        assert refreshed == []
        if outcome == "error":
            assert "disk full" in feedback.value
            assert logs == [feedback.value]
        else:
            assert feedback.value == get_string("warming_invalid_msg")
            assert logs == []


def test_warming_script_actions_match_page_controller_signatures():
    import inspect

    from src.services.browser.control import PageController
    from src.services.warming.scheduler import WARMING_SCRIPT_ACTIONS, normalize_warming_steps

    assert WARMING_SCRIPT_ACTIONS - {"wait"} <= set(PageController.ACTIONS)
    sample = normalize_warming_steps(
        [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "back"},
            {"action": "scroll", "delta_y": 250, "delta_x": 10, "steps": 4, "humanize": True},
            {"action": "click", "selector": "button.ok", "timeout": 5000},
            {"action": "click_at", "x": 120, "y": 240, "humanize": True},
            {"action": "type", "selector": "input[name=q]", "text": "hello", "submit": True},
            {"action": "keyboard_type", "text": "world", "humanize": False},
            {"action": "press", "key": "Enter", "selector": "input[name=q]"},
            {"action": "wait_for", "selector": "#ready", "timeout": 8000},
            {"action": "tab_new", "url": "https://example.org"},
            {"action": "tab_select", "index": 0},
            {"action": "tab_close", "index": 1},
            {"action": "wait", "seconds": 1},
        ]
    )
    for step in sample:
        action = step["action"]
        if action == "wait":
            continue
        sig = inspect.signature(getattr(PageController, action))
        param_keys = {k for k in step if k not in ("action", "optional")}
        assert param_keys <= set(sig.parameters), f"Mismatch for {action}: {param_keys}"


def test_warming_script_validation_and_store_fail_closed(tmp_path):
    from src.services.warming.scheduler import MAX_SCRIPT_JSON_CHARS, normalize_warming_steps

    # Allowed actions normalize cleanly and preserve typed whitespace
    normalized = normalize_warming_steps(
        [
            {"action": "navigate", "url": "example.com", "optional": False},
            {"action": "type", "selector": "input", "text": "  keep spaces  "},
            {"action": "press", "key": "Enter", "selector": None},
            {"action": "tab_new", "url": None},
            {"action": "wait", "min_s": 1, "max_s": 3},
        ]
    )
    assert normalized[0] == {
        "action": "navigate",
        "url": "https://example.com",
        "wait_until": "domcontentloaded",
    }
    assert normalized[1]["text"] == "  keep spaces  "
    assert "selector" not in normalized[2]
    assert "url" not in normalized[3]

    invalid_payloads = [
        "",
        "[]",
        "{not json",
        "x" * (MAX_SCRIPT_JSON_CHARS + 1),
        [{"action": "evaluate", "script": "document.cookie"}],
        [{"action": "screenshot"}],
        [{"action": "snapshot"}],
        [{"action": "text"}],
        [{"action": "tabs"}],
        [{"action": "navigate", "url": "file:///etc/passwd"}],
        [{"action": "navigate", "url": "%66ile:///etc/passwd"}],
        [{"action": "navigate", "url": "javascript:alert(1)"}],
        [{"action": "navigate", "url": "about:blank"}],
        [{"action": "navigate", "url": "chrome://settings"}],
        [{"action": "tab_new", "url": "file:///etc/passwd"}],
        [{"action": "navigate", "url": "https://example.com", "unknown": 1}],
        [{"action": "wait", "seconds": 1}],
        [{"action": "wait", "min_s": 5, "max_s": 2}],
        [{"action": "wait", "seconds": 1, "min_s": 1, "max_s": 2}],
        [{"action": "wait", "seconds": True}],
        [{"action": "wait", "seconds": float("nan")}],
        [{"action": "wait", "seconds": float("inf")}],
        [{"action": "click", "selector": "a", "timeout": 25_001}],
        [{"action": "click", "selector": "a", "timeout": True}],
        [{"action": "tab_select", "index": True}],
        [{"action": "back"}] * 101,
    ]
    for bad in invalid_payloads:
        with pytest.raises(ValueError):
            normalize_warming_steps(bad)

    store = tmp_path / "warming.json"
    ws = WarmingSchedulerService(file_path=str(store))
    assert (
        ws.add_task("Mixed", ["p1"], preset="youtube_trust", script_steps=[{"action": "back"}])
        is None
    )
    assert (
        ws.add_task(
            "BadScript",
            ["p1"],
            preset="script",
            script_steps=[{"action": "evaluate", "script": "1"}],
        )
        is None
    )

    # Corrupt/tampered store on disk fails closed
    store.write_text(
        json.dumps(
            [
                {
                    "id": "tampered",
                    "name": "Tampered",
                    "profile_names": ["p1"],
                    "preset": "script",
                    "script_steps": [{"action": "evaluate", "script": "alert(1)"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    reloaded = WarmingSchedulerService(file_path=str(store))
    assert reloaded.list_tasks() == []

    # Mixed non-script preset with script_steps on disk also fails closed
    store.write_text(
        json.dumps(
            [
                {
                    "id": "mixed",
                    "name": "Mixed",
                    "profile_names": ["p1"],
                    "preset": "youtube_trust",
                    "script_steps": [{"action": "back"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert WarmingSchedulerService(file_path=str(store)).list_tasks() == []


def test_warming_script_execution_cancellation_budget_and_persistence(tmp_path, monkeypatch):
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    store = tmp_path / "warming.json"
    ws = WarmingSchedulerService(file_path=str(store))
    pm = pm_mod.ProfileManager(warming_scheduler=ws)
    pm.add_profile("warm-launch", "", "macos")
    pm.add_profile("warm-open", "", "macos")
    secret_payload = "SuperSecretToken_9988"
    task = ws.add_task(
        name="Script Flow",
        profile_names=["warm-launch", "warm-open"],
        preset="script",
        script_steps=[
            {"action": "navigate", "url": "https://example.com"},
            {"action": "wait", "min_s": 2, "max_s": 4},
            {"action": "click", "selector": "#missing-cookie-banner", "optional": True},
            {"action": "type", "selector": "input[name=q]", "text": secret_payload, "submit": True},
            {"action": "scroll", "delta_y": 500, "humanize": True},
        ],
    )
    assert task is not None and task.resolved_urls() == []

    # Persistence + profile rename retargeting preserves script_steps
    assert pm.patch_profile("warm-open", name="warm-open-renamed")
    reloaded = WarmingSchedulerService(file_path=str(store))
    reloaded_task = reloaded.get_task(task.id)
    assert reloaded_task is not None
    assert reloaded_task.profile_names == ["warm-launch", "warm-open-renamed"]
    assert reloaded_task.script_steps == task.script_steps

    calls = []
    stopped = []

    class ScriptLauncher:
        def __init__(self):
            self.running = {"warm-open-renamed"}

        def is_running(self, name):
            return name in self.running

        def start_thread(self, prof, log):
            self.running.add(prof.name)
            return True

        def wait_for_launch(self, name, timeout):
            return True, None

        def control(self, name, action, params=None, timeout=60):
            if action == "click" and params.get("selector") == "#missing-cookie-banner":
                raise RuntimeError("Element not found")
            calls.append((name, action, params))
            return {"ok": True}

        def stop_profile(self, name, timeout=2):
            self.running.discard(name)
            stopped.append(name)
            return True

    logs = []
    bl = ScriptLauncher()
    assert reloaded.execute_task_sync(task.id, pm, bl, log_fn=logs.append, step_dwell_seconds=0.0)
    assert reloaded.get_task(task.id).status == "completed"
    assert stopped == ["warm-launch"]
    assert "warm-open-renamed" in bl.running
    assert [c[1] for c in calls] == [
        "navigate",
        "type",
        "scroll",
        "navigate",
        "type",
        "scroll",
    ]
    assert all(secret_payload not in line for line in logs)

    # Required step failure aborts current profile, releases lease, and still warms next profile
    fail_task = ws.add_task(
        name="Required Fail",
        profile_names=["warm-launch", "warm-open-renamed"],
        preset="script",
        script_steps=[
            {"action": "click", "selector": "#must-exist"},
            {"action": "scroll", "delta_y": 100},
        ],
    )

    class FailFirstLauncher(ScriptLauncher):
        def control(self, name, action, params=None, timeout=60):
            if name == "warm-launch" and action == "click":
                raise RuntimeError("boom")
            calls.append((name, action, params))
            return {"ok": True}

    calls.clear()
    fail_bl = FailFirstLauncher()
    assert ws.execute_task_sync(fail_task.id, pm, fail_bl, step_dwell_seconds=0.0)
    assert fail_task.last_result == "OK (1/2 profiles)"
    assert ws._profile_active_users == {}
    assert [c[0] for c in calls] == ["warm-open-renamed", "warm-open-renamed"]

    # stop_task cancels a long wait immediately (< 1s) without executing subsequent steps
    wait_entered = threading.Event()
    long_task = ws.add_task(
        name="Long Wait",
        profile_names=["warm-open-renamed"],
        preset="script",
        script_steps=[
            {"action": "navigate", "url": "https://example.com"},
            {"action": "wait", "seconds": 60},
            {"action": "scroll", "delta_y": 200},
        ],
    )

    class LongWaitLauncher(ScriptLauncher):
        def control(self, name, action, params=None, timeout=60):
            calls.append((name, action, params))
            if action == "navigate":
                wait_entered.set()
            return {}

    calls.clear()
    assert ws.run_task_async(long_task.id, pm, LongWaitLauncher())
    assert wait_entered.wait(3)
    t0 = sched_mod.time.monotonic()
    assert ws.stop_task(long_task.id)
    assert sched_mod.time.monotonic() - t0 < 1.5
    assert [c[1] for c in calls] == ["navigate"]

    # #5: duration_minutes budget caps wait and flags timed-out incomplete scripts as failed
    budget_task = ws.add_task(
        name="Budget Cap",
        profile_names=["warm-open-renamed"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "back"},
            {"action": "wait", "seconds": 90},
            {"action": "click", "selector": "#must-run"},
        ],
    )
    waits = []
    fake_clock = [100.0]

    class FakeEvent:
        def is_set(self):
            return False

        def wait(self, sec):
            waits.append(sec)
            fake_clock[0] += sec
            return False

    orig_threading = sched_mod.threading
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_clock[0])
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
    calls.clear()
    assert ws.execute_task_sync(budget_task.id, pm, ScriptLauncher()) is False
    monkeypatch.setattr(sched_mod, "threading", orig_threading)
    assert waits == [60.0]
    assert [c[1] for c in calls] == ["back"]
    assert budget_task.status == "failed"
    assert budget_task.last_result == "Timed out (0/1 profiles)"

    # Trailing wait truncated by budget also ends Timed out rather than OK
    trailing_wait_task = ws.add_task(
        name="Trailing Wait Truncated",
        profile_names=["warm-open-renamed"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "navigate", "url": "https://example.com"},
            {"action": "wait", "seconds": 90},
        ],
    )
    fake_clock[0] = 200.0
    waits.clear()
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_clock[0])
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
    assert ws.execute_task_sync(trailing_wait_task.id, pm, ScriptLauncher()) is False
    monkeypatch.setattr(sched_mod, "threading", orig_threading)
    assert trailing_wait_task.status == "failed"
    assert trailing_wait_task.last_result == "Timed out (0/1 profiles)"

    # Remaining 1-second budget clamps step control timeout and maps 504 -> Timed out
    one_sec_task = ws.add_task(
        name="One Second Left",
        profile_names=["warm-open-renamed"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "wait", "seconds": 59},
            {"action": "navigate", "url": "https://example.com"},
        ],
    )
    observed_timeouts = []

    class ClampTimeoutLauncher(ScriptLauncher):
        def control(self, name, action, params=None, timeout=60):
            from src.services.browser.launcher import BrowserControlError

            observed_timeouts.append(timeout)
            fake_clock[0] += 1.0
            raise BrowserControlError("timed out", status=504)

    fake_clock[0] = 300.0
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_clock[0])
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
    assert ws.execute_task_sync(one_sec_task.id, pm, ClampTimeoutLauncher()) is False
    monkeypatch.setattr(sched_mod, "threading", orig_threading)
    assert observed_timeouts == [1.0]
    assert one_sec_task.status == "failed"
    assert one_sec_task.last_result == "Timed out (0/1 profiles)"


def test_warming_script_api_and_ui_flow(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("script-prof", "", "macos")
    monkeypatch.setattr(
        container.browser_launcher,
        "is_running",
        lambda name: name == "script-prof",
    )
    api_calls = []
    monkeypatch.setattr(
        container.browser_launcher,
        "control",
        lambda name, action, params=None, timeout=60: (
            api_calls.append((name, action, params)) or {}
        ),
    )

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        bad_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Bad Script",
                "profile_names": ["script-prof"],
                "preset": "script",
                "script_steps": [{"action": "evaluate", "script": "1"}],
            },
        )
        assert bad_res.status_code == 400
        assert "Invalid warming script" in bad_res.json()["detail"]

        mixed_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Mixed",
                "profile_names": ["script-prof"],
                "preset": "youtube_trust",
                "script_steps": [{"action": "back"}],
            },
        )
        assert mixed_res.status_code == 400

        ok_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Good Script",
                "profile_names": ["script-prof"],
                "preset": "script",
                "script_steps": [
                    {"action": "navigate", "url": "https://example.com"},
                    {"action": "scroll", "delta_y": 300},
                ],
            },
        )
        assert ok_res.status_code == 201
        created = ok_res.json()
        assert created["preset"] == "script"
        assert len(created["script_steps"]) == 2

        list_res = client.get("/api/v1/warming/tasks").json()
        assert "script" in list_res["presets"]
        assert list_res["tasks"][0]["script_steps"] == created["script_steps"]

        done = threading.Event()
        container.event_bus.subscribe(
            lambda: (
                done.set()
                if container.warming_scheduler.get_task(created["id"]).status == "completed"
                else None
            )
        )
        run_res = client.post(f"/api/v1/warming/tasks/{created['id']}/run")
        assert run_res.status_code == 200 and run_res.json()["started"] is True
        assert done.wait(5)
        assert [c[1] for c in api_calls] == ["navigate", "scroll"]

    # Desktop UI dialog: switch preset -> insert sample -> invalid error -> valid save
    ws = WarmingSchedulerService(file_path=str(tmp_path / "ui_warming.json"))
    page = _DummyPage()
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="script-prof")]),
        SimpleNamespace(),
    )
    controls = list(_walk_controls(page.dialog))
    dropdown = next(c for c in controls if isinstance(c, ft.Dropdown))
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    sample_btn = next(
        c for c in controls if isinstance(c, ft.TextButton) and c.icon == ft.Icons.CODE_ROUNDED
    )
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )

    dropdown.value = "script"
    dropdown.on_select(None)
    assert sample_btn.visible is True

    payload_field.value = '[{"action": "evaluate", "script": "1"}]'
    save_btn.on_click(None)
    assert feedback.visible is True and feedback.color == COLORS["error"]
    assert ws.list_tasks() == []

    sample_btn.on_click(None)
    replace_btn = next(
        c
        for c in _walk_controls(page.dialog)
        if getattr(c, "content", None) == get_string("warming_replace_draft_btn")
    )
    replace_btn.on_click(None)
    save_btn.on_click(None)
    assert feedback.color == COLORS["success"]
    assert len(ws.list_tasks()) == 1
    assert ws.list_tasks()[0].preset == "script"
    assert any(
        isinstance(c, ft.Text) and "script (4 steps)" in (c.value or "")
        for c in _walk_controls(page.dialog)
    )


def test_codex_f1_script_profile_exclusivity_barriers(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("prof-a", "", "macos")
    pm.add_profile("prof-b", "", "macos")

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    s1 = ws.add_task(
        "Script1",
        ["prof-a"],
        preset="script",
        script_steps=[
            {"action": "navigate", "url": "https://a.test"},
            {"action": "type", "selector": "input", "text": "A draft"},
        ],
    )
    s2 = ws.add_task(
        "Script2",
        ["prof-a"],
        preset="script",
        script_steps=[{"action": "navigate", "url": "https://b.test"}],
    )
    s_other = ws.add_task(
        "ScriptOther",
        ["prof-b"],
        preset="script",
        script_steps=[{"action": "navigate", "url": "https://other.test"}],
    )
    url_task = ws.add_task(
        "URLTask",
        ["prof-a"],
        preset="custom",
        custom_urls=["https://url.test"],
    )

    entered = threading.Event()
    release = threading.Event()
    events: list[tuple[str, str]] = []

    class BarrierLauncher:
        def __init__(self):
            self.running = {"prof-a", "prof-b"}

        def is_running(self, name):
            return name in self.running

        def control(self, name, action, params=None, timeout=60):
            events.append((name, action))
            if name == "prof-a" and action == "navigate" and params.get("url") == "https://a.test":
                entered.set()
                release.wait(3)
            return {}

    bl = BarrierLauncher()
    t1 = threading.Thread(
        target=ws.execute_task_sync,
        args=(s1.id, pm, bl),
        kwargs={"step_dwell_seconds": 0.0},
        daemon=True,
    )
    t1.start()
    assert entered.wait(3)

    # Same-profile Script2 and URLTask are rejected as Busy while Script1 is active
    assert ws.execute_task_sync(s2.id, pm, bl, step_dwell_seconds=0.0) is False
    assert s2.status == "failed" and s2.last_result == "Busy (0/1 profiles)"
    assert ws.execute_task_sync(url_task.id, pm, bl, step_dwell_seconds=0.0) is False
    assert url_task.status == "failed" and url_task.last_result == "Busy (0/1 profiles)"

    # Different profile runs concurrently without blocking
    assert ws.execute_task_sync(s_other.id, pm, bl, step_dwell_seconds=0.0) is True
    assert s_other.status == "completed"

    release.set()
    t1.join(3)
    assert s1.status == "completed"
    assert ("prof-a", "type") in events
    assert ws._profile_active_users == {}
    assert ws._profile_script_owners == {}

    # Reverse order: URL preset active on prof-a -> script task on prof-a is rejected as Busy
    url_entered = threading.Event()
    url_release = threading.Event()

    class UrlBarrierLauncher(BarrierLauncher):
        def control(self, name, action, params=None, timeout=60):
            if action == "navigate" and params.get("url") == "https://url.test":
                url_entered.set()
                url_release.wait(3)
            return {}

    u_bl = UrlBarrierLauncher()
    t_url = threading.Thread(
        target=ws.execute_task_sync,
        args=(url_task.id, pm, u_bl),
        kwargs={"step_dwell_seconds": 0.0},
        daemon=True,
    )
    t_url.start()
    assert url_entered.wait(3)
    assert ws.execute_task_sync(s2.id, pm, u_bl, step_dwell_seconds=0.0) is False
    assert s2.status == "failed" and s2.last_result == "Busy (0/1 profiles)"
    url_release.set()
    t_url.join(3)
    assert url_task.status == "completed"
    assert ws._profile_active_users == {}
    assert ws._profile_script_owners == {}


def test_codex_f2_f3_loopback_timeout_and_error_log_redaction(tmp_path, monkeypatch):
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("loop-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class StubPage:
        def __init__(self):
            self.url = "https://initial.test/"
            self.effects: list[str] = []
            self.raise_token_err = False

        def is_closed(self):
            return False

        async def title(self):
            return "Stub"

        async def goto(self, url, wait_until="load"):
            self.effects.append("navigate-started")
            await asyncio.sleep(2.0)
            self.effects.append("navigate-late-effect")

        async def go_back(self):
            self.effects.append("back-effect")

        class _Loc:
            def __init__(self, first):
                self.first = first

        class _First:
            def __init__(self, page):
                self._page = page

            async def click(self, timeout=10_000):
                if self._page.raise_token_err:
                    raise RuntimeError(
                        "Failed on https://secret.test/?token=TOK_123 selector=#secret-sel text=SECRET_DRAFT"
                    )

        def locator(self, _selector):
            return self._Loc(self._First(self))

    page = StubPage()
    ctx = SimpleNamespace(pages=[page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["loop-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["loop-prof"] = (info["port"], info["token"])
    stopped_called = []
    monkeypatch.setattr(bl, "stop_profile", lambda name, **_: stopped_called.append(name) or True)

    try:
        # #2: Optional navigate times out -> back never runs, late effect never applies, task Interrupted
        t_timeout = ws.add_task(
            "Loop Timeout",
            ["loop-prof"],
            preset="script",
            script_steps=[
                {"action": "navigate", "url": "https://slow.test", "optional": True},
                {"action": "back"},
            ],
        )
        orig_control = bl.control
        monkeypatch.setattr(
            bl,
            "control",
            lambda name, action, params=None, timeout=60: orig_control(
                name, action, params, timeout=0.2 if action == "navigate" else timeout
            ),
        )
        logs: list[str] = []
        assert ws.execute_task_sync(t_timeout.id, pm, bl, log_fn=logs.append) is False
        assert t_timeout.status == "failed"
        assert t_timeout.last_result == "Interrupted (0/1 profiles)"
        assert page.effects == ["navigate-started"]
        assert stopped_called == []
        assert bl.is_running("loop-prof")

        # #3: HTTP 422 error carrying URL token, selector, and draft text never leaks into logs
        monkeypatch.setattr(bl, "control", orig_control)
        page.raise_token_err = True
        t_err = ws.add_task(
            "Err Redact",
            ["loop-prof"],
            preset="script",
            script_steps=[
                {"action": "click", "selector": "#secret-sel", "optional": True},
                {"action": "click", "selector": "#secret-sel"},
            ],
        )
        err_logs: list[str] = []
        assert ws.execute_task_sync(t_err.id, pm, bl, log_fn=err_logs.append) is False
        joined = "\n".join(err_logs)
        assert "TOK_123" not in joined
        assert "https://secret.test" not in joined
        assert "#secret-sel" not in joined
        assert "SECRET_DRAFT" not in joined
        assert "HTTP 422" in joined
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


@pytest.mark.parametrize("bad_steps", ["__MISSING__", None, []])
def test_codex_f4_corrupt_script_row_fails_closed_on_save(tmp_path, bad_steps):
    store = tmp_path / "warming.json"
    bad_row = {
        "id": "bad_script",
        "name": "Broken Script",
        "profile_names": ["p1"],
        "preset": "script",
    }
    if bad_steps != "__MISSING__":
        bad_row["script_steps"] = bad_steps
    legacy_row = {
        "id": "legacy_ok",
        "name": "Legacy",
        "profile_names": ["p1"],
        "preset": "youtube_trust",
    }
    store.write_text(json.dumps([bad_row, legacy_row]), encoding="utf-8")
    raw_before = store.read_bytes()

    ws = WarmingSchedulerService(file_path=str(store))
    assert ws.list_tasks() == []
    with pytest.raises(OSError, match="Invalid warming task store"):
        ws.add_task("Unrelated", ["p1"], preset="youtube_trust")
    assert store.read_bytes() == raw_before


def test_codex_f6_f7_validation_parity_and_ui_preset_draft_isolation(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.warming.scheduler import MAX_SCRIPT_JSON_CHARS

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("p1", "", "macos")
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        # Huge integer in wait seconds returns 400 (not 500 OverflowError)
        huge_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Huge Int",
                "profile_names": ["p1"],
                "preset": "script",
                "script_steps": [{"action": "back"}, {"action": "wait", "seconds": 10**400}],
            },
        )
        assert huge_res.status_code == 400

        # Oversized URL (65,024 chars) and oversized list payload return 400 (not 201)
        long_url_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Long URL",
                "profile_names": ["p1"],
                "preset": "script",
                "script_steps": [{"action": "navigate", "url": "https://a.test/" + "x" * 65_000}],
            },
        )
        assert long_url_res.status_code == 400

        big_steps = [{"action": "type", "selector": "#q", "text": "a" * 1800} for _ in range(40)]
        assert len(json.dumps(big_steps)) > MAX_SCRIPT_JSON_CHARS
        big_list_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Big List",
                "profile_names": ["p1"],
                "preset": "script",
                "script_steps": big_steps,
            },
        )
        assert big_list_res.status_code == 400

    # #6 (deeply nested JSON) + #7 (preset switching isolates script vs URL drafts)
    ws = WarmingSchedulerService(file_path=str(tmp_path / "ui_drafts.json"))
    page = _DummyPage()
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="p1")]),
        SimpleNamespace(),
    )
    controls = list(_walk_controls(page.dialog))
    dropdown = next(c for c in controls if isinstance(c, ft.Dropdown))
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    sample_btn = next(
        c for c in controls if isinstance(c, ft.TextButton) and c.icon == ft.Icons.CODE_ROUNDED
    )
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )

    # Deeply nested JSON under char cap shows validation error instead of RecursionError
    dropdown.value = "script"
    dropdown.on_select(None)
    payload_field.value = "[" * 2000 + "]" * 2000
    save_btn.on_click(None)
    assert feedback.visible is True and feedback.color == COLORS["error"]
    assert ws.list_tasks() == []

    # Script -> Insert Sample -> YouTube -> Save must NOT turn JSON tokens into custom_urls
    sample_btn.on_click(None)
    replace_btn = next(
        c
        for c in _walk_controls(page.dialog)
        if getattr(c, "content", None) == get_string("warming_replace_draft_btn")
    )
    replace_btn.on_click(None)
    assert "news.google.com" in payload_field.value
    dropdown.value = "youtube_trust"
    dropdown.on_select(None)
    assert payload_field.value == ""
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 1
    yt_task = ws.list_tasks()[0]
    assert yt_task.preset == "youtube_trust"
    assert yt_task.custom_urls == []
    assert "https://www.youtube.com" in yt_task.resolved_urls()

    # URL text survives YouTube -> Custom -> Script -> YouTube, and Script draft survives round-trip
    payload_field.value = "https://custom.example.org"
    dropdown.value = "custom"
    dropdown.on_select(None)
    assert payload_field.value == "https://custom.example.org"
    dropdown.value = "script"
    dropdown.on_select(None)
    assert "news.google.com" in payload_field.value
    dropdown.value = "youtube_trust"
    dropdown.on_select(None)
    assert payload_field.value == "https://custom.example.org"


def test_codex_s7_1_partial_timeout_and_readonly_wait_for(tmp_path, monkeypatch):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("s7-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class PartialPage:
        def __init__(self):
            self.url = "https://initial.test/"
            self.events: list[str] = []
            self.mode = "submit_timeout"

        def is_closed(self):
            return False

        async def title(self):
            return "Partial"

        async def go_back(self):
            self.events.append("back")

        class _Loc:
            def __init__(self, first):
                self.first = first

        class _First:
            def __init__(self, page):
                self._page = page

            async def fill(self, text, timeout=10_000):
                self._page.events.append(f"fill:{text}")

            async def press(self, key):
                self._page.events.append(f"press:{key}")
                raise PlaywrightTimeoutError("SECRET_SUBMIT_ERR")

            async def wait_for(self, timeout=15_000):
                self._page.events.append("wait_for")
                raise PlaywrightTimeoutError("banner not visible")

        def locator(self, _selector):
            return self._Loc(self._First(self))

    page = PartialPage()
    ctx = SimpleNamespace(pages=[page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["s7-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["s7-prof"] = (info["port"], info["token"])
    try:
        # Case A: optional type(submit=True) fills then times out on Enter -> aborts Interrupted
        t_partial = ws.add_task(
            "Partial Type Submit",
            ["s7-prof"],
            preset="script",
            script_steps=[
                {
                    "action": "type",
                    "selector": "#q",
                    "text": "draft",
                    "submit": True,
                    "optional": True,
                },
                {"action": "back"},
            ],
        )
        logs: list[str] = []
        assert ws.execute_task_sync(t_partial.id, pm, bl, log_fn=logs.append) is False
        assert t_partial.status == "failed"
        assert t_partial.last_result == "Interrupted (0/1 profiles)"
        assert page.events == ["fill:draft", "press:Enter"]
        assert "SECRET_SUBMIT_ERR" not in "\n".join(logs)

        # Case B: optional read-only wait_for timeout remains skippable and completes OK
        page.events.clear()
        t_wait = ws.add_task(
            "Optional WaitFor",
            ["s7-prof"],
            preset="script",
            script_steps=[
                {"action": "wait_for", "selector": "#banner", "optional": True},
                {"action": "back"},
            ],
        )
        assert ws.execute_task_sync(t_wait.id, pm, bl) is True
        assert t_wait.status == "completed"
        assert t_wait.last_result == "OK (1/1 profiles)"
        assert page.events == ["wait_for", "back"]
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_s7_2_late_final_step_past_budget_is_timed_out(tmp_path, monkeypatch):
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("late-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    task = ws.add_task(
        "Late Final Step",
        ["late-prof"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "wait", "seconds": 59},
            {"action": "back"},
        ],
    )
    fake_clock = [100.0]

    class FakeEvent:
        def is_set(self):
            return False

        def wait(self, sec):
            fake_clock[0] += sec
            return False

    class LateLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, _action, _params=None, timeout=60):
            fake_clock[0] += 2.0  # finishes at 161.0 > 160.0 deadline
            return {}

    orig_threading = sched_mod.threading
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_clock[0])
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
    try:
        assert ws.execute_task_sync(task.id, pm, LateLauncher()) is False
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)
    assert task.status == "failed"
    assert task.last_result == "Timed out (0/1 profiles)"


@pytest.mark.parametrize("bad_name", ["__MISSING__", "   "])
def test_codex_s7_3_script_missing_or_blank_name_fails_closed_on_save(tmp_path, bad_name):
    store = tmp_path / "warming.json"
    bad_row = {
        "id": "no_name_script",
        "profile_names": ["p1"],
        "preset": "script",
        "script_steps": [{"action": "back"}],
    }
    if bad_name != "__MISSING__":
        bad_row["name"] = bad_name
    legacy_row = {
        "id": "legacy_ok",
        "name": "Legacy",
        "profile_names": ["p1"],
        "preset": "youtube_trust",
    }
    store.write_text(json.dumps([bad_row, legacy_row]), encoding="utf-8")
    raw_before = store.read_bytes()

    ws = WarmingSchedulerService(file_path=str(store))
    assert ws.list_tasks() == []
    with pytest.raises(OSError, match="Invalid warming task store"):
        ws.add_task("Unrelated", ["p1"], preset="youtube_trust")
    assert store.read_bytes() == raw_before


def test_codex_s7_4_unicode_surrogate_rejection_and_valid_emoji_preserved(tmp_path, monkeypatch):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("u1", "", "macos")
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        surrogate_res = client.post(
            "/api/v1/warming/tasks",
            content=json.dumps(
                {
                    "name": "Surrogate",
                    "profile_names": ["u1"],
                    "preset": "script",
                    "script_steps": [{"action": "type", "selector": "#q", "text": "\ud800"}],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert surrogate_res.status_code == 400
        assert "Invalid warming script" in surrogate_res.json()["detail"]
        assert "\\ud800" not in surrogate_res.json()["detail"]
        assert not (tmp_path / "warming.json").exists()

        valid_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Valid Emoji",
                "profile_names": ["u1"],
                "preset": "script",
                "script_steps": [{"action": "type", "selector": "#q", "text": "  Xin chào 🚀  "}],
            },
        )
        assert valid_res.status_code == 201
        assert valid_res.json()["script_steps"][0]["text"] == "  Xin chào 🚀  "

    # Desktop UI Save callback rejects lone surrogate cleanly without raising UnicodeEncodeError
    ui_store = tmp_path / "ui_surrogate.json"
    ws = WarmingSchedulerService(file_path=str(ui_store))
    page = _DummyPage()
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="u1")]),
        SimpleNamespace(),
    )
    controls = list(_walk_controls(page.dialog))
    dropdown = next(c for c in controls if isinstance(c, ft.Dropdown))
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )

    dropdown.value = "script"
    dropdown.on_select(None)
    payload_field.value = '[{"action":"type","selector":"#q","text":"\\ud800"}]'
    save_btn.on_click(None)
    assert feedback.visible is True and feedback.color == COLORS["error"]
    assert ws.list_tasks() == []
    assert not ui_store.exists()

    payload_field.value = '[{"action":"type","selector":"#q","text":"  Xin chào 🚀  "}]'
    save_btn.on_click(None)
    assert feedback.color == COLORS["success"]
    assert ws.list_tasks()[0].script_steps[0]["text"] == "  Xin chào 🚀  "


def test_codex_s9_1_optional_click_closing_popup_aborts_without_typing_into_base_tab(
    tmp_path, monkeypatch
):
    from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("s9-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class StubPage:
        def __init__(self, url: str, name: str, *, closes_on_click: bool = False):
            self.url = url
            self._name = name
            self.closed = False
            self.closes_on_click = closes_on_click
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            if self.closed:
                raise PlaywrightTargetClosedError("Target page, context or browser has been closed")
            return self._name

        async def go_back(self):
            self.events.append("back")

        class _Loc:
            def __init__(self, first):
                self.first = first

        class _First:
            def __init__(self, page, selector):
                self._page = page
                self._selector = selector

            async def click(self, timeout=10_000):
                self._page.events.append(f"click:{self._selector}")
                if self._page.closes_on_click:
                    self._page.closed = True

            async def fill(self, text, timeout=10_000):
                self._page.events.append(f"fill:{text}")

            async def wait_for(self, timeout=15_000):
                self._page.events.append("wait_for")
                raise PlaywrightTimeoutError("banner not visible")

        def locator(self, selector):
            return self._Loc(self._First(self, selector))

    base = StubPage("https://base.test/", "Base")
    popup = StubPage("https://popup.test/", "Popup", closes_on_click=True)
    ctx = SimpleNamespace(pages=[base, popup])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["s9-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["s9-prof"] = (info["port"], info["token"])
    try:
        # Case A: optional click closes popup then title() raises TargetClosedError -> abort Interrupted
        task_popup = ws.add_task(
            "Popup Close Optional",
            ["s9-prof"],
            preset="script",
            script_steps=[
                {"action": "click", "selector": "#close-popup", "optional": True},
                {"action": "type", "selector": "#secret-input", "text": "SECRET_DATA"},
            ],
        )
        logs: list[str] = []
        assert ws.execute_task_sync(task_popup.id, pm, bl, log_fn=logs.append) is False
        assert task_popup.status == "failed"
        assert task_popup.last_result == "Interrupted (0/1 profiles)"
        assert popup.events == ["click:#close-popup"]
        assert base.events == []
        joined_logs = "\n".join(logs)
        assert "SECRET_DATA" not in joined_logs
        assert "Target page, context" not in joined_logs

        # Case B: optional read-only wait_for on open base tab still skips and completes OK
        task_open = ws.add_task(
            "Open Tab Optional WaitFor",
            ["s9-prof"],
            preset="script",
            script_steps=[
                {"action": "wait_for", "selector": "#banner", "optional": True},
                {"action": "back"},
            ],
        )
        assert ws.execute_task_sync(task_open.id, pm, bl) is True
        assert task_open.status == "completed"
        assert task_open.last_result == "OK (1/1 profiles)"
        assert base.events == ["wait_for", "back"]
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_s9_2_budget_finalization_across_optional_timeout_late_wait_and_cancel_priority(
    tmp_path, monkeypatch
):
    from src.services.browser.launcher import BrowserControlError
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("budget-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    fake_clock = [100.0]
    cancel_state = [False]

    class FakeEvent:
        def __init__(self, *, wait_overshoot: float = 0.0, cancel_on_back: bool = False):
            self.wait_overshoot = wait_overshoot
            self.cancel_on_back = cancel_on_back

        def is_set(self):
            return cancel_state[0]

        def set(self):
            cancel_state[0] = True

        def wait(self, sec):
            fake_clock[0] += sec + self.wait_overshoot
            return cancel_state[0]

    orig_threading = sched_mod.threading

    def _run_with_fake(task_id: str, launcher, *, wait_overshoot: float = 0.0) -> bool:
        cancel_state[0] = False
        monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_clock[0])
        monkeypatch.setattr(
            sched_mod,
            "threading",
            SimpleNamespace(
                Event=lambda: FakeEvent(wait_overshoot=wait_overshoot),
                Thread=orig_threading.Thread,
            ),
        )
        try:
            return ws.execute_task_sync(task_id, pm, launcher)
        finally:
            monkeypatch.setattr(sched_mod, "threading", orig_threading)

    # Case (a): optional read-only wait_for returns 422 action_timeout AFTER 60s deadline -> Timed out
    class OptionalTimeoutPastBudgetLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, _params=None, timeout=60):
            if action == "back":
                fake_clock[0] += 55.0
                return {}
            fake_clock[0] += 5.1  # now at 60.1s > 60s budget
            raise BrowserControlError(
                "TimeoutError: waiting",
                status=422,
                error_code="action_timeout",
                unknown_state=False,
            )

    t_opt_past = ws.add_task(
        "Optional Timeout Past Budget",
        ["budget-prof"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "back"},
            {"action": "wait_for", "selector": "#opt", "optional": True},
        ],
    )
    fake_clock[0] = 100.0
    assert _run_with_fake(t_opt_past.id, OptionalTimeoutPastBudgetLauncher()) is False
    assert t_opt_past.status == "failed"
    assert t_opt_past.last_result == "Timed out (0/1 profiles)"

    # Case (b): wait exactly at budget when starting, but scheduler wakes 0.1s late -> Timed out
    class InstantBackLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, _action, _params=None, timeout=60):
            return {}

    t_late_wait = ws.add_task(
        "Late Wait Overshoot",
        ["budget-prof"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "back"},
            {"action": "wait", "seconds": 60},
        ],
    )
    fake_clock[0] = 200.0
    assert _run_with_fake(t_late_wait.id, InstantBackLauncher(), wait_overshoot=0.1) is False
    assert t_late_wait.status == "failed"
    assert t_late_wait.last_result == "Timed out (0/1 profiles)"

    # Case (c): optional read-only wait_for times out BEFORE deadline -> skipped and completes OK
    class OptionalTimeoutWithinBudgetLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, _params=None, timeout=60):
            if action == "back":
                fake_clock[0] += 5.0
                return {}
            fake_clock[0] += 10.0  # 15.0s <= 60.0s budget
            raise BrowserControlError(
                "TimeoutError: waiting",
                status=422,
                error_code="action_timeout",
                unknown_state=False,
            )

    t_opt_within = ws.add_task(
        "Optional Timeout Within Budget",
        ["budget-prof"],
        preset="script",
        duration_minutes=1,
        script_steps=[
            {"action": "back"},
            {"action": "wait_for", "selector": "#opt", "optional": True},
        ],
    )
    fake_clock[0] = 300.0
    assert _run_with_fake(t_opt_within.id, OptionalTimeoutWithinBudgetLauncher()) is True
    assert t_opt_within.status == "completed"
    assert t_opt_within.last_result == "OK (1/1 profiles)"

    # Case (d): Stop priority when cancelled while a step overruns budget
    class CancelOnControlLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, _action, _params=None, timeout=60):
            fake_clock[0] += 61.0
            cancel_state[0] = True
            return {}

    t_cancel_prio = ws.add_task(
        "Cancel Priority Over Budget",
        ["budget-prof"],
        preset="script",
        duration_minutes=1,
        script_steps=[{"action": "back"}],
    )
    fake_clock[0] = 400.0
    assert _run_with_fake(t_cancel_prio.id, CancelOnControlLauncher()) is False
    assert t_cancel_prio.status == "idle"
    assert t_cancel_prio.last_result == "Stopped (0/1)"


def test_codex_s9_3_surrogate_in_extra_field_name_returns_400_and_safe_ui_feedback(
    tmp_path, monkeypatch
):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    warming_file = tmp_path / "warming.json"
    monkeypatch.setattr(app_config, "WARMING_FILE", str(warming_file))

    container = Container()
    container.profile_manager.add_profile("u1", "", "macos")
    baseline = container.warming_scheduler.add_task("Baseline", ["u1"], preset="youtube_trust")
    assert baseline is not None
    raw_before = warming_file.read_bytes()

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        for bad_step in (
            {"action": "back", "\ud800": 1},
            {"action": "\ud800"},
        ):
            res = client.post(
                "/api/v1/warming/tasks",
                content=json.dumps(
                    {
                        "name": "Surrogate Key",
                        "profile_names": ["u1"],
                        "preset": "script",
                        "script_steps": [bad_step],
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
            assert res.status_code == 400
            detail = res.json()["detail"]
            assert "Invalid warming script" in detail
            detail.encode("utf-8")
            assert "\\ud800" in detail  # ASCII-escaped representation, not raw surrogate
            assert warming_file.read_bytes() == raw_before

    # Desktop UI Save callback shows UTF-8 safe error feedback and preserves store bytes
    ws = WarmingSchedulerService(file_path=str(warming_file))
    page = _DummyPage()
    open_warming_dialog(
        page,
        ws,
        SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="u1")]),
        SimpleNamespace(),
    )
    controls = list(_walk_controls(page.dialog))
    dropdown = next(c for c in controls if isinstance(c, ft.Dropdown))
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )

    dropdown.value = "script"
    dropdown.on_select(None)
    payload_field.value = '[{"action":"back","\\ud800":1}]'
    save_btn.on_click(None)
    assert feedback.visible is True
    assert feedback.color == COLORS["error"]
    feedback.value.encode("utf-8")
    assert warming_file.read_bytes() == raw_before


def test_codex_s11_1_tab_select_and_between_step_popup_close_never_types_into_base_tab(
    tmp_path, monkeypatch
):
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("s11-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class TabPageStub:
        def __init__(self, url: str, label: str, *, close_on_front: bool = False):
            self.url = url
            self._label = label
            self.closed = False
            self.close_on_front = close_on_front
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return self._label

        async def bring_to_front(self):
            self.events.append("bring_to_front")
            if self.close_on_front:
                self.closed = True

        async def close(self):
            self.events.append("close")
            self.closed = True

        class _Loc:
            def __init__(self, first):
                self.first = first

        class _First:
            def __init__(self, page):
                self._page = page

            async def fill(self, text, timeout=10_000):
                self._page.events.append(f"fill:{text}")

        def locator(self, _selector):
            return self._Loc(self._First(self))

    base = TabPageStub("https://base.test/", "Base")
    popup_close_select = TabPageStub("https://popup-a.test/", "PopupA", close_on_front=True)
    ctx = SimpleNamespace(pages=[base, popup_close_select])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["s11-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["s11-prof"] = (info["port"], info["token"])
    try:
        # Case (a): tab_select silently closes popup -> aborts Interrupted, base untouched
        t_sel = ws.add_task(
            "Popup Closes On Select",
            ["s11-prof"],
            preset="script",
            script_steps=[
                {"action": "tab_select", "index": 1},
                {"action": "type", "selector": "#q", "text": "SECRET_A"},
            ],
        )
        assert ws.execute_task_sync(t_sel.id, pm, bl) is False
        assert t_sel.status == "failed"
        assert t_sel.last_result == "Interrupted (0/1 profiles)"
        assert popup_close_select.events == ["bring_to_front"]
        assert base.events == []

        # Case (b): tab_select succeeds, popup closes during wait -> type aborts before touching base
        popup_wait_close = TabPageStub("https://popup-b.test/", "PopupB")
        ctx.pages = [base, popup_wait_close]
        t_wait = ws.add_task(
            "Popup Closes During Wait",
            ["s11-prof"],
            preset="script",
            script_steps=[
                {"action": "tab_select", "index": 1},
                {"action": "wait", "seconds": 1},
                {"action": "type", "selector": "#q", "text": "SECRET_B"},
            ],
        )
        orig_threading = sched_mod.threading

        class ClosingWaitEvent:
            def __init__(self):
                self._set = False

            def is_set(self):
                return self._set

            def set(self):
                self._set = True

            def wait(self, _sec):
                popup_wait_close.closed = True
                return False

        monkeypatch.setattr(
            sched_mod,
            "threading",
            SimpleNamespace(Event=ClosingWaitEvent, Thread=orig_threading.Thread),
        )
        try:
            assert ws.execute_task_sync(t_wait.id, pm, bl) is False
        finally:
            monkeypatch.setattr(sched_mod, "threading", orig_threading)
        assert t_wait.status == "failed"
        assert t_wait.last_result == "Interrupted (0/1 profiles)"
        assert popup_wait_close.events == ["bring_to_front"]
        assert base.events == []

        # Case (c): valid tabs + intentional tab switching completes OK on each chosen tab
        popup_open = TabPageStub("https://popup-c.test/", "PopupC")
        ctx.pages = [base, popup_open]
        t_switch = ws.add_task(
            "Intentional Tab Switch",
            ["s11-prof"],
            preset="script",
            script_steps=[
                {"action": "tab_select", "index": 1},
                {"action": "type", "selector": "#q", "text": "on_popup"},
                {"action": "tab_select", "index": 0},
                {"action": "type", "selector": "#q", "text": "on_base"},
            ],
        )
        assert ws.execute_task_sync(t_switch.id, pm, bl) is True
        assert t_switch.status == "completed"
        assert t_switch.last_result == "OK (1/1 profiles)"
        assert popup_open.events == ["bring_to_front", "fill:on_popup"]
        assert base.events == ["bring_to_front", "fill:on_base"]

        # Case (d): intentional tab_close allows subsequent step to type into remaining base tab
        base.events.clear()
        popup_open.events.clear()
        t_close = ws.add_task(
            "Intentional Tab Close",
            ["s11-prof"],
            preset="script",
            script_steps=[
                {"action": "tab_select", "index": 1},
                {"action": "tab_close", "index": 1},
                {"action": "type", "selector": "#q", "text": "after_close"},
            ],
        )
        assert ws.execute_task_sync(t_close.id, pm, bl) is True
        assert t_close.status == "completed"
        assert t_close.last_result == "OK (1/1 profiles)"
        assert popup_open.events == ["bring_to_front", "close"]
        assert base.events == ["fill:after_close"]
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_s13_1_popup_close_between_request_and_dispatch_never_types_into_base_tab(
    tmp_path, monkeypatch
):
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("s13-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    arm_close = [False]

    class RacePageStub:
        def __init__(self, url: str, label: str, *, arm_on_front: bool = False):
            self.url = url
            self._label = label
            self.closed = False
            self.arm_on_front = arm_on_front
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return self._label

        async def bring_to_front(self):
            self.events.append("bring_to_front")
            if self.arm_on_front:
                arm_close[0] = True

        class _Loc:
            def __init__(self, first):
                self.first = first

        class _First:
            def __init__(self, page):
                self._page = page

            async def fill(self, text, timeout=10_000):
                self._page.events.append(f"fill:{text}")

        def locator(self, _selector):
            return self._Loc(self._First(self))

    base = RacePageStub("https://base.test/", "Base")
    popup_race = RacePageStub("https://popup-race.test/", "PopupRace", arm_on_front=True)
    ctx = SimpleNamespace(pages=[base, popup_race])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    orig_create_task = control.asyncio.create_task

    def _close_popup():
        popup_race.closed = True
        popup_race.events.append("closed_before_dispatch")

    def _racing_create_task(coro, *args, **kwargs):
        if arm_close[0] and getattr(getattr(coro, "cr_code", None), "co_name", "") == "dispatch":
            arm_close[0] = False
            asyncio.get_running_loop().call_soon(_close_popup)
        return orig_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(control.asyncio, "create_task", _racing_create_task)

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["s13-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["s13-prof"] = (info["port"], info["token"])
    try:
        task = ws.add_task(
            "Popup Closes Between Check And Dispatch",
            ["s13-prof"],
            preset="script",
            script_steps=[
                {"action": "tab_select", "index": 1},
                {"action": "type", "selector": "#q", "text": "SECRET_RACE"},
            ],
        )
        assert ws.execute_task_sync(task.id, pm, bl) is False
        assert task.status == "failed"
        assert task.last_result == "Interrupted (0/1 profiles)"
        assert popup_race.events == ["bring_to_front", "closed_before_dispatch"]
        assert base.events == []
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        monkeypatch.setattr(control.asyncio, "create_task", orig_create_task)
        stop_loop.set()
        srv.join(3)


def test_warming_playbooks_catalog_api_and_natural_url_browsing(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.warming.scheduler import (
        WARMING_SCENARIO_PLAYBOOKS,
        describe_warming_step,
        list_warming_scenarios,
        normalize_warming_steps,
    )

    # Every built-in scenario playbook validates cleanly and uses realistic human interactions
    scenarios = list_warming_scenarios()
    by_id = {s["id"]: s for s in scenarios}
    assert {
        "youtube_trust",
        "social_news",
        "gmail_trust",
        "ecommerce_cookie",
        "custom",
        "script",
    } <= set(by_id)
    assert "type" in by_id["social_news"]["interactive_actions"]
    assert "click" in by_id["social_news"]["interactive_actions"]
    assert "press" in by_id["social_news"]["interactive_actions"]
    assert "click" in by_id["gmail_trust"]["interactive_actions"]
    assert "wait_for" in by_id["gmail_trust"]["interactive_actions"]
    assert "back" not in by_id["gmail_trust"]["interactive_actions"]
    assert "back" not in by_id["social_news"]["interactive_actions"]
    assert "scroll" in by_id["youtube_trust"]["interactive_actions"]
    for pid, steps in WARMING_SCENARIO_PLAYBOOKS.items():
        assert len(steps) >= 10, pid
        assert normalize_warming_steps(steps) == steps
        assert not any("reddit.com" in str(st.get("url") or "") for st in steps), pid

    lbl, det = describe_warming_step(
        {
            "action": "navigate",
            "url": "https://user:SECRET_PW@example.test:8443/watch?token=SECRET_SHOULD_NOT_APPEAR",
        }
    )
    assert lbl == "navigate"
    assert det == "example.test:8443/watch"
    assert "SECRET_PW" not in det
    assert "SECRET_SHOULD_NOT_APPEAR" not in det

    assert describe_warming_step(
        {
            "action": "type",
            "selector": "textarea[name='q']",
            "texts": ["giá vàng hôm nay", "giá bạc hôm nay", "drama hôm nay"],
            "submit": True,
        }
    ) == ("type", "textarea[name='q'] \"giá vàng hôm nay\" (+2) + Enter")

    # REST GET /warming/tasks exposes scenario catalog alongside tasks/presets
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    container = Container()
    container.profile_manager.add_profile("nat-prof", "", "macos")
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        res = client.get("/api/v1/warming/tasks").json()
        assert len(res["scenarios"]) == 6

    # Natural multi-phase URL browsing calls scroll + press/re-scroll while preserving dwell sum
    from src.services.warming import scheduler as sched_mod

    ws = container.warming_scheduler
    task = ws.add_task(
        "Natural URL Phase",
        ["nat-prof"],
        preset="custom",
        custom_urls=["https://example.test/"],
        duration_minutes=1,
    )
    waits = []
    controls = []

    class FakeEvent:
        def is_set(self):
            return False

        def wait(self, sec):
            waits.append(sec)
            return False

    class RecordLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            controls.append((action, params))
            return {}

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=FakeEvent, Thread=orig_threading.Thread)
    )
    try:
        assert ws.execute_task_sync(task.id, container.profile_manager, RecordLauncher()) is True
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)
    assert pytest.approx(sum(waits), rel=1e-6) == 60.0
    assert [c[0] for c in controls].count("scroll") >= 6


def test_warming_dialog_hallmark_scenario_cards_step_preview_and_visual_builder(tmp_path):
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming_ui.json"))
    many_profiles = [SimpleNamespace(name=f"p{i}") for i in range(6)]
    page = _DummyPage()
    open_warming_dialog(page, ws, SimpleNamespace(list_profiles=lambda: many_profiles), None)

    controls = list(_walk_controls(page.dialog))
    # Existing widget index contracts stay invariant
    text_fields = [c for c in controls if isinstance(c, ft.TextField)]
    assert text_fields[0].value == "Warm 6 profile(s)"
    dropdowns = [c for c in controls if isinstance(c, ft.Dropdown)]
    assert dropdowns[0].value == "youtube_trust"
    hidden_size12 = [
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    ]
    assert len(hidden_size12) == 1

    # > 5 targets warning pill visible
    assert any(
        isinstance(c, ft.Text)
        and "6" in (c.value or "")
        and "sequentially" in (c.value or "").lower()
        for c in controls
    )

    # All 6 scenario cards are clickable; built-in Social & News shows active interactive playbook
    social_title = get_string("warming_card_social_title")
    social_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == social_title for t in _walk_controls(c))
    )
    social_card.on_click(None)
    assert dropdowns[0].value == "social_news"
    preview_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any("www.google.com" in t for t in preview_texts)
    assert any("www.wikipedia.org" in t for t in preview_texts)
    assert any("#searchInput" in t for t in preview_texts)
    assert not any("reddit.com" in t for t in preview_texts)

    # Gmail Trust card is clickable and displays interactive Gmail inbox/mail-read steps
    gmail_title = get_string("warming_card_gmail_title")
    gmail_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == gmail_title for t in _walk_controls(c))
    )
    gmail_card.on_click(None)
    assert dropdowns[0].value == "gmail_trust"
    gmail_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any("mail.google.com" in t for t in gmail_texts)
    assert any("tr.zA" in t for t in gmail_texts)
    social_card.on_click(None)

    # Override URL in built-in mode switches preview cleanly to URL override without #searchInput
    payload_field = text_fields[1]
    payload_field.value = "https://example.test/override"
    payload_field.on_change(None)
    override_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any("example.test/override" in t for t in override_texts)
    assert not any("www.wikipedia.org" in t for t in override_texts)
    assert not any("#searchInput" in t for t in override_texts)
    payload_field.value = ""
    payload_field.on_change(None)

    # Customize as Script copies built-in Social playbook into script JSON + editable builder
    customize_btn = next(
        c
        for c in controls
        if isinstance(c, ft.OutlinedButton)
        and c.content == get_string("warming_customize_script_btn")
    )
    customize_btn.on_click(None)
    assert dropdowns[0].value == "script"
    assert "wikipedia.org" in (payload_field.value or "")
    assert any(
        isinstance(c, ft.Text) and "#searchInput" in (c.value or "")
        for c in _walk_controls(page.dialog)
    )
    before_steps = json.loads(payload_field.value)

    # Add a step via Quick-Add Step builder
    step_action_dd = dropdowns[1]
    step_val_field = text_fields[2]
    add_step_btn = next(
        c
        for c in controls
        if isinstance(c, ft.OutlinedButton) and c.content == get_string("warming_add_step_btn")
    )
    step_action_dd.value = "click"
    step_action_dd.on_select(None)
    step_val_field.value = ".custom-target"
    add_step_btn.on_click(None)
    after_add = json.loads(payload_field.value)
    assert len(after_add) == len(before_steps) + 1
    assert after_add[-1]["action"] == "click"
    assert after_add[-1]["selector"] == ".custom-target"

    # Reorder and remove via inline step buttons
    move_up_btns = [
        c
        for c in _walk_controls(page.dialog)
        if isinstance(c, ft.IconButton) and c.tooltip == get_string("warming_move_up")
    ]
    move_up_btns[-1].on_click(None)
    after_move = json.loads(payload_field.value)
    assert after_move[-2]["selector"] == ".custom-target"

    remove_btns = [
        c
        for c in _walk_controls(page.dialog)
        if isinstance(c, ft.IconButton) and c.tooltip == get_string("warming_remove_step")
    ]
    remove_btns[-2].on_click(None)
    after_remove = json.loads(payload_field.value)
    assert len(after_remove) == len(before_steps)

    # Save task and verify rendered task rows contain zero middots
    save_btn = next(
        c
        for c in _walk_controls(page.dialog)
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 1
    rendered_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any(f"script ({len(before_steps)} steps)" in t for t in rendered_texts)
    assert not any(" · " in t for t in rendered_texts)


def test_codex_r2_1_url_mode_pins_tab_on_close_and_drops_press(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("url-tab-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class TwoTabStub:
        def __init__(self, url: str, label: str):
            self.url = url
            self._label = label
            self.closed = False
            self.events: list[str] = []

            class _Kb:
                def __init__(self, page):
                    self._p = page

                async def press(self, key: str):
                    self._p.events.append(f"press:{key}")

            self.keyboard = _Kb(self)

        def is_closed(self):
            return self.closed

        async def title(self):
            return self._label

        async def goto(self, url: str, wait_until: str = "load"):
            self.events.append(f"goto:{url}")

        async def evaluate(self, _expr: str):
            self.events.append("scroll")
            return True

    bg_page = TwoTabStub("https://background.test/", "Background")
    target_page = TwoTabStub("https://target.test/", "Target")
    ctx = SimpleNamespace(pages=[bg_page, target_page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["url-tab-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["url-tab-prof"] = (info["port"], info["token"])
    scrolls_seen = 0
    orig_control = bl.control

    def _closing_control(
        prof_name,
        action,
        params=None,
        timeout=60,
        *,
        require_current=False,
        target_page_id=None,
        return_page_id=False,
    ):
        nonlocal scrolls_seen
        res = orig_control(
            prof_name,
            action,
            params,
            timeout=timeout,
            require_current=require_current,
            target_page_id=target_page_id,
            return_page_id=return_page_id,
        )
        if action == "scroll":
            scrolls_seen += 1
            if scrolls_seen == 2:
                target_page.closed = True
        return res

    bl.control = _closing_control

    class InstantEvent:
        def is_set(self):
            return False

        def wait(self, _sec):
            return False

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=InstantEvent, Thread=orig_threading.Thread)
    )
    try:
        task = ws.add_task(
            "URL Two-Tab Pin",
            ["url-tab-prof"],
            preset="custom",
            custom_urls=["https://example.test/start"],
            duration_minutes=1,
        )
        assert ws.execute_task_sync(task.id, pm, bl, step_dwell_seconds=6.0) is False
        assert task.status == "failed"
        assert task.last_result == "Interrupted (0/1 profiles)"
        assert target_page.events == ["goto:https://example.test/start", "scroll", "scroll"]
        assert bg_page.events == []
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_r2_2_url_mode_stop_during_scroll2_sends_no_more_commands(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("stop-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    task = ws.add_task(
        "URL Stop Mid-Scroll",
        ["stop-prof"],
        preset="custom",
        custom_urls=["https://example.test/stop"],
        duration_minutes=1,
    )

    trace: list[str] = []
    scrolls = 0

    class StopLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, _params=None, timeout=60, *, require_current=False):
            nonlocal scrolls
            trace.append(action)
            if action == "scroll":
                scrolls += 1
                if scrolls == 2:
                    ws.stop_task(task.id)
            return {}

    class ZeroWaitEvent:
        def __init__(self):
            self._flag = False

        def set(self):
            self._flag = True

        def is_set(self):
            return self._flag

        def wait(self, _sec):
            return self._flag

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=ZeroWaitEvent, Thread=orig_threading.Thread)
    )
    try:
        assert ws.execute_task_sync(task.id, pm, StopLauncher(), step_dwell_seconds=6.0) is True
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)

    assert trace == ["navigate", "scroll", "scroll"]
    assert task.status == "idle"
    assert task.last_result == "Stopped (1/1)"


def test_codex_r2_3_redacts_malformed_urls_step_objects_and_url_logs(tmp_path):
    from src.services.browser.launcher import BrowserControlError
    from src.services.warming.scheduler import describe_warming_step

    assert describe_warming_step(
        {"action": "navigate", "url": "https://user:SECRET_PW@example.test:8443/path?k=SEC#frag"}
    ) == ("navigate", "example.test:8443/path")
    assert describe_warming_step({"action": "navigate", "url": "https://[broken"}) == (
        "navigate",
        "URL",
    )
    assert describe_warming_step(
        {"action": "navigate", "url": "https://u:DUMMY@x:bad/?q=FAKE#z"}
    ) == ("navigate", "URL")
    assert describe_warming_step({"action": "navigate", "url": {"toString": None}}) == (
        "navigate",
        "URL",
    )
    assert describe_warming_step(None) == ("step", "")

    pm = SimpleNamespace(
        profiles={"log-prof": SimpleNamespace(name="log-prof")},
        list_profiles=lambda: [SimpleNamespace(name="log-prof")],
    )
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    secret_url = "https://u:SECRET_PW@good.example.test:8443/feed?tok=SECRET_Q#FRAG_SECRET"
    bad_url = "https://u:SECRET_PW@bad.example.test:8443/fail?tok=SECRET_Q#FRAG_SECRET"
    task = ws.add_task(
        "Redacted Logs",
        ["log-prof"],
        preset="custom",
        custom_urls=[secret_url, bad_url],
    )
    logs: list[str] = []

    class LogTestLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60, *, require_current=False):
            if action == "navigate" and params and "bad.example.test" in params.get("url", ""):
                raise BrowserControlError(f"boom {bad_url}", status=422)
            return {}

    assert (
        ws.execute_task_sync(
            task.id, pm, LogTestLauncher(), log_fn=logs.append, step_dwell_seconds=0.0
        )
        is True
    )
    joined = "\n".join(logs)
    assert "good.example.test:8443/feed" in joined
    assert "bad.example.test:8443/fail" in joined
    for leaked in ("SECRET_PW", "SECRET_Q", "FRAG_SECRET"):
        assert leaked not in joined

    # Flet Custom URLs tolerates partial invalid IPv6 URLs without raising
    page = _DummyPage()
    open_warming_dialog(page, ws, pm, SimpleNamespace())
    controls = list(_walk_controls(page.dialog))
    custom_title = get_string("warming_card_custom_title")
    custom_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == custom_title for t in _walk_controls(c))
    )
    custom_card.on_click(None)
    payload_field = [c for c in _walk_controls(page.dialog) if isinstance(c, ft.TextField)][1]
    payload_field.value = "https://[broken"
    payload_field.on_change(None)
    assert any(
        isinstance(c, ft.Text) and c.value == get_string("warming_override_invalid_msg")
        for c in _walk_controls(page.dialog)
    )


def test_codex_r2_6_7_flet_draft_overwrite_guard_and_truthful_preview(tmp_path):
    from src.core.strings import get_string

    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming_guard.json"))
    pm = SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name="p1")])
    page = _DummyPage()
    open_warming_dialog(page, ws, pm, SimpleNamespace())
    controls = list(_walk_controls(page.dialog))
    dropdown = next(c for c in controls if isinstance(c, ft.Dropdown))
    text_fields = [c for c in controls if isinstance(c, ft.TextField)]
    payload_field = text_fields[1]
    duration_field = text_fields[4]

    # Built-in cards display interactive step counts matching actual execution
    from src.services.warming.scheduler import WARMING_SCENARIO_PLAYBOOKS

    card_texts = [c.value or "" for c in controls if isinstance(c, ft.Text)]
    assert any(
        get_string(
            "warming_steps_count_badge", count=len(WARMING_SCENARIO_PLAYBOOKS["social_news"])
        )
        == t
        for t in card_texts
    )

    # Create unsaved script draft, switch to Social & News, then try Customize -> Cancel keeps draft
    dropdown.value = "script"
    dropdown.on_select(None)
    unsaved_json = '[{"action": "navigate", "url": "https://draft.test/unsaved"}]'
    payload_field.value = unsaved_json
    payload_field.on_change(None)

    social_title = get_string("warming_card_social_title")
    social_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == social_title for t in _walk_controls(c))
    )
    social_card.on_click(None)
    assert dropdown.value == "social_news"
    assert duration_field.value == "5"
    duration_field.value = "9"

    # Attempt Customize -> Cancel keeps preset, duration, and /unsaved script draft intact
    customize_btn = next(
        c
        for c in controls
        if isinstance(c, ft.OutlinedButton)
        and c.content == get_string("warming_customize_script_btn")
    )
    customize_btn.on_click(None)
    cancel_btn = next(
        c
        for c in _walk_controls(page.dialog)
        if isinstance(c, ft.TextButton) and c.content == get_string("cancel")
    )
    cancel_btn.on_click(None)
    assert dropdown.value == "social_news"
    assert duration_field.value == "9"
    dropdown.value = "script"
    dropdown.on_select(None)
    assert "/unsaved" in payload_field.value

    # Template pill and Insert Sample also prompt; Replace overwrites cleanly
    sample_btn = next(
        c for c in controls if isinstance(c, ft.TextButton) and c.icon == ft.Icons.CODE_ROUNDED
    )
    sample_btn.on_click(None)
    replace_btn = next(
        c
        for c in _walk_controls(page.dialog)
        if getattr(c, "content", None) == get_string("warming_replace_draft_btn")
    )
    replace_btn.on_click(None)
    assert "news.google.com" in payload_field.value
    assert "/unsaved" not in payload_field.value


def test_codex_s9_1_url_mode_aborts_when_active_tab_switched_while_both_open(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("switch-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class OpenTabStub:
        def __init__(self, url: str, label: str):
            self.url = url
            self._label = label
            self.closed = False
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return self._label

        async def bring_to_front(self):
            self.events.append("bring_to_front")

        async def goto(self, url: str, wait_until: str = "load"):
            self.events.append(f"goto:{url}")

        async def evaluate(self, _expr: str):
            self.events.append("scroll")
            return True

    tab0 = OpenTabStub("https://work.test/", "Work")
    tab1 = OpenTabStub("https://start.test/", "WarmingTab")
    ctx = SimpleNamespace(pages=[tab0, tab1])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["switch-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["switch-prof"] = (info["port"], info["token"])
    orig_control = bl.control
    scrolls_seen = 0

    def _switching_control(
        prof_name,
        action,
        params=None,
        timeout=60,
        *,
        require_current=False,
        target_page_id=None,
        return_page_id=False,
    ):
        nonlocal scrolls_seen
        res = orig_control(
            prof_name,
            action,
            params,
            timeout=timeout,
            require_current=require_current,
            target_page_id=target_page_id,
            return_page_id=return_page_id,
        )
        if action == "scroll":
            scrolls_seen += 1
            if scrolls_seen == 2:
                # Switch active tab to tab0 while BOTH tabs remain open.
                orig_control(prof_name, "tab_select", {"index": 0}, timeout=5)
        return res

    bl.control = _switching_control

    class InstantEvent:
        def is_set(self):
            return False

        def wait(self, _sec):
            return False

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=InstantEvent, Thread=orig_threading.Thread)
    )
    try:
        task = ws.add_task(
            "URL Tab Switch Guard",
            ["switch-prof"],
            preset="custom",
            custom_urls=["https://example.test/one", "https://example.test/two"],
            duration_minutes=1,
        )
        assert ws.execute_task_sync(task.id, pm, bl, step_dwell_seconds=6.0) is False
        assert task.status == "failed"
        assert task.last_result == "Interrupted (0/1 profiles)"
        assert tab1.events == ["goto:https://example.test/one", "scroll", "scroll"]
        assert tab0.events == ["bring_to_front"]
        assert not tab0.closed and not tab1.closed
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_s9_3_url_mode_known_scroll_error_keeps_dwell_budget(tmp_path, monkeypatch):
    from src.core import config as app_config
    from src.services.browser.launcher import BrowserControlError
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("dwell-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    task = ws.add_task(
        "Dwell Known Scroll Error",
        ["dwell-prof"],
        preset="custom",
        custom_urls=["https://example.test/only"],
        duration_minutes=1,
    )
    waits: list[float] = []
    controls: list[str] = []

    class RecordEvent:
        def is_set(self):
            return False

        def wait(self, sec):
            waits.append(sec)
            return False

    class FailingScrollLauncher:
        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            controls.append(action)
            if action == "scroll":
                raise BrowserControlError(
                    "wheel failed", status=422, error_code="action_error", unknown_state=False
                )
            return {}

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=RecordEvent, Thread=orig_threading.Thread)
    )
    try:
        assert ws.execute_task_sync(task.id, pm, FailingScrollLauncher()) is True
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)

    assert pytest.approx(sum(waits), rel=1e-6) == 60.0
    assert controls.count("navigate") == 3
    assert controls.count("scroll") == 3
    assert task.status == "completed"


def test_codex_s9_4_override_validation_rejects_unsupported_schemes_and_filters_mixed(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core import config as app_config
    from src.core.container import Container
    from src.core.strings import get_string

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("ovr-prof", "", "macos")
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        bad_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Bad Override",
                "profile_names": ["ovr-prof"],
                "preset": "social_news",
                "custom_urls": ["ftp://example.test/override"],
                "duration_minutes": 4,
            },
        )
        assert bad_res.status_code == 400
    assert container.warming_scheduler.list_tasks() == []

    # Flet: unsupported override on Social & News shows error and never silently saves Wikipedia defaults
    page = _DummyPage()
    ws = container.warming_scheduler
    open_warming_dialog(page, ws, container.profile_manager, SimpleNamespace())
    controls = list(_walk_controls(page.dialog))
    social_title = get_string("warming_card_social_title")
    social_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == social_title for t in _walk_controls(c))
    )
    social_card.on_click(None)
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )

    payload_field.value = "ftp://example.test/override"
    payload_field.on_change(None)
    preview_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any(get_string("warming_override_invalid_msg") == t for t in preview_texts)
    assert not any("example.test/override" in t for t in preview_texts)
    save_btn.on_click(None)
    assert feedback.visible is True and feedback.color == COLORS["error"]
    assert ws.list_tasks() == []

    # Mixed override keeps only HTTP(S), surfaces ignored count, and saves filtered URL list
    payload_field.value = "ftp://bad.test https://good.example/override"
    payload_field.on_change(None)
    mixed_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any("good.example/override" in t for t in mixed_texts)
    assert any(get_string("warming_urls_ignored_warn", count=1) == t for t in mixed_texts)
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 1
    assert ws.list_tasks()[0].custom_urls == ["https://good.example/override"]

    # Blank intentional override still saves built-in Social & News defaults cleanly
    payload_field.value = ""
    payload_field.on_change(None)
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 2
    assert ws.list_tasks()[1].custom_urls == []
    assert "https://www.wikipedia.org" in ws.list_tasks()[1].resolved_urls()


def test_codex_s11_1_failed_first_navigate_cannot_redirect_url2_after_tab_switch(
    tmp_path, monkeypatch
):
    from src.core import config as app_config
    from src.services.browser import control
    from src.services.browser.launcher import BrowserLauncher
    from src.services.warming import scheduler as sched_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    pm = pm_mod.ProfileManager()
    pm.add_profile("fail-nav-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    class NavErrTabStub:
        def __init__(self, url: str, label: str, *, fail_url_substr: str = ""):
            self.url = url
            self._label = label
            self.fail_url_substr = fail_url_substr
            self.closed = False
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return self._label

        async def bring_to_front(self):
            self.events.append("bring_to_front")

        async def goto(self, url: str, wait_until: str = "load"):
            self.events.append(f"goto:{url}")
            if self.fail_url_substr and self.fail_url_substr in url:
                raise RuntimeError("net::ERR_CONNECTION_REFUSED")

        async def evaluate(self, _expr: str):
            self.events.append("scroll")
            return True

    tab0 = NavErrTabStub("https://work.test/", "Work")
    tab1 = NavErrTabStub(
        "https://start.test/", "WarmingTab", fail_url_substr="example.test/fail-first"
    )
    ctx = SimpleNamespace(pages=[tab0, tab1])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["fail-nav-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["fail-nav-prof"] = (info["port"], info["token"])
    orig_control = bl.control
    switched = [False]

    def _switching_on_first_nav_err(
        prof_name,
        action,
        params=None,
        timeout=60,
        *,
        require_current=False,
        target_page_id=None,
        return_page_id=False,
    ):
        try:
            return orig_control(
                prof_name,
                action,
                params,
                timeout=timeout,
                require_current=require_current,
                target_page_id=target_page_id,
                return_page_id=return_page_id,
            )
        except Exception:
            if not switched[0]:
                switched[0] = True
                orig_control(prof_name, "tab_select", {"index": 0}, timeout=5)
            raise

    bl.control = _switching_on_first_nav_err

    class InstantEvent:
        def is_set(self):
            return False

        def wait(self, _sec):
            return False

    orig_threading = sched_mod.threading
    monkeypatch.setattr(
        sched_mod, "threading", SimpleNamespace(Event=InstantEvent, Thread=orig_threading.Thread)
    )
    try:
        task = ws.add_task(
            "Fail First Nav Switch",
            ["fail-nav-prof"],
            preset="custom",
            custom_urls=["https://example.test/fail-first", "https://example.test/second"],
            duration_minutes=1,
        )
        assert ws.execute_task_sync(task.id, pm, bl, step_dwell_seconds=6.0) is False
        assert task.status == "failed"
        assert task.last_result == "Interrupted (0/1 profiles)"
        assert tab1.events == ["goto:https://example.test/fail-first"]
        assert tab0.events == ["bring_to_front"]

        # Even if initial navigate error lacks page_id (e.g. HTTP 400), URL #2 is never tried unpinned
        class MissingIdLauncher:
            def __init__(self):
                self.calls = 0

            def is_running(self, _name):
                return True

            def control(
                self,
                _name,
                _action,
                _params=None,
                timeout=60,
                *,
                require_current=False,
                target_page_id=None,
                return_page_id=False,
            ):
                self.calls += 1
                raise BrowserControlError("bad request", status=400, unknown_state=False)

        from src.services.browser.launcher import BrowserControlError

        missing_bl = MissingIdLauncher()
        task2 = ws.add_task(
            "Missing Pid First Err",
            ["fail-nav-prof"],
            preset="custom",
            custom_urls=["https://example.test/one", "https://example.test/two"],
            duration_minutes=1,
        )
        assert ws.execute_task_sync(task2.id, pm, missing_bl, step_dwell_seconds=0.0) is False
        assert missing_bl.calls == 1
        assert task2.status == "failed"
        assert task2.last_result == "Interrupted (0/1 profiles)"
    finally:
        monkeypatch.setattr(sched_mod, "threading", orig_threading)
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_codex_s11_3_legacy_invalid_custom_and_builtin_overrides_preserve_disk_and_block_run(
    tmp_path, monkeypatch
):
    from src.core import config as app_config

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    warming_file = tmp_path / "warming.json"
    monkeypatch.setattr(app_config, "WARMING_FILE", str(warming_file))

    legacy_store = [
        {
            "id": "legacy-custom",
            "name": "Legacy Custom Bad",
            "profile_names": ["leg-prof"],
            "preset": "custom",
            "custom_urls": ["https:///example.test/old"],
            "duration_minutes": 3,
        },
        {
            "id": "legacy-builtin",
            "name": "Legacy Social Bad",
            "profile_names": ["leg-prof"],
            "preset": "social_news",
            "custom_urls": ["https:///example.test/legacy-ovr"],
            "duration_minutes": 3,
        },
        {
            "id": "valid-yt",
            "name": "Valid YouTube",
            "profile_names": ["leg-prof"],
            "preset": "youtube_trust",
            "custom_urls": [],
            "duration_minutes": 3,
        },
    ]
    warming_file.write_text(json.dumps(legacy_store, indent=2), encoding="utf-8")

    pm = pm_mod.ProfileManager()
    pm.add_profile("leg-prof", "", "macos")
    ws = WarmingSchedulerService(file_path=str(warming_file))

    assert ws.get_task("legacy-custom").custom_urls == ["https:///example.test/old"]
    assert ws.get_task("legacy-builtin").custom_urls == ["https:///example.test/legacy-ovr"]
    assert ws.get_task("legacy-custom").resolved_urls() == []
    assert ws.get_task("legacy-builtin").resolved_urls() == []

    launches: list[str] = []
    controls: list[tuple[str, dict | None]] = []

    class RecordLauncher:
        def is_running(self, _name):
            return False

        def start_thread(self, profile, _log):
            launches.append(profile.name)
            return True

        def wait_for_launch(self, _name, timeout=45):
            return True, None

        def control(self, _name, action, params=None, timeout=60):
            controls.append((action, params))
            return {}

        def stop_profile(self, _name):
            return True

    launcher = RecordLauncher()
    for tid, orig_url in (
        ("legacy-custom", "https:///example.test/old"),
        ("legacy-builtin", "https:///example.test/legacy-ovr"),
    ):
        assert ws.execute_task_sync(tid, pm, launcher, step_dwell_seconds=0.0) is False
        tk = ws.get_task(tid)
        assert tk.status == "failed"
        assert "Invalid URLs" in tk.last_result
        assert tk.custom_urls == [orig_url]
        assert launches == []
        assert controls == []

    persisted = {item["id"]: item for item in json.loads(warming_file.read_text(encoding="utf-8"))}
    assert persisted["legacy-custom"]["custom_urls"] == ["https:///example.test/old"]
    assert persisted["legacy-builtin"]["custom_urls"] == ["https:///example.test/legacy-ovr"]

    # Valid task in the same store still runs cleanly
    assert ws.execute_task_sync("valid-yt", pm, launcher, step_dwell_seconds=0.0) is True
    assert launches == ["leg-prof"]
    assert any("youtube.com" in (c[1] or {}).get("url", "") for c in controls)


def test_codex_s13_3_port_validation_parity_across_scheduler_rest_and_flet(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core import config as app_config
    from src.core.container import Container
    from src.core.strings import get_string
    from src.services.warming.scheduler import normalize_warming_urls

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    warming_file = tmp_path / "warming.json"
    monkeypatch.setattr(app_config, "WARMING_FILE", str(warming_file))

    for bad_url in (
        "https://example.test:70000/",
        "https://example.test:65536/",
        "https://example.test:-1/",
        "https://example.test:bad/",
        "https://[::1]:70000/x",
        "localhost:70000/x",
        "https://example.test\\foo",
        ["https://bad host/x"],
    ):
        assert normalize_warming_urls(bad_url) == []

    assert normalize_warming_urls(
        "https://example.test:8443/ok https://example.test:65535/ https://[::1]:8080/x localhost:8080/x"
    ) == [
        "https://example.test:8443/ok",
        "https://example.test:65535/",
        "https://[::1]:8080/x",
        "https://localhost:8080/x",
    ]

    container = Container()
    container.profile_manager.add_profile("port-prof", "", "macos")
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        for bad_preset in ("social_news", "custom"):
            bad_res = client.post(
                "/api/v1/warming/tasks",
                json={
                    "name": f"Bad Port {bad_preset}",
                    "profile_names": ["port-prof"],
                    "preset": bad_preset,
                    "custom_urls": ["https://example.test:70000/"],
                    "duration_minutes": 3,
                },
            )
            assert bad_res.status_code == 400
        assert container.warming_scheduler.list_tasks() == []

        mixed_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Mixed Port Override",
                "profile_names": ["port-prof"],
                "preset": "social_news",
                "custom_urls": ["https://example.test:70000/", "https://example.test:8443/ok"],
                "duration_minutes": 4,
            },
        )
        assert mixed_res.status_code == 201
        assert mixed_res.json()["custom_urls"] == ["https://example.test:8443/ok"]

        ok_res = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "Valid Port Custom",
                "profile_names": ["port-prof"],
                "preset": "custom",
                "custom_urls": ["https://example.test:8443/ok"],
                "duration_minutes": 3,
            },
        )
        assert ok_res.status_code == 201
        assert ok_res.json()["custom_urls"] == ["https://example.test:8443/ok"]

    # Flet: bad port on Social & News rejects save and never renders raw "URL" step fallback
    page = _DummyPage()
    ws = container.warming_scheduler
    for existing_task in list(ws.list_tasks()):
        ws.delete_task(existing_task.id)
    open_warming_dialog(page, ws, container.profile_manager, SimpleNamespace())
    controls = list(_walk_controls(page.dialog))
    social_title = get_string("warming_card_social_title")
    social_card = next(
        c
        for c in controls
        if isinstance(c, ft.Container)
        and getattr(c, "on_click", None) is not None
        and any(isinstance(t, ft.Text) and t.value == social_title for t in _walk_controls(c))
    )
    social_card.on_click(None)
    payload_field = [c for c in controls if isinstance(c, ft.TextField)][1]
    save_btn = next(
        c
        for c in controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )
    feedback = next(
        c for c in controls if isinstance(c, ft.Text) and c.size == 12 and not c.visible
    )

    payload_field.value = "https://example.test:70000/"
    payload_field.on_change(None)
    preview_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert get_string("warming_override_invalid_msg") in preview_texts
    assert "navigate" not in preview_texts
    assert not any("70000" in t or "wikipedia.org" in t for t in preview_texts)
    save_btn.on_click(None)
    assert feedback.visible is True and feedback.color == COLORS["error"]
    assert ws.list_tasks() == []

    payload_field.value = "https://example.test:70000/ https://example.test:8443/ok"
    payload_field.on_change(None)
    mixed_texts = [c.value or "" for c in _walk_controls(page.dialog) if isinstance(c, ft.Text)]
    assert any("example.test:8443/ok" in t for t in mixed_texts)
    assert get_string("warming_urls_ignored_warn", count=1) in mixed_texts
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 1
    assert ws.list_tasks()[0].custom_urls == ["https://example.test:8443/ok"]

    payload_field.value = "https://example.test:8443/ok"
    payload_field.on_change(None)
    save_btn.on_click(None)
    assert len(ws.list_tasks()) == 2
    assert ws.list_tasks()[1].custom_urls == ["https://example.test:8443/ok"]

    # Legacy store entry with invalid port is preserved verbatim on disk and blocked from running
    warming_file.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-bad-port",
                    "name": "Legacy Bad Port",
                    "profile_names": ["port-prof"],
                    "preset": "custom",
                    "custom_urls": ["https://example.test:70000/"],
                    "duration_minutes": 3,
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    reloaded = WarmingSchedulerService(file_path=str(warming_file))
    legacy_task = reloaded.get_task("legacy-bad-port")
    assert legacy_task.custom_urls == ["https://example.test:70000/"]
    assert legacy_task.resolved_urls() == []
    launches: list[str] = []

    class NoLaunch:
        def is_running(self, _name):
            return False

        def start_thread(self, profile, _log):
            launches.append(profile.name)
            return True

    assert (
        reloaded.execute_task_sync("legacy-bad-port", container.profile_manager, NoLaunch())
        is False
    )
    assert legacy_task.status == "failed"
    assert "Invalid URLs" in legacy_task.last_result
    assert launches == []


def test_warming_interactive_social_gmail_texts_headless_and_pre_action_timeout(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core import config as app_config
    from src.core.container import Container
    from src.core.strings import get_string
    from src.models.profile import Profile
    from src.services.browser import control, process
    from src.services.browser import launcher as launcher_mod
    from src.services.warming.scheduler import (
        _SOCIAL_GOOGLE_QUERIES,
        _WIKI_SEARCH_TOPICS,
        normalize_warming_steps,
    )
    from src.ui.dialogs import warming as warming_dlg_mod

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    warming_file = tmp_path / "warming.json"
    monkeypatch.setattr(app_config, "WARMING_FILE", str(warming_file))

    # 1. `type` texts validation
    for bad in (
        [{"action": "type", "selector": "input"}],
        [{"action": "type", "selector": "input", "text": "a", "texts": ["b"]}],
        [{"action": "type", "selector": "input", "texts": []}],
        [{"action": "type", "selector": "input", "texts": ["   "]}],
        [{"action": "type", "selector": "input", "texts": [f"q{i}" for i in range(31)]}],
    ):
        with pytest.raises(ValueError):
            normalize_warming_steps(bad)

    valid_type = normalize_warming_steps(
        [{"action": "type", "selector": "input", "texts": ["giá vàng hôm nay", "drama hôm nay"]}]
    )
    assert valid_type[0]["texts"] == ["giá vàng hôm nay", "drama hôm nay"]

    # 2. Built-in social_news & gmail_trust execute interactive playbooks directly + headless flag
    container = Container()
    container.profile_manager.add_profile("warm-p", "", "macos", engine="chrome")
    ws = container.warming_scheduler

    social = ws.add_task("Social Interactive", ["warm-p"], preset="social_news", headless=True)
    gmail = ws.add_task("Gmail Trust Run", ["warm-p"], preset="gmail_trust", headless=True)
    assert social is not None and social.headless is True
    assert gmail is not None and gmail.headless is True

    # Persisted across reload
    reloaded = WarmingSchedulerService(file_path=str(warming_file))
    assert reloaded.get_task(social.id).headless is True
    assert reloaded.get_task(gmail.id).headless is True

    launches: list[tuple[str, bool | None]] = []
    controls: list[tuple[str, dict]] = []
    logs: list[str] = []

    class RecordingLauncher:
        def __init__(self, initially_running: set[str] | None = None):
            self.running = set(initially_running or ())
            self.url = "about:blank"
            self._click_n = 0

        def is_running(self, name: str) -> bool:
            return name in self.running

        def start_thread(self, profile, _log, *, headless=None) -> bool:
            launches.append((profile.name, headless))
            self.running.add(profile.name)
            return True

        def wait_for_launch(self, _name: str, timeout: float = 45.0):
            return True, None

        def control(self, _name: str, action: str, params=None, timeout: float = 60.0):
            p = dict(params or {})
            controls.append((action, p))
            if action == "navigate" and p.get("url"):
                self.url = str(p["url"])
            elif action == "click":
                self._click_n += 1
                self.url = f"{self.url}/msg-{self._click_n}"
            return {"ok": True, "url": self.url}

        def stop_profile(self, name: str, timeout: int = 2) -> bool:
            self.running.discard(name)
            return True

    bl = RecordingLauncher()
    assert (
        ws.execute_task_sync(
            social.id,
            container.profile_manager,
            bl,
            log_fn=logs.append,
            step_dwell_seconds=0.0,
        )
        is True
    )
    assert launches == [("warm-p", True)]
    assert "warm-p" not in bl.running
    nav_urls = [p.get("url", "") for act, p in controls if act == "navigate"]
    assert "https://www.google.com" in nav_urls
    assert "https://www.wikipedia.org" in nav_urls
    assert "https://news.ycombinator.com" in nav_urls
    assert not any("reddit.com" in u for u in nav_urls)
    typed_texts = [p.get("text", "") for act, p in controls if act == "type"]
    assert any(t in _SOCIAL_GOOGLE_QUERIES for t in typed_texts)
    assert any(t in _WIKI_SEARCH_TOPICS for t in typed_texts)
    assert any(act == "click" and "h3" in p.get("selector", "") for act, p in controls)
    assert any(act == "click" and "/wiki/" in p.get("selector", "") for act, p in controls)
    # Typed queries are never leaked into logs
    joined_logs = "\n".join(logs)
    assert not any(q in joined_logs for q in _SOCIAL_GOOGLE_QUERIES)

    # Gmail trust playbook runs inbox scroll + email clicks + inbox navigate (zero back), and logs reuse if already open
    controls.clear()
    launches.clear()
    logs.clear()
    bl_open = RecordingLauncher(initially_running={"warm-p"})
    assert (
        ws.execute_task_sync(
            gmail.id,
            container.profile_manager,
            bl_open,
            log_fn=logs.append,
            step_dwell_seconds=0.0,
        )
        is True
    )
    assert launches == []
    assert any("already running" in line for line in logs)
    assert (
        sum(1 for act, p in controls if act == "navigate" and "mail.google.com" in p.get("url", ""))
        == 3
    )
    assert sum(1 for act, p in controls if act == "click" and "tr.zA" in p.get("selector", "")) == 2
    assert sum(1 for act, _ in controls if act == "back") == 0
    assert sum(1 for act, _ in controls if act == "scroll") >= 6

    # 3. spawn_browser & BrowserLauncher forward headless flag to FOXPROFILE_HEADLESS cleanly
    captured_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda _cmd, **kw: captured_envs.append(kw["env"]) or SimpleNamespace(poll=lambda: None),
    )
    process.spawn_browser(Profile("p-hid", engine="chrome"), headless=True)
    assert captured_envs[-1]["FOXPROFILE_HEADLESS"] == "true"
    process.spawn_browser(Profile("p-vis", engine="chrome"), headless=False)
    assert captured_envs[-1]["FOXPROFILE_HEADLESS"] == "false"

    spawn_calls: list[tuple[str, bool | None]] = []
    monkeypatch.setattr(
        launcher_mod,
        "spawn_browser",
        lambda prof, *, headless=None: (
            spawn_calls.append((prof.name, headless)) or SimpleNamespace(poll=lambda: None)
        ),
    )
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)
    real_bl = launcher_mod.BrowserLauncher()
    monkeypatch.setattr(real_bl, "_monitor_process", lambda *a: None)
    try:
        assert real_bl.start_thread(Profile("p-h"), lambda _: None, headless=True) is True
        assert spawn_calls == [("p-h", True)]
    finally:
        real_bl._active_sessions.clear()

    # 4. REST API + Flet UI persist headless checkbox (unchecked by default) and show badge
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        r = client.post(
            "/api/v1/warming/tasks",
            json={
                "name": "REST Gmail Hidden",
                "profile_names": ["warm-p"],
                "preset": "gmail_trust",
                "duration_minutes": 4,
                "headless": True,
            },
        )
        assert r.status_code == 201
        assert r.json()["headless"] is True

    monkeypatch.setattr(warming_dlg_mod, "host_supports_headed", lambda: True)
    page = _DummyPage()
    open_warming_dialog(page, ws, container.profile_manager, SimpleNamespace())
    dlg_controls = list(_walk_controls(page.dialog))
    hide_cb = next(
        c
        for c in dlg_controls
        if isinstance(c, ft.Checkbox) and c.label == get_string("warming_hide_browser")
    )
    assert hide_cb.value is False
    hide_cb.value = True
    save_btn = next(
        c
        for c in dlg_controls
        if getattr(c, "content", None) == get_string("warming_save_schedule_btn")
    )
    save_btn.on_click(None)
    assert ws.list_tasks()[-1].headless is True
    assert any(
        isinstance(c, ft.Text) and c.value == get_string("warming_headless_badge")
        for c in _walk_controls(page.dialog)
    )

    # 5. PreActionTimeoutError lets optional missing click/type skip without state_uncertain abort
    class _StubLocator:
        def __init__(self, mode: str):
            self._mode = mode
            self.first = self

        async def wait_for(self, *, state: str = "visible", timeout: int = 10_000):
            if self._mode == "missing":
                raise TimeoutError("Locator timed out waiting for visible")

        async def click(self, timeout: int = 10_000):
            if self._mode == "post_click_timeout":
                raise TimeoutError("Click timed out after dispatch")

        async def fill(self, text: str, timeout: int = 10_000):
            return None

    class _StubPage:
        def __init__(self):
            self.url = "https://example.test/"
            self.closed = False
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return "Stub"

        async def goto(self, url: str, wait_until: str = "load"):
            self.url = url
            self.events.append(f"goto:{url}")

        def locator(self, selector: str):
            if selector == ".missing-banner":
                return _StubLocator("missing")
            if selector == ".stuck-button":
                return _StubLocator("post_click_timeout")
            return _StubLocator("ok")

    stub_page = _StubPage()
    ctx = SimpleNamespace(pages=[stub_page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)
    ctrl_bl = launcher_mod.BrowserLauncher()
    ctrl_bl._active_sessions["warm-p"] = SimpleNamespace(poll=lambda: None)
    ctrl_bl._controls["warm-p"] = (info["port"], info["token"])
    try:
        opt_ok = ws.add_task(
            "Optional Missing Banner",
            ["warm-p"],
            preset="script",
            script_steps=[
                {"action": "navigate", "url": "https://example.test/start"},
                {
                    "action": "click",
                    "selector": ".missing-banner",
                    "timeout": 500,
                    "optional": True,
                },
                {"action": "navigate", "url": "https://example.test/after"},
            ],
        )
        assert ws.execute_task_sync(opt_ok.id, container.profile_manager, ctrl_bl) is True
        assert opt_ok.status == "completed"
        assert stub_page.events == [
            "goto:https://example.test/start",
            "goto:https://example.test/after",
        ]

        # Post-visibility click timeout STILL aborts as Interrupted (unknown_state=True)
        stub_page.events.clear()
        post_abort = ws.add_task(
            "Post Click Timeout Aborts",
            ["warm-p"],
            preset="script",
            script_steps=[
                {"action": "navigate", "url": "https://example.test/start"},
                {"action": "click", "selector": ".stuck-button", "timeout": 500, "optional": True},
                {"action": "navigate", "url": "https://example.test/never"},
            ],
        )
        assert ws.execute_task_sync(post_abort.id, container.profile_manager, ctrl_bl) is False
        assert post_abort.status == "failed"
        assert "Interrupted" in post_abort.last_result
        assert stub_page.events == ["goto:https://example.test/start"]
    finally:
        ctrl_bl._active_sessions.clear()
        ctrl_bl._controls.clear()
        stop_loop.set()
        srv.join(3)

    # 6. Codex §7 #1/#2/#4 regressions: Gmail guards, safe budget cutoff vs transport/unsettled 504, explicit headless/headed/legacy env
    import urllib.error

    from src.services.browser.launcher import BrowserControlError
    from src.services.warming import scheduler as sched_mod

    orig_threading = sched_mod.threading

    class InstantBudgetEvent:
        def is_set(self):
            return False

        def wait(self, _sec):
            return False

    # 6a. Codex §7 #1, §9 #1/#2 & §11 #1/#2: positive Gmail inbox/message/return verification,
    # session loss stop, empty vs unverified vs cut-short separation, and return-owed budget cutoffs
    signed_out_task = ws.add_task("Gmail Signed Out", ["warm-p"], preset="gmail_trust")
    signed_out_table_task = ws.add_task("Gmail Signed Out Table", ["warm-p"], preset="gmail_trust")
    session_expire_click_task = ws.add_task(
        "Gmail Session Expire On Click", ["warm-p"], preset="gmail_trust"
    )
    session_expire_return_task = ws.add_task(
        "Gmail Session Expire On Return", ["warm-p"], preset="gmail_trust"
    )
    empty_inbox_task = ws.add_task("Gmail Empty Inbox", ["warm-p"], preset="gmail_trust")
    unverified_empty_task = ws.add_task(
        "Gmail Click Miss No Marker", ["warm-p"], preset="gmail_trust"
    )
    noop_click_task = ws.add_task("Gmail Noop Click", ["warm-p"], preset="gmail_trust")
    absent_url_task = ws.add_task("Gmail Absent URL Evidence", ["warm-p"], preset="gmail_trust")
    cutoff_before_click_task = ws.add_task(
        "Gmail Populated Cutoff Before Click", ["warm-p"], preset="gmail_trust", duration_minutes=1
    )
    cutoff_before_return_task = ws.add_task(
        "Gmail Cutoff Before Return", ["warm-p"], preset="gmail_trust", duration_minutes=1
    )
    cutoff_before_return_verify_task = ws.add_task(
        "Gmail Cutoff Before Return Verify", ["warm-p"], preset="gmail_trust", duration_minutes=1
    )
    cutoff_after_return_task = ws.add_task(
        "Gmail Cutoff After Verified Return", ["warm-p"], preset="gmail_trust", duration_minutes=1
    )
    one_mail_task = ws.add_task("Gmail One Mail", ["warm-p"], preset="gmail_trust")

    class GmailStateLauncher:
        def __init__(self, mode: str):
            self.mode = mode
            self.actions: list[str] = []
            self.click_count = 0
            self.nav_count = 0
            self.url = "https://prior.test/previous"

        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            self.actions.append(action)
            p = dict(params or {})
            if action == "navigate":
                self.nav_count += 1
                if self.mode in ("signed_out", "signed_out_table"):
                    self.url = "https://accounts.google.com/signed-out"
                elif self.mode in ("session_expire_click", "session_expire_return") and (
                    self.nav_count >= 2
                ):
                    self.url = "https://mail.google.com/signed-out"
                else:
                    self.url = "https://mail.google.com/mail/u/0/#inbox"
                return {} if self.mode == "absent_url" else {"url": self.url, "title": "Entry"}
            if action == "wait_for":
                sel = str(p.get("selector", ""))
                is_msg_probe = "h2.hP" in sel or "data-message-id" in sel
                if self.mode == "signed_out":
                    raise BrowserControlError(
                        "TimeoutError: waiting for inbox",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                if self.mode == "signed_out_table" and ("tr.zA" in sel or "td.TC" in sel):
                    # Generic div[role='main'] table has neither tr.zA nor td.TC -> must fail
                    raise BrowserControlError(
                        "TimeoutError: generic table is not Gmail inbox",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                if self.mode == "unverified_empty" and "td.TC" in sel and "tr.zA" not in sel:
                    raise BrowserControlError(
                        "TimeoutError: empty inbox marker absent",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                if is_msg_probe and (
                    self.mode in ("noop_click", "session_expire_click")
                    or not self.url.endswith(f"/msg{self.click_count}")
                ):
                    raise BrowserControlError(
                        "TimeoutError: open message view not found",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                if (
                    not is_msg_probe
                    and self.url.endswith("/signed-out")
                    and ("tr.zA" in sel or "td.TC" in sel)
                ):
                    raise BrowserControlError(
                        "TimeoutError: signed-out page is not Gmail inbox",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                return {} if self.mode == "absent_url" else {"url": self.url, "title": "Inbox"}
            if action == "click":
                self.click_count += 1
                if self.mode in ("empty", "unverified_empty"):
                    raise BrowserControlError(
                        "TimeoutError: no message row",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
                if self.mode == "absent_url":
                    return {}
                if self.mode == "noop_click":
                    return {"url": self.url, "title": "Inbox"}
                if self.mode == "session_expire_click":
                    self.url = "https://mail.google.com/signed-out"
                    return {"url": self.url, "title": "Signed out"}
                if self.mode in ("populated", "session_expire_return"):
                    self.url = f"https://mail.google.com/mail/u/0/#inbox/msg{self.click_count}"
                    return {"url": self.url, "title": f"Mail {self.click_count}"}
                if self.mode == "one_mail":
                    if self.click_count == 1:
                        self.url = "https://mail.google.com/mail/u/0/#inbox/msg1"
                        return {"url": self.url, "title": "Mail 1"}
                    raise BrowserControlError(
                        "TimeoutError: no 2nd message",
                        status=422,
                        error_code="action_timeout",
                        unknown_state=False,
                    )
            return {} if self.mode == "absent_url" else {"url": self.url, "title": "Page"}

    bl_signed_out = GmailStateLauncher("signed_out")
    assert (
        ws.execute_task_sync(
            signed_out_task.id, container.profile_manager, bl_signed_out, step_dwell_seconds=0.0
        )
        is False
    )
    assert signed_out_task.status == "failed"
    assert "No messages" not in signed_out_task.last_result
    assert "back" not in bl_signed_out.actions

    bl_signed_out_table = GmailStateLauncher("signed_out_table")
    assert (
        ws.execute_task_sync(
            signed_out_table_task.id,
            container.profile_manager,
            bl_signed_out_table,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert signed_out_table_task.status == "failed"
    assert "No messages" not in signed_out_table_task.last_result
    assert "back" not in bl_signed_out_table.actions

    bl_session_exp_click = GmailStateLauncher("session_expire_click")
    assert (
        ws.execute_task_sync(
            session_expire_click_task.id,
            container.profile_manager,
            bl_session_exp_click,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert session_expire_click_task.status == "failed"
    assert "OK" not in session_expire_click_task.last_result
    assert "No messages" not in session_expire_click_task.last_result
    # Stops immediately after Click 1 message-view verification fails: zero return navigates or 2nd clicks
    assert bl_session_exp_click.actions == [
        "navigate",
        "wait_for",
        "scroll",
        "scroll",
        "click",
        "wait_for",
    ]

    bl_session_exp_ret = GmailStateLauncher("session_expire_return")
    assert (
        ws.execute_task_sync(
            session_expire_return_task.id,
            container.profile_manager,
            bl_session_exp_ret,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert session_expire_return_task.status == "failed"
    assert "OK" not in session_expire_return_task.last_result
    assert bl_session_exp_ret.click_count == 1
    assert bl_session_exp_ret.nav_count == 2

    bl_empty = GmailStateLauncher("empty")
    assert (
        ws.execute_task_sync(
            empty_inbox_task.id, container.profile_manager, bl_empty, step_dwell_seconds=0.0
        )
        is True
    )
    assert empty_inbox_task.status == "completed"
    assert empty_inbox_task.last_result == "No messages (1/1 profiles)"
    assert "back" not in bl_empty.actions
    assert bl_empty.actions.count("wait_for") == 2

    bl_unverified_empty = GmailStateLauncher("unverified_empty")
    assert (
        ws.execute_task_sync(
            unverified_empty_task.id,
            container.profile_manager,
            bl_unverified_empty,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert unverified_empty_task.status == "failed"
    assert "No messages" not in unverified_empty_task.last_result
    assert bl_unverified_empty.nav_count == 1
    assert bl_unverified_empty.click_count == 1

    bl_noop = GmailStateLauncher("noop_click")
    assert (
        ws.execute_task_sync(
            noop_click_task.id, container.profile_manager, bl_noop, step_dwell_seconds=0.0
        )
        is False
    )
    assert noop_click_task.status == "failed"
    assert "No messages" not in noop_click_task.last_result
    assert "back" not in bl_noop.actions
    assert bl_noop.nav_count == 1

    bl_absent = GmailStateLauncher("absent_url")
    assert (
        ws.execute_task_sync(
            absent_url_task.id, container.profile_manager, bl_absent, step_dwell_seconds=0.0
        )
        is False
    )
    assert absent_url_task.status == "failed"
    assert "No messages" not in absent_url_task.last_result

    # Populated inbox cut off before the 1st message click (60s budget): must NOT report No messages or OK
    orig_mono_gmail = sched_mod.time.monotonic
    orig_uniform_gmail = sched_mod.random.uniform
    gmail_clock = [1000.0]

    class _ClockAdvanceEvent:
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            return self._set

        def wait(self, timeout=None):
            if timeout:
                gmail_clock[0] += float(timeout)
            return self._set

    bl_cutoff = GmailStateLauncher("populated")
    monkeypatch.setattr(sched_mod.time, "monotonic", lambda: gmail_clock[0])
    monkeypatch.setattr(
        sched_mod,
        "threading",
        SimpleNamespace(Event=_ClockAdvanceEvent, Thread=orig_threading.Thread),
    )
    monkeypatch.setattr(sched_mod.random, "uniform", lambda _lo, hi: float(hi))
    # Advance clock slightly on each control call so steps 1..7 consume ~50s, triggering safe cutoff before Click 1
    orig_cutoff_ctrl = bl_cutoff.control

    def _slow_cutoff_ctrl(name, action, params=None, timeout=60):
        gmail_clock[0] += 6.5
        return orig_cutoff_ctrl(name, action, params=params, timeout=timeout)

    bl_cutoff.control = _slow_cutoff_ctrl
    try:
        assert (
            ws.execute_task_sync(cutoff_before_click_task.id, container.profile_manager, bl_cutoff)
            is False
        )
        assert cutoff_before_click_task.status == "failed"
        assert "Timed out" in cutoff_before_click_task.last_result
        assert "No messages" not in cutoff_before_click_task.last_result
        assert bl_cutoff.actions == ["navigate", "wait_for", "scroll", "scroll"]

        # Codex §11 #2: 60s budget, first nav 13s, min dwells -> opens /mail/1, 22s left (< 25s) before Return 1 navigate.
        # Safe cutoff must NOT dispatch Return 1 navigate and must report Timed out (not OK) while tab stays on /mail/1.
        gmail_clock[0] = 2000.0
        bl_ret_cutoff = GmailStateLauncher("populated")
        monkeypatch.setattr(sched_mod.random, "uniform", lambda lo, _hi: float(lo))
        orig_ret_ctrl = bl_ret_cutoff.control

        def _nav13_ctrl(name, action, params=None, timeout=60):
            if action == "navigate" and bl_ret_cutoff.nav_count == 0:
                gmail_clock[0] += 13.0
            return orig_ret_ctrl(name, action, params=params, timeout=timeout)

        bl_ret_cutoff.control = _nav13_ctrl
        assert (
            ws.execute_task_sync(
                cutoff_before_return_task.id, container.profile_manager, bl_ret_cutoff
            )
            is False
        )
        assert cutoff_before_return_task.status == "failed"
        assert "Timed out" in cutoff_before_return_task.last_result
        assert "OK" not in cutoff_before_return_task.last_result
        assert bl_ret_cutoff.nav_count == 1
        assert bl_ret_cutoff.url.endswith("/msg1")
        assert bl_ret_cutoff.actions == [
            "navigate",
            "wait_for",
            "scroll",
            "scroll",
            "click",
            "wait_for",
            "scroll",
            "scroll",
            "scroll",
        ]

        # Codex §11 #2 edge: Return 1 navigate runs (leaving 17s < 20s) before Return 1 inbox wait_for -> still Timed out
        gmail_clock[0] = 3000.0
        bl_ret_ver_cutoff = GmailStateLauncher("populated")
        orig_ret_ver_ctrl = bl_ret_ver_cutoff.control

        def _nav_split_ctrl(name, action, params=None, timeout=60):
            if action == "navigate":
                gmail_clock[0] += 8.0 if bl_ret_ver_cutoff.nav_count == 0 else 10.0
            return orig_ret_ver_ctrl(name, action, params=params, timeout=timeout)

        bl_ret_ver_cutoff.control = _nav_split_ctrl
        assert (
            ws.execute_task_sync(
                cutoff_before_return_verify_task.id,
                container.profile_manager,
                bl_ret_ver_cutoff,
            )
            is False
        )
        assert cutoff_before_return_verify_task.status == "failed"
        assert "Timed out" in cutoff_before_return_verify_task.last_result
        assert bl_ret_ver_cutoff.nav_count == 2
        assert bl_ret_ver_cutoff.actions[-1] == "navigate"

        # Positive counterpart: budget runs out safely AFTER Return 1 inbox wait_for verified -> OK!
        gmail_clock[0] = 4000.0
        bl_after_ret = GmailStateLauncher("populated")
        orig_after_ret_ctrl = bl_after_ret.control

        def _after_ret_ctrl(name, action, params=None, timeout=60):
            # 12s pre-click + 13s msg1 + 8s nav1 + 5s post-return dwells = 38s; add 12s on return wait_for -> 10s left (< 13s before Click 2)
            if action == "navigate" and bl_after_ret.nav_count == 0:
                gmail_clock[0] += 8.0
            elif action == "wait_for" and bl_after_ret.nav_count == 2:
                gmail_clock[0] += 12.0
            return orig_after_ret_ctrl(name, action, params=params, timeout=timeout)

        bl_after_ret.control = _after_ret_ctrl
        assert (
            ws.execute_task_sync(
                cutoff_after_return_task.id, container.profile_manager, bl_after_ret
            )
            is True
        )
        assert cutoff_after_return_task.status == "completed"
        assert cutoff_after_return_task.last_result == "OK (1/1 profiles)"
        assert bl_after_ret.click_count == 1
        assert bl_after_ret.url.endswith("#inbox")

        # Gatekeeper P2 #1A: Click 2 action_timeout -> inbox-intact probe hits budget_cutoff -> Timed out (not OK)
        cutoff_click2_probe_task = ws.add_task(
            "Gmail Click2 Probe Cutoff", ["warm-p"], preset="gmail_trust", duration_minutes=1
        )
        gmail_clock[0] = 5000.0
        bl_c2_probe = GmailStateLauncher("one_mail")
        orig_c2_ctrl = bl_c2_probe.control

        def _c2_probe_ctrl(name, action, params=None, timeout=60):
            # 12s + 13s = 25s before Return 1 (35s left >= 25s/20s). Return 1 wait_for adds 16.5s,
            # plus 5s post-return dwells -> 13.5s left before Click 2 (>= 13s).
            # Click 2 times out after 8s -> 5.5s left (< 13s Step 23 inbox-reverify) -> budget_cutoff without probing.
            if action == "wait_for" and bl_c2_probe.nav_count == 2:
                gmail_clock[0] += 16.5
            elif action == "click" and bl_c2_probe.click_count == 1:
                gmail_clock[0] += 8.0
            return orig_c2_ctrl(name, action, params=params, timeout=timeout)

        bl_c2_probe.control = _c2_probe_ctrl
        assert (
            ws.execute_task_sync(
                cutoff_click2_probe_task.id, container.profile_manager, bl_c2_probe
            )
            is False
        )
        assert cutoff_click2_probe_task.status == "failed"
        assert "Timed out" in cutoff_click2_probe_task.last_result
        assert "OK" not in cutoff_click2_probe_task.last_result
        assert bl_c2_probe.actions[-1] == "click"
    finally:
        monkeypatch.setattr(sched_mod.time, "monotonic", orig_mono_gmail)
        monkeypatch.setattr(sched_mod.random, "uniform", orig_uniform_gmail)
        monkeypatch.setattr(sched_mod, "threading", orig_threading)

    # Gatekeeper P2 #1B: Cross-origin redirect observed on non-click step (scroll after Return 1) fails immediately
    scroll_drift_task = ws.add_task("Gmail Scroll Origin Drift", ["warm-p"], preset="gmail_trust")
    bl_drift = GmailStateLauncher("populated")
    orig_drift_ctrl = bl_drift.control

    def _drift_ctrl(name, action, params=None, timeout=60):
        res = orig_drift_ctrl(name, action, params=params, timeout=timeout)
        if action == "scroll" and bl_drift.nav_count == 2:
            return {"url": "https://accounts.google.com/v3/signin", "title": "Sign in"}
        return res

    bl_drift.control = _drift_ctrl
    assert (
        ws.execute_task_sync(
            scroll_drift_task.id, container.profile_manager, bl_drift, step_dwell_seconds=0.0
        )
        is False
    )
    assert scroll_drift_task.status == "failed"
    assert "OK" not in scroll_drift_task.last_result
    assert bl_drift.click_count == 1

    # Codex §13 #2: Same-origin route change observed on final scroll after Return 2 fails immediately (never OK)
    final_scroll_drift_task = ws.add_task(
        "Gmail Final Scroll Same-Origin Drift", ["warm-p"], preset="gmail_trust"
    )
    bl_final_drift = GmailStateLauncher("populated")
    orig_final_drift_ctrl = bl_final_drift.control

    def _final_drift_ctrl(name, action, params=None, timeout=60):
        res = orig_final_drift_ctrl(name, action, params=params, timeout=timeout)
        if action == "scroll" and bl_final_drift.nav_count == 3:
            return {"url": "https://mail.google.com/mail/u/0/#inbox/other-msg", "title": "Other"}
        return res

    bl_final_drift.control = _final_drift_ctrl
    assert (
        ws.execute_task_sync(
            final_scroll_drift_task.id,
            container.profile_manager,
            bl_final_drift,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert final_scroll_drift_task.status == "failed"
    assert "OK" not in final_scroll_drift_task.last_result
    assert bl_final_drift.nav_count == 3
    assert bl_final_drift.actions[-1] == "scroll"

    # Codex §13 #1: Exported Gmail catalog template copied to preset="script" includes post-click message verification
    exported_gmail = next(
        s["sample_steps"] for s in sched_mod.list_warming_scenarios() if s["id"] == "gmail_trust"
    )
    copied_script_redir_task = ws.add_task(
        "Copied Gmail Script Auth Redirect",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail,
    )
    bl_copied_redir = GmailStateLauncher("session_expire_click")
    assert (
        ws.execute_task_sync(
            copied_script_redir_task.id,
            container.profile_manager,
            bl_copied_redir,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert copied_script_redir_task.status == "failed"
    assert "OK" not in copied_script_redir_task.last_result
    assert bl_copied_redir.nav_count == 1
    assert bl_copied_redir.actions == [
        "navigate",
        "wait_for",
        "scroll",
        "scroll",
        "click",
        "wait_for",
    ]

    # Codex §15 #1: Copied Gmail catalog script where Click 2 executes without opening a message (stays on /inbox)
    # must tighten Step 23 strictly to _GMAIL_MESSAGE_VIEW_SELECTOR (never accepting stale tr.zA) with zero follow-up scrolls.
    copied_c2_pending_task = ws.add_task(
        "Copied Gmail Script Click2 Pending",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail,
    )
    bl_copied_c2_pending = GmailStateLauncher("populated")
    orig_c2_pending_ctrl = bl_copied_c2_pending.control
    c2_verify_selectors: list[str] = []

    def _c2_pending_ctrl(name, action, params=None, timeout=60):
        p = dict(params or {})
        if action == "click" and bl_copied_c2_pending.click_count == 1:
            bl_copied_c2_pending.actions.append(action)
            bl_copied_c2_pending.click_count += 1
            # Click 2 executes but leaves tab on #inbox without opening msg2
            return {"url": bl_copied_c2_pending.url, "title": "Inbox"}
        if action == "wait_for" and bl_copied_c2_pending.click_count == 2:
            c2_verify_selectors.append(str(p.get("selector", "")))
        return orig_c2_pending_ctrl(name, action, params=params, timeout=timeout)

    bl_copied_c2_pending.control = _c2_pending_ctrl
    assert (
        ws.execute_task_sync(
            copied_c2_pending_task.id,
            container.profile_manager,
            bl_copied_c2_pending,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert copied_c2_pending_task.status == "failed"
    assert "OK" not in copied_c2_pending_task.last_result
    assert bl_copied_c2_pending.nav_count == 2
    assert bl_copied_c2_pending.actions[-2:] == ["click", "wait_for"]
    assert len(c2_verify_selectors) == 1 and "tr.zA" not in c2_verify_selectors[0]

    # Codex §15 #1: Copied Gmail catalog script on 1-email inbox (Click 2 confirmed not dispatched via action_timeout)
    # accepts inbox fallback cleanly, whereas action_error on Click 2 does NOT fall back to stale inbox.
    copied_one_mail_task = ws.add_task(
        "Copied Gmail Script One Mail",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail,
    )
    bl_copied_one = GmailStateLauncher("one_mail")
    assert (
        ws.execute_task_sync(
            copied_one_mail_task.id,
            container.profile_manager,
            bl_copied_one,
            step_dwell_seconds=0.0,
        )
        is True
    )
    assert copied_one_mail_task.status == "completed"
    assert copied_one_mail_task.last_result == "OK (1/1 profiles)"

    copied_c2_err_task = ws.add_task(
        "Copied Gmail Script Click2 ActionError",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail,
    )
    bl_copied_c2_err = GmailStateLauncher("populated")
    orig_c2_err_ctrl = bl_copied_c2_err.control

    def _c2_err_ctrl(name, action, params=None, timeout=60):
        if action == "click" and bl_copied_c2_err.click_count == 1:
            bl_copied_c2_err.actions.append(action)
            bl_copied_c2_err.click_count += 1
            raise BrowserControlError(
                "Error: detached node",
                status=422,
                error_code="action_error",
                unknown_state=False,
            )
        return orig_c2_err_ctrl(name, action, params=params, timeout=timeout)

    bl_copied_c2_err.control = _c2_err_ctrl
    assert (
        ws.execute_task_sync(
            copied_c2_err_task.id,
            container.profile_manager,
            bl_copied_c2_err,
            step_dwell_seconds=0.0,
        )
        is False
    )
    assert copied_c2_err_task.status == "failed"
    assert bl_copied_c2_err.nav_count == 2

    # Codex §17 #1: Intervening no-dispatch wait between Click 2 and its verifier preserves confirmed missing-row
    # fallback on 1-email inbox (including real deadline path step_dwell_seconds=None), keeps executed/uncertain
    # Click 2 fail-closed, and still invalidates pending state if a real browser action runs in between.
    exported_gmail_wait_zero = []
    for st in exported_gmail:
        s = {"action": "wait", "seconds": 0} if st["action"] == "wait" else dict(st)
        if (
            s["action"] == "wait_for"
            and s.get("selector") == sched_mod._GMAIL_CLICK2_VERIFY_SELECTOR
        ):
            exported_gmail_wait_zero.append({"action": "wait", "seconds": 0})
        exported_gmail_wait_zero.append(s)
    copied_one_mail_wait_task = ws.add_task(
        "Copied Gmail Script One Mail With Wait",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail_wait_zero,
    )
    bl_copied_one_wait = GmailStateLauncher("one_mail")
    assert (
        ws.execute_task_sync(
            copied_one_mail_wait_task.id,
            container.profile_manager,
            bl_copied_one_wait,
        )
        is True
    )
    assert copied_one_mail_wait_task.status == "completed"
    assert copied_one_mail_wait_task.last_result == "OK (1/1 profiles)"

    copied_c2_pending_wait_task = ws.add_task(
        "Copied Gmail Script Click2 Pending With Wait",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail_wait_zero,
    )
    bl_copied_c2_pending_wait = GmailStateLauncher("populated")
    orig_c2_pending_wait_ctrl = bl_copied_c2_pending_wait.control
    c2_wait_verify_selectors: list[str] = []

    def _c2_pending_wait_ctrl(name, action, params=None, timeout=60):
        p = dict(params or {})
        if action == "click" and bl_copied_c2_pending_wait.click_count == 1:
            bl_copied_c2_pending_wait.actions.append(action)
            bl_copied_c2_pending_wait.click_count += 1
            return {"url": bl_copied_c2_pending_wait.url, "title": "Inbox"}
        if action == "wait_for" and bl_copied_c2_pending_wait.click_count == 2:
            c2_wait_verify_selectors.append(str(p.get("selector", "")))
        return orig_c2_pending_wait_ctrl(name, action, params=params, timeout=timeout)

    bl_copied_c2_pending_wait.control = _c2_pending_wait_ctrl
    assert (
        ws.execute_task_sync(
            copied_c2_pending_wait_task.id,
            container.profile_manager,
            bl_copied_c2_pending_wait,
        )
        is False
    )
    assert copied_c2_pending_wait_task.status == "failed"
    assert "OK" not in copied_c2_pending_wait_task.last_result
    assert bl_copied_c2_pending_wait.nav_count == 2
    assert bl_copied_c2_pending_wait.actions[-2:] == ["click", "wait_for"]
    assert len(c2_wait_verify_selectors) == 1 and "tr.zA" not in c2_wait_verify_selectors[0]

    # Uncertain click (action_error) + intervening wait must also fail closed even if a stale message selector appears on same URL.
    copied_c2_err_wait_task = ws.add_task(
        "Copied Gmail Script Click2 ActionError With Wait",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail_wait_zero,
    )
    bl_copied_c2_err_wait = GmailStateLauncher("populated")
    orig_c2_err_wait_ctrl = bl_copied_c2_err_wait.control

    def _c2_err_wait_ctrl(name, action, params=None, timeout=60):
        if action == "click" and bl_copied_c2_err_wait.click_count == 1:
            bl_copied_c2_err_wait.actions.append(action)
            bl_copied_c2_err_wait.click_count += 1
            raise BrowserControlError(
                "Error: detached node",
                status=422,
                error_code="action_error",
                unknown_state=False,
            )
        if action == "wait_for" and bl_copied_c2_err_wait.click_count == 2:
            # Even if a message-view selector matched on the same inbox URL, URL-change check rejects it.
            bl_copied_c2_err_wait.actions.append(action)
            return {"url": bl_copied_c2_err_wait.url, "title": "Inbox"}
        return orig_c2_err_wait_ctrl(name, action, params=params, timeout=timeout)

    bl_copied_c2_err_wait.control = _c2_err_wait_ctrl
    assert (
        ws.execute_task_sync(
            copied_c2_err_wait_task.id,
            container.profile_manager,
            bl_copied_c2_err_wait,
        )
        is False
    )
    assert copied_c2_err_wait_task.status == "failed"
    assert bl_copied_c2_err_wait.nav_count == 2

    # A real intervening browser action (scroll) between missed Click 2 and verifier clears pending_inbox_reverify.
    exported_gmail_scroll_between = []
    for st in exported_gmail:
        s = {"action": "wait", "seconds": 0} if st["action"] == "wait" else dict(st)
        if (
            s["action"] == "wait_for"
            and s.get("selector") == sched_mod._GMAIL_CLICK2_VERIFY_SELECTOR
        ):
            exported_gmail_scroll_between.append({"action": "scroll", "delta_y": 120})
        exported_gmail_scroll_between.append(s)
    copied_one_mail_scroll_task = ws.add_task(
        "Copied Gmail Script One Mail With Intervening Scroll",
        ["warm-p"],
        preset="script",
        script_steps=exported_gmail_scroll_between,
    )
    bl_copied_one_scroll = GmailStateLauncher("one_mail")
    assert (
        ws.execute_task_sync(
            copied_one_mail_scroll_task.id,
            container.profile_manager,
            bl_copied_one_scroll,
        )
        is False
    )
    assert copied_one_mail_scroll_task.status == "failed"
    assert bl_copied_one_scroll.nav_count == 2

    # Gatekeeper P2 #2 (Engram #9501): Stop arriving during Click 1 dispatches ZERO post-Stop wait_for probe
    stop_click_task = ws.add_task("Gmail Stop During Click", ["warm-p"], preset="gmail_trust")
    bl_stop_click = GmailStateLauncher("populated")
    orig_stop_ctrl = bl_stop_click.control

    def _stop_on_click_ctrl(name, action, params=None, timeout=60):
        res = orig_stop_ctrl(name, action, params=params, timeout=timeout)
        if action == "click":
            ws.stop_task(stop_click_task.id)
        return res

    bl_stop_click.control = _stop_on_click_ctrl
    assert (
        ws.execute_task_sync(
            stop_click_task.id, container.profile_manager, bl_stop_click, step_dwell_seconds=0.0
        )
        is False
    )
    assert stop_click_task.status == "idle"
    assert stop_click_task.last_result == "Stopped (0/1)"
    assert bl_stop_click.actions == ["navigate", "wait_for", "scroll", "scroll", "click"]

    bl_one = GmailStateLauncher("one_mail")
    assert (
        ws.execute_task_sync(
            one_mail_task.id, container.profile_manager, bl_one, step_dwell_seconds=0.0
        )
        is True
    )
    assert one_mail_task.status == "completed"
    assert one_mail_task.last_result == "OK (1/1 profiles)"
    assert "back" not in bl_one.actions
    assert bl_one.actions.count("navigate") == 3

    # 6b. Codex §7 #2: Immediate URLError (504 transport_timeout) & unsettled 504 stay non-success; only safe boundary / deadline_expired reports OK
    safe_stop_task = ws.add_task(
        "Builtin Safe Stop", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )
    conn_refused_task = ws.add_task(
        "Builtin Immediate URLError", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )
    unsettled_504_task = ws.add_task(
        "Builtin Unsettled 504", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )
    deadline_exp_early_task = ws.add_task(
        "Builtin Deadline Expired Early", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )
    deadline_exp_elapsed_task = ws.add_task(
        "Builtin Deadline Expired Elapsed", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )
    tab_close_task = ws.add_task(
        "Builtin Tab Close 422", ["warm-p"], preset="youtube_trust", duration_minutes=1
    )

    class SafeStopLauncher:
        def __init__(self):
            self.timeouts: list[float] = []

        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            self.timeouts.append(timeout)
            return {}

    class Unsettled504Launcher:
        def __init__(self):
            self.calls = 0

        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            self.calls += 1
            if self.calls == 2:
                raise BrowserControlError(
                    "TimeoutError: Action timed out",
                    status=504,
                    error_code="state_uncertain",
                    unknown_state=True,
                    settled=False,
                )
            return {}

    class DeadlineExpiredLauncher:
        def __init__(self, *, advance_clock=None):
            self.calls = 0
            self._advance_clock = advance_clock

        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            self.calls += 1
            if self.calls == 2:
                if self._advance_clock is not None:
                    self._advance_clock()
                raise BrowserControlError(
                    "TimeoutError: Deadline expired before start",
                    status=504,
                    error_code="deadline_expired",
                    unknown_state=False,
                    settled=True,
                )
            return {}

    class TabClose422Launcher:
        def __init__(self):
            self.calls = 0

        def is_running(self, _name):
            return True

        def control(self, _name, action, params=None, timeout=60):
            self.calls += 1
            if self.calls == 2:
                raise BrowserControlError(
                    "TargetClosedError: page closed",
                    status=422,
                    error_code="state_uncertain",
                    unknown_state=True,
                )
            return {}

    # Real BrowserLauncher.control mapping immediate URLError(ConnectionRefusedError) -> 504 transport_timeout with budget remaining
    refused_bl = launcher_mod.BrowserLauncher()
    refused_bl._active_sessions["warm-p"] = SimpleNamespace(poll=lambda: None)
    refused_bl._controls["warm-p"] = (1, "tok")
    req_calls = [0]

    class _FakeHttpResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"ok": true, "result": {"url": "https://news.google.com", "title": "N"}, '
                b'"page_id": "p1"}'
            )

    def _fake_urlopen(req, timeout=None):
        req_calls[0] += 1
        if req_calls[0] == 1:
            return _FakeHttpResp()
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(launcher_mod.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        sched_mod,
        "threading",
        SimpleNamespace(Event=InstantBudgetEvent, Thread=orig_threading.Thread),
    )
    monkeypatch.setattr(sched_mod.random, "uniform", lambda lo, hi: float(hi))
    try:
        # True safe pre-action budget stop: never fires a clamped <base_ctrl_timeout call, finishes OK
        s_bl = SafeStopLauncher()
        assert ws.execute_task_sync(safe_stop_task.id, container.profile_manager, s_bl) is True
        assert safe_stop_task.status == "completed"
        assert "OK (1/1 profiles)" in safe_stop_task.last_result
        assert all(t >= 10.0 for t in s_bl.timeouts)

        # Immediate URLError via real BrowserLauncher.control with budget left -> Interrupted (NEVER OK)
        assert (
            ws.execute_task_sync(conn_refused_task.id, container.profile_manager, refused_bl)
            is False
        )
        assert conn_refused_task.status == "failed"
        assert "Interrupted" in conn_refused_task.last_result

        # Ambiguous unsettled 504 mid-flight -> Interrupted (NEVER OK)
        u_bl = Unsettled504Launcher()
        assert ws.execute_task_sync(unsettled_504_task.id, container.profile_manager, u_bl) is False
        assert unsettled_504_task.status == "failed"
        assert "Interrupted" in unsettled_504_task.last_result
        assert u_bl.calls == 2

        # deadline_expired with budget STILL remaining -> fails as Timed out (NEVER OK)
        d_early_bl = DeadlineExpiredLauncher()
        assert (
            ws.execute_task_sync(deadline_exp_early_task.id, container.profile_manager, d_early_bl)
            is False
        )
        assert deadline_exp_early_task.status == "failed"
        assert "Timed out" in deadline_exp_early_task.last_result

        # Confirmed no-dispatch deadline_expired (unknown_state=False, settled=True) AFTER wall deadline elapsed -> safe OK
        fake_now = [1000.0]
        orig_mono = sched_mod.time.monotonic
        monkeypatch.setattr(sched_mod.time, "monotonic", lambda: fake_now[0])
        try:
            d_elapsed_bl = DeadlineExpiredLauncher(
                advance_clock=lambda: fake_now.__setitem__(0, 1061.0)
            )
            assert (
                ws.execute_task_sync(
                    deadline_exp_elapsed_task.id, container.profile_manager, d_elapsed_bl
                )
                is True
            )
            assert deadline_exp_elapsed_task.status == "completed"
            assert "OK (1/1 profiles)" in deadline_exp_elapsed_task.last_result
        finally:
            monkeypatch.setattr(sched_mod.time, "monotonic", orig_mono)

        # 422 tab close -> Interrupted
        tc_bl = TabClose422Launcher()
        assert ws.execute_task_sync(tab_close_task.id, container.profile_manager, tc_bl) is False
        assert tab_close_task.status == "failed"
        assert "Interrupted" in tab_close_task.last_result
        assert tc_bl.calls == 2
    finally:
        refused_bl._active_sessions.clear()
        refused_bl._controls.clear()
        monkeypatch.setattr(sched_mod, "threading", orig_threading)

    # 6c. Codex §7 #4: Scheduler -> BrowserLauncher -> spawn_browser child env for explicit True, explicit False (headed & display-less), legacy None, and already-running reuse
    monkeypatch.setenv("FOXPROFILE_HEADLESS", "true")
    popen_envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda _cmd, **kw: popen_envs.append(kw["env"]) or SimpleNamespace(poll=lambda: None),
    )
    monkeypatch.setattr(launcher_mod, "spawn_browser", process.spawn_browser)
    monkeypatch.setattr(launcher_mod, "wait_for_exit", lambda *a: None)

    def _run_launch_case(task_obj, *, headed_host: bool, already_open: bool = False):
        popen_envs.clear()
        monkeypatch.setattr(sched_mod, "host_supports_headed", lambda: headed_host)
        env_bl = launcher_mod.BrowserLauncher()
        monkeypatch.setattr(env_bl, "_monitor_process", lambda *a: None)
        monkeypatch.setattr(env_bl, "wait_for_launch", lambda _n, timeout=45: (True, None))
        monkeypatch.setattr(env_bl, "control", lambda *a, **kw: ({"url": "https://a.test"}, "pid1"))
        monkeypatch.setattr(env_bl, "stop_profile", lambda _n, timeout=2: True)
        if already_open:
            env_bl._active_sessions["warm-p"] = SimpleNamespace(poll=lambda: None)
        case_logs: list[str] = []
        try:
            ws.execute_task_sync(
                task_obj.id,
                container.profile_manager,
                env_bl,
                log_fn=case_logs.append,
                step_dwell_seconds=0.0,
            )
            return list(popen_envs), case_logs
        finally:
            env_bl._active_sessions.clear()

    t_exp_true = ws.add_task(
        "Env True", ["warm-p"], preset="custom", custom_urls=["https://a.test"], headless=True
    )
    t_exp_false = ws.add_task(
        "Env False", ["warm-p"], preset="custom", custom_urls=["https://a.test"], headless=False
    )
    t_legacy = ws.add_task(
        "Env Legacy", ["warm-p"], preset="custom", custom_urls=["https://a.test"]
    )
    assert t_legacy.headless is None

    envs_true, _ = _run_launch_case(t_exp_true, headed_host=True)
    assert envs_true[-1]["FOXPROFILE_HEADLESS"] == "true"

    envs_false_headed, _ = _run_launch_case(t_exp_false, headed_host=True)
    assert envs_false_headed[-1]["FOXPROFILE_HEADLESS"] == "false"

    envs_false_nodisp, logs_nodisp = _run_launch_case(t_exp_false, headed_host=False)
    assert envs_false_nodisp[-1]["FOXPROFILE_HEADLESS"] == "true"
    assert any("no display" in line.lower() for line in logs_nodisp)

    # Display-less host with parent FOXPROFILE_HEADLESS unset/false forces "true"; "virtual" is preserved
    monkeypatch.delenv("FOXPROFILE_HEADLESS", raising=False)
    envs_false_nodisp_unset, _ = _run_launch_case(t_exp_false, headed_host=False)
    assert envs_false_nodisp_unset[-1]["FOXPROFILE_HEADLESS"] == "true"

    monkeypatch.setenv("FOXPROFILE_HEADLESS", "false")
    envs_false_nodisp_false, _ = _run_launch_case(t_exp_false, headed_host=False)
    assert envs_false_nodisp_false[-1]["FOXPROFILE_HEADLESS"] == "true"

    monkeypatch.setenv("FOXPROFILE_HEADLESS", "virtual")
    envs_false_nodisp_virt, _ = _run_launch_case(t_exp_false, headed_host=False)
    assert envs_false_nodisp_virt[-1]["FOXPROFILE_HEADLESS"] == "virtual"

    monkeypatch.setenv("FOXPROFILE_HEADLESS", "true")
    envs_legacy, _ = _run_launch_case(t_legacy, headed_host=True)
    assert envs_legacy[-1]["FOXPROFILE_HEADLESS"] == "true"

    envs_reuse, logs_reuse = _run_launch_case(t_exp_false, headed_host=True, already_open=True)
    assert envs_reuse == []
    assert any("already running" in line.lower() for line in logs_reuse)

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        info = client.get("/api/v1/warming/tasks").json()
        assert isinstance(info["host_supports_headed"], bool)

    # Codex §9 #3: Desktop dialog on display-less host locks Hide checkbox and never labels saved headless=False as Visible
    from src.ui.dialogs import warming as warming_dlg_mod

    monkeypatch.setattr(warming_dlg_mod, "host_supports_headed", lambda: False)
    page_nodisp = _DummyPage()
    open_warming_dialog(page_nodisp, ws, container.profile_manager, SimpleNamespace())
    nodisp_ctrls = list(_walk_controls(page_nodisp.dialog))
    forced_cb = next(
        c
        for c in nodisp_ctrls
        if isinstance(c, ft.Checkbox) and c.label == get_string("warming_hide_browser_forced")
    )
    assert forced_cb.value is True and forced_cb.disabled is True
    nodisp_texts = [c.value for c in nodisp_ctrls if isinstance(c, ft.Text)]
    assert get_string("warming_headless_no_display_badge") in nodisp_texts
    assert get_string("warming_visible_badge") not in nodisp_texts
