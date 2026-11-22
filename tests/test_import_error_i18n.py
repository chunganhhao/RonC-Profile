import asyncio
import json
import zipfile
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core import strings
from src.services.browser import cookies
from src.services.profile import manager
from src.ui.actions import cookies as cookie_actions
from src.ui.actions import transfer


@pytest.fixture
def imports(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    pm = manager.ProfileManager()
    assert pm.add_profile("Existing", "", "windows")
    path = tmp_path / "profile.zip"
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("profile.json", json.dumps({"name": "existing"}))
    return pm, path


def test_duplicate_service_retains_message_with_error_code(imports):
    pm, path = imports
    success, error = pm.import_profile(str(path))
    assert (success, error) == (False, "Profile 'existing' already exists")
    assert getattr(error, "code", None) == "profile_exists"
    assert error.name == "existing"


def test_cookie_service_retains_message_with_error_code():
    with pytest.raises(cookies.CookieError) as exc:
        cookies.parse_cookies("{bad")
    assert str(exc.value).startswith("Invalid cookie JSON: Expecting property name")
    assert getattr(exc.value, "code", None) == "invalid_cookie_json"


@pytest.mark.parametrize("language", ["vi", "en"])
def test_desktop_import_errors_localized(imports, tmp_path, monkeypatch, language):
    pm, path = imports
    previous = strings.current_language()
    strings.set_language(language)
    try:
        logs, refreshed = [], []
        picker = SimpleNamespace(
            pick_files=AsyncMock(return_value=[SimpleNamespace(path=str(path))])
        )
        asyncio.run(
            transfer.import_profile(picker, pm, logs.append, lambda: refreshed.append(True))
        )
        expected = (
            "Hồ sơ 'existing' đã tồn tại"
            if language == "vi"
            else "Profile 'existing' already exists"
        )
        assert expected in logs[-1]
        assert refreshed == []

        cookie_path = tmp_path / "cookies.json"
        cookie_path.write_text("{bad")
        picker.pick_files.return_value = [SimpleNamespace(path=str(cookie_path))]
        callbacks = []
        monkeypatch.setattr(
            cookie_actions, "open_cookie_dialog", lambda *args: callbacks.append(args[4])
        )
        monkeypatch.setattr(cookie_actions.fingerprint, "summary", lambda _: None)
        bl = SimpleNamespace(is_running=lambda _: False, exclusive=lambda _: nullcontext())
        cookie_actions.manage_cookies(None, "Existing", picker, pm, bl, logs.append)
        asyncio.run(callbacks[0]())
        expected = "JSON cookie không hợp lệ" if language == "vi" else "Invalid cookie JSON"
        assert expected in logs[-1]
        assert "Expecting property" not in logs[-1]
        assert "line 1" not in logs[-1]
    finally:
        strings.set_language(previous)


def test_import_api_contract(imports, monkeypatch):
    pm, path = imports
    monkeypatch.setattr("src.api.app.API_TOKEN", "")
    monkeypatch.setattr("src.api.auth.API_TOKEN", "")
    container = SimpleNamespace(
        profile_manager=pm,
        event_bus=SimpleNamespace(emit=lambda: None),
        browser_launcher=SimpleNamespace(
            is_running=lambda _: False, exclusive=lambda _: nullcontext()
        ),
    )
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    response = client.post("/api/v1/profiles/import", json={"zip_path": str(path)})
    print("ZIP API:", response.status_code, response.json())
    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "profile_name": None,
        "error": "Profile 'existing' already exists",
    }
    response = client.post("/api/v1/profiles/Existing/cookies", json={"content": "{bad"})
    print("COOKIE API:", response.status_code, response.json())
    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "imported": 0,
        "error": "Invalid cookie JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    }
