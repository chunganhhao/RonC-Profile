import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from src.api.routes import profiles
from src.api.routes.profiles import router
from src.services.profile import manager

VALUES = {
    "notes": "Saved note",
    "account_uid": "fixture-user",
    "start_url": "https://example.test/start",
    "tags": ["Fixture", "Saved"],
}


@pytest.fixture
def profile_api(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    pm = manager.ProfileManager()
    assert pm.add_profile("fixture", "", "linux", **VALUES)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_profile_manager] = lambda: pm
    app.dependency_overrides[get_browser_launcher] = lambda: SimpleNamespace(
        is_running=lambda _: False
    )
    app.dependency_overrides[get_event_bus] = lambda: SimpleNamespace(emit=lambda: None)
    return pm, TestClient(app)


@pytest.mark.parametrize("field", VALUES)
@pytest.mark.parametrize("operation", ["null", "absent", "empty", "value"])
def test_patch_optional_field_presence_and_persistence(profile_api, field, operation):
    pm, client = profile_api
    empty = [] if field == "tags" else ""
    replacement = (
        ["Updated"]
        if field == "tags"
        else ("https://example.test/updated" if field == "start_url" else "Updated value")
    )
    body = (
        {}
        if operation == "absent"
        else {field: {"null": None, "empty": empty, "value": replacement}[operation]}
    )
    expected = dict(VALUES)
    if operation in ("null", "empty"):
        expected[field] = [] if field == "tags" else None
    elif operation == "value":
        expected[field] = replacement
    before = client.get("/api/v1/profiles/fixture").json()
    response = client.patch("/api/v1/profiles/fixture", json=body)
    assert response.status_code == 200, response.text
    assert response.json().keys() == before.keys()
    for key, value in expected.items():
        assert response.json()[key] == value
        assert getattr(pm.profiles["fixture"], key) == value
    assert response.json()["group"] == (expected["tags"][0] if expected["tags"] else None)
    persisted = json.loads(Path(manager.PROFILES_FILE).read_text())["fixture"]
    reloaded = manager.ProfileManager().profiles["fixture"]
    for key, value in expected.items():
        assert persisted[key] == value
        assert getattr(reloaded, key) == value
    assert client.get("/api/v1/profiles/fixture").json() == response.json()


def test_manager_none_keeps_desktop_update_semantics(profile_api):
    pm, _ = profile_api
    assert pm.update_profile("fixture", "fixture", "", "linux", **dict.fromkeys(VALUES))
    reloaded = manager.ProfileManager().profiles["fixture"]
    for field, value in VALUES.items():
        assert getattr(reloaded, field) == value


def test_patch_null_matches_new_profile_defaults_and_clears_legacy_group(profile_api):
    pm, client = profile_api
    assert pm.add_profile("defaults", "", "linux")
    assert pm.add_profile("legacy", "", "linux", group="Legacy")
    default = pm.profiles["defaults"]
    response = client.patch("/api/v1/profiles/fixture", json=dict.fromkeys(VALUES))
    assert response.status_code == 200
    for field in VALUES:
        assert response.json()[field] == getattr(default, field)
    response = client.patch("/api/v1/profiles/legacy", json={"tags": None})
    assert response.status_code == 200
    assert response.json()["tags"] == []
    assert response.json()["group"] is None
    assert manager.ProfileManager().profiles["legacy"].effective_tags == []


def test_concurrent_partial_patches_keep_both_fields(profile_api, monkeypatch):
    pm, client = profile_api
    assert pm.add_profile("chromium", "", "macos", engine="chrome")
    path = "/api/v1/profiles/chromium"
    same_read = Barrier(2, timeout=5)
    require_profile = profiles.require_profile

    def read_before_either_write(name, pm):
        snapshot = replace(require_profile(name, pm))
        same_read.wait()
        return snapshot

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(profiles, "require_profile", read_before_either_write)
        timezone_patch = pool.submit(client.patch, path, json={"timezone": "Asia/Ho_Chi_Minh"})
        locale_patch = pool.submit(client.patch, path, json={"locale": "vi-VN"})
        for pending in (timezone_patch, locale_patch):
            response = pending.result()
            assert response.status_code == 200, response.text

    response = client.get(path).json()
    persisted = manager.ProfileManager().get_profile("chromium")
    assert persisted is not None
    assert response["timezone"] == persisted.timezone == "Asia/Ho_Chi_Minh"
    assert response["locale"] == persisted.locale == "vi-VN"


def test_patch_null_clears_proxy_and_geo_without_replacing_other_fields(profile_api):
    pm, client = profile_api
    name = "chromium"
    assert pm.add_profile(
        name, "192.0.2.10:8080", "macos", "Asia/Ho_Chi_Minh", "vi-VN", engine="chrome"
    )
    path = f"/api/v1/profiles/{name}"
    for field, retained in (
        ("proxy", {"timezone": "Asia/Ho_Chi_Minh", "locale": "vi-VN"}),
        ("timezone", {"proxy": None, "locale": "vi-VN"}),
        ("locale", {"proxy": None, "timezone": None}),
    ):
        response = client.patch(path, json={field: None})
        assert response.status_code == 200, response.text
        profile = response.json()
        assert profile[field] is None
        assert profile["engine"] == "chrome" and profile["os_type"] == "macos"
        assert {key: profile[key] for key in retained} == retained
