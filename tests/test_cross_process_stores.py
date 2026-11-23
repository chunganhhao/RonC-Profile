"""Two app processes share one store: each write and lookup must see the other's changes."""

import contextlib
import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_profile_manager,
    get_warming_scheduler,
)
from src.api.routes.profiles import router as profiles_router
from src.api.routes.warming import router as warming_router
from src.models.profile import Profile
from src.services.browser import lease
from src.services.browser.extensions import ExtensionService
from src.services.browser.lease import profile_lease, store_lease
from src.services.profile import manager
from src.services.warming import scheduler
from src.services.warming.scheduler import WarmingSchedulerService

ROOT = Path(__file__).resolve().parents[1]


def _archive(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"manifest_version": 3, "name": name, "version": "1"})
        )
    return buf.getvalue()


def test_simultaneous_extension_edits_from_two_processes_keep_both(tmp_path, monkeypatch):
    store, ext_dir = tmp_path / "extensions.json", tmp_path / "extensions"
    ext = ExtensionService(str(store), str(ext_dir))
    alpha = ext.install_from_zip(_archive("Alpha"), filename="alpha.zip")
    beta = ext.install_from_zip(_archive("Beta"), filename="beta.zip")
    peer_script = (
        "import sys\n"
        "from src.services.browser.extensions import ExtensionService\n"
        "ExtensionService(sys.argv[1], sys.argv[2]).update_extension(sys.argv[3], enabled=False)\n"
    )
    peer = None
    real_reload = ExtensionService.reload

    def reload_then_let_peer_write(self):
        nonlocal peer
        real_reload(self)
        if peer is None:
            # The other process edits Alpha after this one read the file and before it writes.
            peer = subprocess.Popen(
                [sys.executable, "-c", peer_script, str(store), str(ext_dir), alpha.id], cwd=ROOT
            )
            with contextlib.suppress(subprocess.TimeoutExpired):
                peer.wait(timeout=2)

    monkeypatch.setattr(ExtensionService, "reload", reload_then_let_peer_write)
    assert ext.update_extension(beta.id, enabled=False).enabled is False
    assert peer.wait(timeout=30) == 0

    saved = {e["name"]: e["enabled"] for e in json.loads(store.read_text())["extensions"]}
    assert saved == {"Alpha": False, "Beta": False}


class _Launcher:
    def __init__(self):
        self.visits: list[str] = []
        self.visited = threading.Event()

    def is_running(self, _name):
        return True

    def control(self, _name, action, payload, timeout=None):
        if action == "navigate":
            self.visits.append(payload["url"])
            self.visited.set()
        return {}


class _Profiles:
    profiles = {"warm": Profile(name="warm", engine="chrome")}


def test_assigning_an_extension_lets_other_threads_write_the_profile_store(tmp_path, monkeypatch):
    ext = ExtensionService(str(tmp_path / "extensions.json"), str(tmp_path / "extensions"))
    alpha = ext.install_from_zip(_archive("Alpha"), filename="alpha.zip")
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    pm = manager.ProfileManager()
    written = threading.Event()

    def list_profiles():
        # The extension transaction must not already hold the process-wide store mutex.
        def write():
            with store_lease(manager.PROFILES_FILE):
                written.set()

        threading.Thread(target=write, daemon=True).start()
        assert written.wait(2), "profile store write blocked by the extension assignment"
        return []

    monkeypatch.setattr(pm, "list_profiles", list_profiles)
    assert ext.assign_profiles(alpha.id, [], pm) == 0


def _running_pair(tmp_path):
    path = str(tmp_path / "warming.json")
    owner = WarmingSchedulerService(file_path=path)
    task = owner.add_task(
        "Peer", ["warm"], preset="custom", custom_urls=["https://example.com/"], duration_minutes=5
    )
    peer = WarmingSchedulerService(file_path=path)
    launcher = _Launcher()
    assert owner.run_task_async(task.id, _Profiles(), launcher)
    assert launcher.visited.wait(10)
    return path, owner, peer, task, launcher


def _shared(path, task_id):
    return next(t for t in json.loads(Path(path).read_text()) if t["id"] == task_id)


def test_stop_from_peer_process_ends_the_owners_run_before_answering(tmp_path):
    path, owner, peer, task, launcher = _running_pair(tmp_path)

    assert peer.stop_task(task.id)

    shared = _shared(path, task.id)
    assert (shared["status"], shared["last_result"]) == ("idle", "Stopped (1/1)")
    assert owner.get_task(task.id).status == "idle"
    assert task.id not in owner._cancel_flags
    visits = len(launcher.visits)
    time.sleep(1)
    assert len(launcher.visits) == visits


def test_peer_process_refuses_a_duplicate_run_and_keeps_the_owners_state(tmp_path):
    path, owner, peer, task, launcher = _running_pair(tmp_path)
    peer_launcher = _Launcher()
    try:
        assert owner.run_task_async(task.id, _Profiles(), launcher) is False
        assert peer.run_task_async(task.id, _Profiles(), peer_launcher) is False
        time.sleep(0.5)
        assert peer_launcher.visits == []
        assert _shared(path, task.id)["status"] == "running"
        assert owner.get_task(task.id).status == "running"
    finally:
        owner.stop_task(task.id)
    # The owner's lease is free once its run ends, so a peer can run the task next.
    assert peer.stop_task(task.id)
    assert peer.run_task_async(task.id, _Profiles(), peer_launcher)
    assert peer_launcher.visited.wait(10)
    assert peer.stop_task(task.id)


def _add_task(service, name):
    return service.add_task(
        name, ["warm"], preset="custom", custom_urls=["https://example.com/"], duration_minutes=5
    )


def test_peer_stop_answers_504_while_the_owner_has_not_stopped(tmp_path, monkeypatch):
    path = str(tmp_path / "warming.json")
    owner = WarmingSchedulerService(file_path=path)
    task = _add_task(owner, "Stuck")
    owner._tasks[task.id].status = "running"
    owner._save()
    peer = WarmingSchedulerService(file_path=path)
    monkeypatch.setattr(scheduler, "PEER_STOP_WAIT_SECONDS", 0.3)
    app = FastAPI()
    app.include_router(warming_router, prefix="/api/v1")
    app.dependency_overrides[get_warming_scheduler] = lambda: peer
    app.dependency_overrides[get_event_bus] = lambda: type("EB", (), {"emit": lambda *a: None})()

    # The owner process holds the run lease and does not answer the Stop in time.
    with profile_lease(owner._run_lease_path(task.id)):
        response = TestClient(app).post(f"/api/v1/warming/tasks/{task.id}/stop")

    assert response.status_code == 504
    assert response.json()["detail"] == "Warming task is still stopping in another process"
    assert _shared(path, task.id)["status"] == "idle"


def test_deleting_a_task_removes_its_run_lease_unless_a_run_holds_it(tmp_path):
    service = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    idle, held = _add_task(service, "Idle"), _add_task(service, "Held")
    idle_lock = lease._lease_file(service._run_lease_path(idle.id))
    held_lock = lease._lease_file(service._run_lease_path(held.id))
    with profile_lease(service._run_lease_path(idle.id)):
        pass
    assert idle_lock.exists()

    assert service.delete_task(idle.id)
    with profile_lease(service._run_lease_path(held.id)):
        assert service.delete_task(held.id)
        assert held_lock.exists()

    assert not idle_lock.exists()


def test_named_lookup_sees_a_profile_another_process_created(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    peer = manager.ProfileManager()
    other = manager.ProfileManager()
    assert other.add_profile("NewOnOther", "", "macos")
    app = FastAPI()
    app.include_router(profiles_router, prefix="/api/v1")
    app.dependency_overrides[get_profile_manager] = lambda: peer
    app.dependency_overrides[get_browser_launcher] = lambda: type(
        "BL", (), {"is_running": lambda self, _n: False}
    )()

    assert peer.get_profile("NewOnOther").name == "NewOnOther"
    response = TestClient(app).get("/api/v1/profiles/NewOnOther")
    assert response.status_code == 200
    assert response.json()["name"] == "NewOnOther"
    assert peer.get_profile("Missing") is None
