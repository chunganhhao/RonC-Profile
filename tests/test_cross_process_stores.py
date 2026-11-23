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

from src.api.dependencies import get_browser_launcher, get_profile_manager
from src.api.routes.profiles import router as profiles_router
from src.models.profile import Profile
from src.services.browser.extensions import ExtensionService
from src.services.browser.lease import store_lease
from src.services.profile import manager
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


def test_assigning_an_extension_lets_other_threads_write_the_profile_store(tmp_path):
    ext = ExtensionService(str(tmp_path / "extensions.json"), str(tmp_path / "extensions"))
    alpha = ext.install_from_zip(_archive("Alpha"), filename="alpha.zip")
    written = threading.Event()

    class _Manager:
        def list_profiles(self):
            # add_profile takes the manager's lock, then the profile store lease.
            def write():
                with store_lease(tmp_path / "profiles.json"):
                    written.set()

            threading.Thread(target=write, daemon=True).start()
            assert written.wait(2), "profile store write blocked by the extension assignment"
            return []

        def save_profiles(self):
            pass

    assert ext.assign_profiles(alpha.id, [], _Manager()) == 0


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
