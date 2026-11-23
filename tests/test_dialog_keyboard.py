from types import SimpleNamespace
from unittest.mock import Mock
from weakref import ref

import flet as ft
import pytest

from src.models.profile import Profile
from src.ui.actions.profile import edit_profile
from src.ui.app import App
from src.ui.dialogs import cores
from src.ui.dialogs.modal import show_modal_dialog, show_profile_editor
from src.ui.dialogs.profile import open_profile_dialog
from src.ui.dialogs.profile_field import open_profile_field_dialog
from src.ui.dialogs.proxy_pool import open_proxy_pool_dialog
from src.ui.dialogs.warming import open_warming_dialog


class DialogPage:
    def __init__(self, notify_dismiss=True):
        self.dialogs = []
        self.notify_dismiss = notify_dismiss
        self.update = Mock()
        self.on_resize = Mock()
        self.on_keyboard_event = Mock()

    def show_dialog(self, dialog):
        dialog.open = True
        self.dialogs.append(dialog)

    def pop_dialog(self):
        dialog = self.dialogs.pop()
        dialog.open = False
        if self.notify_dismiss and dialog.on_dismiss:
            dialog.on_dismiss(None)
        return dialog


def test_modal_escape_ignores_non_dialog_siblings_and_restores_underlying_handler():
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    first, second = ft.AlertDialog(), ft.AlertDialog()
    close_first = show_modal_dialog(page, first)
    first_keyboard = page.on_keyboard_event
    show_modal_dialog(page, second)
    parent = ft.Column(controls=[first, second, ft.Text("Not a dialog")])
    first._parent = second._parent = ref(parent)

    close_first(None)
    assert page.dialogs == [first, second]
    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert page.dialogs == [first]
    assert page.on_keyboard_event is first_keyboard
    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert not page.dialogs
    assert page.on_keyboard_event is original_keyboard


def test_failed_editor_open_does_not_block_retry(monkeypatch):
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    failed, retry = ft.AlertDialog(), ft.AlertDialog()

    def fail_show(dialog):
        dialog.open = True
        raise RuntimeError("render failed")

    with monkeypatch.context() as patch:
        patch.setattr(page, "show_dialog", fail_show)
        with pytest.raises(RuntimeError, match="render failed"):
            show_profile_editor(page, failed, Mock())
    assert page.on_keyboard_event is original_keyboard
    assert show_profile_editor(page, retry, Mock()) is not None
    assert page.dialogs == [retry]


@pytest.mark.parametrize("kind", ["edit", "tag"])
@pytest.mark.parametrize("action", ["cancel", "save", "escape"])
def test_editor_closes_and_reopens_without_client_dismiss_event(kind, action):
    page = DialogPage(notify_dismiss=False)
    originals = (page.on_keyboard_event, page.on_resize)
    profile = Profile("Chromium demo", engine="chrome")
    save = Mock(return_value=None)

    def open_editor():
        if kind == "tag":
            open_profile_field_dialog(page, profile, "tags", save)
        else:
            open_profile_dialog(page, None, save, profile)

    open_editor()
    (dialog,) = page.dialogs
    if action == "escape":
        page.on_keyboard_event(SimpleNamespace(key="Escape"))
    else:
        dialog.actions[action == "save"].on_click(None)
    assert not page.dialogs
    assert (page.on_keyboard_event, page.on_resize) == originals
    assert save.call_count == (action == "save")

    open_editor()
    assert len(page.dialogs) == 1
    page.dialogs[-1].actions[0].on_click(None)


@pytest.mark.parametrize("kind", ["create", "edit", "tag", "cores", "proxy_pool", "warming"])
def test_escape_dismisses_modal_without_saving_and_restores_handlers(monkeypatch, kind):
    page = DialogPage()
    original_keyboard, original_resize = page.on_keyboard_event, page.on_resize
    profile = Profile("Chromium demo", engine="chrome")
    save = Mock()
    if kind == "tag":
        open_profile_field_dialog(page, profile, "tags", save)
    elif kind == "cores":
        monkeypatch.setattr(
            cores.cores,
            "inventory",
            lambda: dict(activation_allowed=False, cores=[], candidates=[]),
        )
        cores.open_core_manager(page)
    elif kind == "proxy_pool":
        pool = SimpleNamespace(list_entries=lambda: [], usage_counts=lambda _: {})
        open_proxy_pool_dialog(page, pool, None, None, target_profile_names=[profile.name])
    elif kind == "warming":
        warming = SimpleNamespace(list_tasks=lambda: [])
        open_warming_dialog(page, warming, SimpleNamespace(list_profiles=lambda: [profile]), None)
    else:
        open_profile_dialog(page, None, save, profile if kind == "edit" else None)

    (dialog,) = page.dialogs
    try:
        page.on_keyboard_event(SimpleNamespace(key="Enter"))
        assert page.dialogs == [dialog]
        page.on_keyboard_event(SimpleNamespace(key="Escape"))
        assert not page.dialogs
        assert page.on_keyboard_event is original_keyboard
        assert page.on_resize is original_resize
        save.assert_not_called()
    finally:
        if page.dialogs:
            page.pop_dialog()


@pytest.mark.parametrize("kind", ["edit", "tag"])
def test_double_click_on_row_pencil_opens_only_one_editor_and_can_reopen(kind):
    page = DialogPage()
    profile = Profile("Chromium demo", engine="chrome")
    pm = SimpleNamespace(profiles={profile.name: profile}, list_profiles=lambda: [profile])
    bl = Mock()
    if kind == "edit":

        def open_editor():
            edit_profile(page, profile.name, pm, bl, None, Mock(), Mock())
    else:
        gui = SimpleNamespace(page=page, pm=pm, bl=bl)

        def open_editor():
            App._on_edit_field(gui, profile.name, "tags")

    try:
        open_editor()
        open_editor()
        page.dialogs[-1].actions[0].on_click(None)
        assert not page.dialogs
        open_editor()
        assert len(page.dialogs) == 1
    finally:
        while page.dialogs:
            page.pop_dialog()


def _walk(control):
    yield control
    for child in getattr(control, "controls", ()) or ():
        yield from _walk(child)
    if isinstance(getattr(control, "content", None), ft.Control):
        yield from _walk(control.content)


@pytest.mark.parametrize("is_edit", [False, True], ids=["create", "edit"])
def test_enter_in_profile_name_runs_the_save_button_path(is_edit):
    page = DialogPage()
    profile = Profile("Original name", engine="chrome") if is_edit else None
    save = Mock(return_value=None)
    open_profile_dialog(page, None, save, profile)
    (dialog,) = page.dialogs
    name = next(c for c in _walk(dialog.content) if isinstance(c, ft.TextField))
    try:
        assert name.value == ("Original name" if is_edit else "")
        name.value = "Chromium demo"
        name.on_submit(None)
        assert save.call_args.args[0] == "Chromium demo"
        save.assert_called_once()
        assert not page.dialogs
    finally:
        if page.dialogs:
            page.pop_dialog()
