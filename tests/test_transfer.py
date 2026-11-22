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
    from src.api.routes import profiles as profiles_route

    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(tmp_path / "data"))
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


@pytest.fixture
def import_api(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from src.api.routes import profiles as profiles_route
    from src.core import config as app_config
    from src.core.events import EventBus

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "camoufox_data"))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(tmp_path / "camoufox_data"))
    monkeypatch.setattr(app_config, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(app_config, "DATA_DIR", str(tmp_path / "camoufox_data"))
    app = FastAPI()
    app.include_router(profiles_route.router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        profile_manager=manager.ProfileManager(),
        browser_launcher=BrowserLauncher(),
        event_bus=EventBus(),
    )
    return TestClient(app, raise_server_exceptions=False), app


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "core_version",
        "proxy",
        "os_type",
        "timezone",
        "locale",
        "engine",
        "group",
        "tags",
        "extension_ids",
        "display_order",
        "account_uid",
        "account_password",
        "two_fa_secret",
        "backup_codes",
        "recovery_email",
        "notes",
        "start_url",
        "webrtc_mode",
        "canvas_mode",
    ],
)
@pytest.mark.parametrize("value", [12345, ["windows"], {"bad": "value"}, True, None])
def test_import_bad_field_keeps_api_list_usable(tmp_path, import_api, field, value):
    client, app = import_api
    if field == "display_order" and type(value) is int:
        value = 0
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "profile.json", json.dumps({"name": "bad", "engine": "chrome", field: value})
        )
    response = client.post("/api/v1/profiles/import", json={"zip_path": str(path)})
    listed = client.get("/api/v1/profiles")
    saved = dict(app.state.container.profile_manager.profiles)
    app.state.container.profile_manager = manager.ProfileManager()
    reloaded = client.get("/api/v1/profiles")
    body = response.json()
    assert response.status_code == 200
    assert listed.status_code == reloaded.status_code == 200
    if field in ("group", "account_uid", "two_fa_secret", "recovery_email", "notes"):
        assert body["success"] is (value is None)
    if body["success"]:
        assert body["profile_name"] == "bad"
        assert set(saved) == set(app.state.container.profile_manager.profiles) == {"bad"}
        if field == "account_password":
            assert saved["bad"].account_password == value
    else:
        assert isinstance(body["error"], str) and body["error"]
        assert not saved and not app.state.container.profile_manager.profiles
        assert not (tmp_path / "camoufox_data" / "bad").exists()
    if field == "os_type":
        assert body["success"] is (value is None)
        if value is None:
            assert saved["bad"].os_type == "windows"
        else:
            assert body["error"] == "Invalid os_type in archive (must be text)"


@pytest.mark.parametrize("metadata", [12345, ["windows"], True, None, "text"])
def test_import_nonobject_metadata_keeps_api_list_usable(tmp_path, import_api, metadata):
    client, app = import_api
    path = tmp_path / "nonobject.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("profile.json", json.dumps(metadata))
    response = client.post("/api/v1/profiles/import", json={"zip_path": str(path)})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert not app.state.container.profile_manager.profiles
    assert client.get("/api/v1/profiles").status_code == 200
    app.state.container.profile_manager = manager.ProfileManager()
    assert not app.state.container.profile_manager.profiles
    assert client.get("/api/v1/profiles").status_code == 200
    assert not list((tmp_path / "camoufox_data").iterdir())


def test_import_preserves_nonstandard_string_os_type(tmp_path, import_api):
    client, app = import_api
    path = tmp_path / "custom-os.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "profile.json", json.dumps({"name": "custom", "engine": "chrome", "os_type": "custom"})
        )
    response = client.post("/api/v1/profiles/import", json={"zip_path": str(path)})
    assert response.status_code == 200 and response.json()["success"]
    assert client.get("/api/v1/profiles").status_code == 200
    assert app.state.container.profile_manager.profiles["custom"].os_type == "custom"
    app.state.container.profile_manager = manager.ProfileManager()
    assert client.get("/api/v1/profiles").status_code == 200
    assert app.state.container.profile_manager.profiles["custom"].os_type == "custom"


def test_chromium_export_import_round_trip(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    original = Profile(
        name="chromium",
        engine="chrome",
        os_type="linux",
        proxy="http://localhost:8080",
        timezone="Europe/Paris",
        locale="en-US",
        core_version="148.0.0",
        group="work",
        tags=["work", "personal"],
        extension_ids=["extension"],
        display_order=7,
        account_uid="user",
        account_password="  password  ",
        two_fa_secret="JBSWY3DPEHPK3PXP",
        recovery_email="user@example.com",
        backup_codes="12345678\n87654321",
        notes="notes",
        start_url="https://example.com",
        webrtc_mode="disable",
        canvas_mode="noise",
    )
    ok, path = export_to_zip(original, str(tmp_path / "source"), str(tmp_path))
    assert ok, path
    ok, imported = import_from_zip(path, str(tmp_path / "destination"))
    assert ok, imported
    assert imported.to_dict() == original.to_dict()


@pytest.mark.parametrize("value", [None, "", False, 0, [], {}])
def test_import_legacy_empty_fields(tmp_path, value):
    path = tmp_path / "empty.zip"
    metadata = {"name": "legacy", "engine": "chrome", "proxy": "None"}
    metadata.update(
        dict.fromkeys(
            (
                "group",
                "account_uid",
                "account_password",
                "two_fa_secret",
                "recovery_email",
                "notes",
            ),
            value,
        )
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("profile.json", json.dumps(metadata))
    ok, profile = import_from_zip(str(path), str(tmp_path / "data"))
    assert ok, profile
    assert profile.proxy is None
    assert profile.os_type == "windows"


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
