from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import profiles
from src.core.strings import get_string
from src.models.profile import Profile
from src.services.browser.launcher import BrowserLauncher
from src.ui.actions import cookies


@pytest.fixture
def setup_reset(monkeypatch):
    bl = BrowserLauncher()
    by_name = {"p": Profile(name="p")}
    pm = SimpleNamespace(profiles=by_name, get_profile=by_name.get)
    reset = Mock(return_value=True)
    monkeypatch.setattr(profiles.fingerprint, "reset", reset)
    monkeypatch.setattr(cookies.fingerprint, "summary", lambda path: None)
    yield pm, bl, reset
    bl._active_sessions.clear()


@contextmanager
def busy_profile(bl, state):
    if state == "locked":
        with bl.exclusive("p"):
            yield
    else:
        bl._active_sessions["p"] = SimpleNamespace(poll=lambda: None)
        try:
            yield
        finally:
            bl._active_sessions.clear()


@pytest.mark.parametrize("state", ["locked", "running"])
def test_ui_reset_rechecks_at_confirmation(monkeypatch, setup_reset, state):
    pm, bl, reset = setup_reset
    callbacks = {}
    messages = []
    monkeypatch.setattr(
        cookies,
        "open_cookie_dialog",
        lambda *args: callbacks.update(open_reset=args[-1]),
    )
    monkeypatch.setattr(
        cookies,
        "open_confirm_dialog",
        lambda page, name, callback, **kwargs: callbacks.update(confirm=callback),
    )
    cookies.manage_cookies(None, "p", None, pm, bl, messages.append)
    callbacks["open_reset"]()
    with busy_profile(bl, state):
        callbacks["confirm"]()
    reset.assert_not_called()
    assert messages == [get_string("profile_busy", name="p")]


def api_client(pm, bl):
    app = FastAPI()
    app.include_router(profiles.router)
    app.dependency_overrides[profiles.get_profile_manager] = lambda: pm
    app.dependency_overrides[profiles.get_browser_launcher] = lambda: bl
    return TestClient(app)


@pytest.mark.parametrize("state", ["locked", "running"])
def test_api_reset_busy_after_stopped_check(monkeypatch, setup_reset, state):
    pm, bl, reset = setup_reset
    # The initial stopped check cannot guarantee the profile stays stopped.
    monkeypatch.setattr(bl, "is_running", lambda name: False)
    with busy_profile(bl, state):
        response = api_client(pm, bl).delete("/profiles/p/fingerprint")
    reset.assert_not_called()
    assert response.status_code == 409
    assert response.json() == {"detail": profiles._BUSY_DETAIL}


@pytest.mark.parametrize("exists", [True, False])
def test_api_reset_free_profile(setup_reset, exists):
    pm, bl, reset = setup_reset
    reset.return_value = exists
    response = api_client(pm, bl).delete("/profiles/p/fingerprint")
    assert response.status_code == 200
    reset.assert_called_once()
    with bl.exclusive("p"):
        pass


@pytest.mark.parametrize("exists", [True, False])
def test_ui_reset_free_profile(monkeypatch, setup_reset, exists):
    pm, bl, reset = setup_reset
    reset.return_value = exists
    callbacks = {}
    messages = []
    monkeypatch.setattr(
        cookies,
        "open_cookie_dialog",
        lambda *args: callbacks.update(open_reset=args[-1]),
    )
    monkeypatch.setattr(
        cookies,
        "open_confirm_dialog",
        lambda page, name, callback, **kwargs: callbacks.update(confirm=callback),
    )
    cookies.manage_cookies(None, "p", None, pm, bl, messages.append)
    callbacks["open_reset"]()
    callbacks["confirm"]()
    reset.assert_called_once()
    assert messages == [get_string("fingerprint_was_reset", name="p")]
    with bl.exclusive("p"):
        pass
