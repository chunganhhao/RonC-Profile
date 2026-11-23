import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.strings import get_string
from src.services.profile.manager import ProfileStoreChangedError
from src.ui.actions import bulk, profile


@pytest.mark.parametrize("action", [bulk, profile])
@pytest.mark.parametrize("error_type", [OSError, ProfileStoreChangedError])
@pytest.mark.parametrize("message", ["store busy", ""])
def test_delete_reports_errors_and_finishes(monkeypatch, action, error_type, message):
    names = ["first", "second", "third"] if action is bulk else ["second"]
    deleted = []

    def delete(name):
        if name == "second":
            raise error_type(message)
        deleted.append(name)

    pm = SimpleNamespace(delete_profile=delete)
    log, refresh, done = Mock(), Mock(), Mock()
    monkeypatch.setattr(action, "open_confirm_dialog", lambda p, n, cb, **kw: cb())
    if action is bulk:
        bulk.bulk_delete_profiles(None, names, pm, log, refresh, done)
        done.assert_called_once_with()
        assert deleted == ["first", "third"]
        assert log.call_args_list[0].args == (get_string("deleted_profile", name="first"),)
        assert log.call_args_list[-1].args == (get_string("deleted_profile", name="third"),)
    else:
        profile.delete_profile(None, "second", pm, log, refresh)
        assert deleted == []
    log.assert_any_call(message or "Profile/store is busy; refresh and retry")
    refresh.assert_called_once_with()


@pytest.mark.parametrize("action", [bulk, profile])
def test_delete_success(monkeypatch, action):
    pm = SimpleNamespace(delete_profile=Mock())
    log, refresh, done = Mock(), Mock(), Mock()
    monkeypatch.setattr(action, "open_confirm_dialog", lambda p, n, cb, **kw: cb())
    if action is bulk:
        bulk.bulk_delete_profiles(None, ["first"], pm, log, refresh, done)
        done.assert_called_once_with()
    else:
        profile.delete_profile(None, "first", pm, log, refresh)
    pm.delete_profile.assert_called_once_with("first")
    log.assert_called_once_with(get_string("deleted_profile", name="first"))
    refresh.assert_called_once_with()


def test_bulk_delete_empty(monkeypatch):
    confirm = Mock()
    monkeypatch.setattr(bulk, "open_confirm_dialog", confirm)
    pm = SimpleNamespace(delete_profile=Mock())
    log, refresh, done = Mock(), Mock(), Mock()
    bulk.bulk_delete_profiles(None, [], pm, log, refresh, done)
    for callback in (confirm, pm.delete_profile, log, refresh, done):
        callback.assert_not_called()


@pytest.mark.parametrize("cancel_with_escape", [True, False])
def test_single_delete_confirmation_cancel(cancel_with_escape):
    pm = SimpleNamespace(delete_profile=Mock())
    log, refresh = Mock(), Mock()
    page = Mock()
    previous_keyboard_handler = page.on_keyboard_event
    page.show_dialog.side_effect = lambda dialog: setattr(dialog, "open", True)
    profile.delete_profile(page, "first", pm, log, refresh)
    dialog = page.show_dialog.call_args.args[0]
    assert dialog.modal

    if cancel_with_escape:
        enter = SimpleNamespace(key="Enter")
        asyncio.run(page.on_keyboard_event(enter))
        previous_keyboard_handler.assert_called_once_with(enter)
        page.pop_dialog.assert_not_called()
        asyncio.run(page.on_keyboard_event(SimpleNamespace(key="Escape")))
    else:
        cancel_button = next(c for c in dialog.actions if c.content == get_string("cancel"))
        cancel_button.on_click(None)
    page.pop_dialog.assert_called_once_with()
    assert page.on_keyboard_event is previous_keyboard_handler
    for callback in (pm.delete_profile, log, refresh):
        callback.assert_not_called()


def test_single_delete_confirmation_runs_before_close():
    page = Mock(on_keyboard_event=None)
    page.show_dialog.side_effect = lambda dialog: setattr(dialog, "open", True)
    pm = SimpleNamespace(delete_profile=Mock())
    pm.delete_profile.side_effect = lambda _: page.pop_dialog.assert_not_called()
    profile.delete_profile(page, "first", pm, Mock(), Mock())
    dialog = page.show_dialog.call_args.args[0]
    delete_button = next(c for c in dialog.actions if c.content == get_string("delete"))

    delete_button.on_click(None)

    pm.delete_profile.assert_called_once_with("first")
    page.pop_dialog.assert_called_once_with()
    assert page.on_keyboard_event is None
