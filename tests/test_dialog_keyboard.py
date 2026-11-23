from types import SimpleNamespace
from unittest.mock import Mock

import flet as ft
import pytest

from src.models.profile import Profile
from src.ui.actions.profile import edit_profile
from src.ui.app import App
from src.ui.dialogs import cores
from src.ui.dialogs.profile import open_profile_dialog
from src.ui.dialogs.profile_field import open_profile_field_dialog
from src.ui.dialogs.proxy_pool import open_proxy_pool_dialog
from src.ui.dialogs.warming import open_warming_dialog


class DialogPage:
    def __init__(self):
        self.dialogs = []
        self.update = Mock()
        self.on_resize = Mock()
        self.on_keyboard_event = Mock()

    def show_dialog(self, dialog):
        dialog.open = True
        self.dialogs.append(dialog)

    def pop_dialog(self):
        dialog = self.dialogs.pop()
        dialog.open = False
        if dialog.on_dismiss:
            dialog.on_dismiss(None)
        return dialog


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
