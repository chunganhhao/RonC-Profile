import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.services.browser.launcher import BrowserLauncher, ProfileBusyError, _LaunchResult
from src.services.profile import manager
from src.services.profile.transfer import import_from_zip


def _archive(path, name, files):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("profile.json", json.dumps({"name": name, "os_type": "windows"}))
        for arcname, data in files.items():
            z.writestr(arcname, data)
    return str(path)


def test_import_rejects_traversal_profile_name(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "..", {})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "name" in msg


def test_import_rejects_zip_slip_entry(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/../../escaped.txt": "x"})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "Unsafe path" in msg
    assert not (tmp_path / "escaped.txt").exists()


def test_import_extracts_normal_archive(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/fingerprint.json": "{}"})
    ok, profile = import_from_zip(zip_path, str(tmp_path / "data"))
    assert ok and profile.name == "good"
    assert (tmp_path / "data" / "good" / "fingerprint.json").read_text() == "{}"


def test_exclusive_blocks_second_holder():
    bl = BrowserLauncher()
    with bl.exclusive("p"), pytest.raises(ProfileBusyError), bl.exclusive("p"):
        pass
    with bl.exclusive("p"):  # released after the block
        pass


def test_launch_result_settles_once():
    r = _LaunchResult()
    r.settle(True)
    r.settle(False, "late failure")
    assert r.ok is True and r.error is None


@pytest.mark.parametrize("destination", ["outside", "profile", "nested"])
def test_export_api_does_not_archive_its_output(tmp_path, monkeypatch, destination):
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("src.api.app.API_TOKEN", "")
    monkeypatch.setattr("src.api.auth.API_TOKEN", "")
    pm = manager.ProfileManager()
    assert pm.add_profile("alice", "", "linux", engine="chrome")
    data = tmp_path / "data" / "alice"
    default = data / "Default"
    default.mkdir()
    (default / "Preferences").write_bytes(b'{"fixture": true}')
    outside = tmp_path / "export"
    outside.mkdir()
    export_dir = {"outside": outside, "profile": data, "nested": default}[destination]

    app = create_app(SimpleNamespace(profile_manager=pm))
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post(
        "/api/v1/profiles/alice/export", json={"export_dir": str(export_dir), "include_data": True}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["success"], result
    archive_path = Path(result["zip_path"])
    assert archive_path.parent == export_dir
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["profile.json", "data/Default/Preferences"]
        assert json.loads(archive.read("profile.json"))["engine"] == "chrome"
        assert archive.read("data/Default/Preferences") == b'{"fixture": true}'


def test_export_without_data_keeps_the_identity(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "data"
    (data / "Default").mkdir(parents=True)
    for name in ("fingerprint.json", "chrome_persona.json", "geo_locale.json"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "Default" / "Cookies").write_text("x", encoding="utf-8")
    ok, zip_path = export_to_zip(Profile(name="alice"), str(data), str(tmp_path), False)
    assert ok
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert {"data/fingerprint.json", "data/chrome_persona.json", "data/geo_locale.json"} <= names
    assert "data/Default/Cookies" not in names
