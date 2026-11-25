import asyncio
import errno
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from weakref import ref

import flet as ft
import pytest

from chrome_engine.fetch import FetchError
from src.core import strings
from src.models.profile import Profile
from src.services.browser.extensions import ExtensionEntry
from src.ui.actions.profile import edit_profile
from src.ui.app import App
from src.ui.dialogs import cookies, cores, export, extensions, log, mcp_guide
from src.ui.dialogs.modal import show_modal_dialog, show_profile_editor
from src.ui.dialogs.profile import open_profile_dialog
from src.ui.dialogs.profile_field import open_profile_field_dialog
from src.ui.dialogs.proxy_pool import open_proxy_pool_dialog
from src.ui.dialogs.warming import open_warming_dialog


class DialogPage:
    def __init__(self, notify_dismiss=True):
        self.dialogs = []
        self.dialog_parent = ft.Column(controls=self.dialogs)
        self.notify_dismiss = notify_dismiss
        self.update = Mock()
        self.on_resize = Mock()
        self.on_keyboard_event = Mock()

    def show_dialog(self, dialog):
        dialog._parent = ref(self.dialog_parent)
        dialog.open = True
        self.dialogs.append(dialog)

    def pop_dialog(self):
        dialog = self.dialogs.pop()
        dialog.open = False
        if self.notify_dismiss and dialog.on_dismiss:
            dialog.on_dismiss(None)
        return dialog


def _send_key(page, key):
    (dialog,) = page.dialogs
    handler = (
        dialog.content.on_key_up or dialog.content.on_key_down
        if isinstance(dialog.content, ft.KeyboardListener)
        else page.on_keyboard_event
    )
    handler(SimpleNamespace(key=key))


def _click_action(dialog, index):
    action = dialog.actions[index]
    if isinstance(action, ft.KeyboardListener):
        action = action.content
    action.on_click(None)


@pytest.mark.parametrize(
    "kind", ["cookies", "extensions", "extension_settings", "export", "log", "mcp"]
)
def test_escape_dismisses_utility_dialog_and_restores_handlers(kind, monkeypatch):
    page = DialogPage(notify_dismiss=False)
    originals = (page.on_keyboard_event, page.on_resize)
    profile = Profile("Chromium demo", engine="chrome")
    service = SimpleNamespace(list_entries=lambda: [], usage_counts=lambda _: {})
    pm = SimpleNamespace(list_profiles=lambda: [profile])
    action = Mock()
    if kind == "cookies":
        cookies.open_cookie_dialog(page, profile.name, "", action, action, action)
    elif kind == "extensions":
        extensions.open_extensions_dialog(page, service, pm)
    elif kind == "extension_settings":
        entry = ExtensionEntry("demo", "Demo", "1", "", "", "")
        extensions.open_extension_settings_dialog(page, entry, service, pm, on_saved=action)
    elif kind == "export":
        export.open_export_dialog(page, Mock(), [profile], action)
    elif kind == "log":
        log.open_log_dialog(page, [])
    else:
        monkeypatch.setattr(mcp_guide, "threading", SimpleNamespace(Thread=Mock()))
        mcp_guide.open_mcp_guide(page, Mock())

    if kind == "export":
        cancel, _ = page.dialogs[-1].actions
        cancel.disabled = True
        _send_key(page, "Escape")
        assert len(page.dialogs) == 1
        cancel.disabled = False

    _send_key(page, "Enter")
    assert len(page.dialogs) == 1
    _send_key(page, "Escape")
    assert page.dialogs == []
    assert (page.on_keyboard_event, page.on_resize) == originals
    action.assert_not_called()


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
    parent.controls.remove(second)
    assert page.on_keyboard_event is first_keyboard
    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert not page.dialogs
    assert page.on_keyboard_event is original_keyboard


@pytest.mark.parametrize("flet_unmounts", [True, False])
def test_escape_release_only_closes_the_dialog_that_received_it(flet_unmounts):
    page = ft.Page(Mock())
    page.update = Mock()
    page._dialogs.update = Mock()
    original_keyboard = page.on_keyboard_event
    extensions_dialog, settings_dialog = (
        ft.AlertDialog(content=ft.Text(title)) for title in ("Extensions", "Settings")
    )
    extensions_dialog.update = settings_dialog.update = Mock()
    show_modal_dialog(page, extensions_dialog, escape_on_release=True)
    show_modal_dialog(page, settings_dialog, escape_on_release=True)
    escape = SimpleNamespace(key="Escape")

    settings_dialog.content.on_key_up(escape)
    assert not settings_dialog.open
    assert settings_dialog in page._dialogs.controls
    settings_dialog.content.on_key_up(escape)
    assert extensions_dialog.open
    settings_dialog.content.on_key_up(escape)
    assert extensions_dialog.open

    if flet_unmounts:
        asyncio.run(settings_dialog.on_dismiss(SimpleNamespace(data=False)))
        assert page._dialogs.controls == [extensions_dialog]
        settings_dialog.content.on_key_up(escape)
        assert extensions_dialog.open
    extensions_dialog.content.on_key_up(escape)
    assert not extensions_dialog.open
    if not flet_unmounts:
        asyncio.run(settings_dialog.on_dismiss(SimpleNamespace(data=False)))
    asyncio.run(extensions_dialog.on_dismiss(SimpleNamespace(data=False)))
    assert page.on_keyboard_event is original_keyboard
    show_modal_dialog(page, extensions_dialog)
    assert extensions_dialog.open
    assert page._dialogs.controls == [extensions_dialog]


def test_detached_dialog_does_not_pop_another_dialog():
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    detached, current = ft.AlertDialog(), ft.AlertDialog()
    close_detached = show_modal_dialog(page, detached)
    show_modal_dialog(page, current)
    detached._parent = None

    close_detached()

    assert page.dialogs == [detached, current]
    assert not detached.open
    assert current.open
    page.dialogs.remove(detached)
    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert not page.dialogs
    assert page.on_keyboard_event is original_keyboard


def test_escape_restores_keyboard_handler_when_active_dialog_is_detached():
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    dialog = ft.AlertDialog()
    show_modal_dialog(page, dialog)
    page.dialogs.remove(dialog)
    dialog._parent = None

    page.on_keyboard_event(SimpleNamespace(key="Escape"))

    assert not dialog.open
    assert page.on_keyboard_event is original_keyboard


def test_detached_dialog_honors_close_guard():
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    dialog = ft.AlertDialog()
    can_close = Mock(return_value=False)
    show_modal_dialog(page, dialog, can_close=can_close)
    page.dialogs.remove(dialog)
    dialog._parent = None

    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert dialog.open
    assert page.on_keyboard_event is not original_keyboard

    can_close.return_value = True
    page.on_keyboard_event(SimpleNamespace(key="Escape"))
    assert not dialog.open
    assert page.on_keyboard_event is original_keyboard


@pytest.mark.parametrize("settings_is_topmost", [True, False])
def test_extension_settings_refreshes_after_persisting(settings_is_topmost):
    page = DialogPage()
    saved = Mock()
    service = SimpleNamespace(assign_profiles=Mock())
    pm = SimpleNamespace(list_profiles=lambda: [])
    entry = ExtensionEntry("demo", "Demo", "1", "", "", "")
    extensions.open_extension_settings_dialog(page, entry, service, pm, on_saved=saved)
    (settings,) = page.dialogs
    if not settings_is_topmost:
        show_modal_dialog(page, ft.AlertDialog())
    _click_action(settings, -1)

    service.assign_profiles.assert_called_once()
    saved.assert_called_once_with()
    assert settings.open != settings_is_topmost


@pytest.mark.parametrize("parent_action", ["escape", "close"])
def test_extensions_can_close_after_settings_escape(parent_action):
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    entry = ExtensionEntry("demo", "Demo", "1", "", "", "")
    service = SimpleNamespace(list_entries=lambda: [], usage_counts=lambda _: {})
    pm = SimpleNamespace(list_profiles=lambda: [])
    extensions.open_extensions_dialog(page, service, pm)
    (parent,) = page.dialogs
    extensions.open_extension_settings_dialog(page, entry, service, pm)
    child = page.dialogs[-1]
    escape = SimpleNamespace(key="Escape")

    # The parent must not subscribe to Flet's page-wide key-down event stream.
    assert page.on_keyboard_event is original_keyboard
    child.content.on_key_up(escape)
    assert page.dialogs == [parent]
    if parent_action == "escape":
        parent.content.on_key_up(escape)
    else:
        _click_action(parent, 0)
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
        _send_key(page, "Escape")
    else:
        _click_action(dialog, action == "save")
    assert not page.dialogs
    assert (page.on_keyboard_event, page.on_resize) == originals
    assert save.call_count == (action == "save")

    open_editor()
    assert len(page.dialogs) == 1
    _click_action(page.dialogs[-1], 0)


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
        _send_key(page, "Enter")
        assert page.dialogs == [dialog]
        _send_key(page, "Escape")
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
        _click_action(page.dialogs[-1], 0)
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


@pytest.mark.parametrize("explicit_from", [True, False], ids=["explicit_from", "implicit_context"])
def test_core_staging_disk_full_shows_localized_guidance(monkeypatch, caplog, explicit_from):
    monkeypatch.setattr(strings, "_ACTIVE", strings.STRINGS["vi"])
    monkeypatch.setattr(
        cores.cores,
        "inventory",
        lambda: dict(
            activation_allowed=True,
            cores=[],
            candidates=[
                dict(
                    engine="chrome", version="test", staged=False, can_stage=True, blocked_reason=""
                )
            ],
        ),
    )

    def fail(*_args, **_kwargs):
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as exc:
            error = FetchError("Download failed: https://example.test/core.dmg")
            if explicit_from:
                raise error from exc
            raise error  # noqa: B904 - exercise implicit exception chaining

    monkeypatch.setattr(cores.cores, "stage", fail)
    page = DialogPage()
    cores.open_core_manager(page)
    (dialog,) = page.dialogs
    controls = list(_walk(dialog.content))
    choices = next(c for c in controls if isinstance(c, ft.Dropdown))
    confirm = next(c for c in controls if isinstance(c, ft.Checkbox))
    download = next(c for c in controls if type(c) is ft.Button)
    choices.value = "chrome:test"
    confirm.value = True
    confirm.on_change(None)
    assert not download.disabled

    asyncio.run(download.on_click(None))

    messages = [c.value for c in controls if isinstance(c, ft.Text)]
    assert "Không đủ dung lượng ở nơi lưu core. Hãy giải phóng dung lượng rồi thử lại." in messages
    assert not any("Download failed" in message or "Errno" in message for message in messages)
    assert "No space left on device" in caplog.text
    assert download.disabled and not confirm.value


def test_core_staging_error_chain_can_contain_a_cycle():
    exc = RuntimeError()
    exc.__cause__ = exc
    assert cores._stage_error_key(exc) == "core_stage_failed"


def test_core_staging_does_not_classify_suppressed_context_as_disk_full():
    try:
        raise OSError(errno.ENOSPC, "earlier unrelated error")
    except OSError:
        with pytest.raises(RuntimeError) as caught:
            raise RuntimeError("staging failure") from None
    assert cores._stage_error_key(caught.value) == "core_stage_failed"


@pytest.mark.parametrize(
    "code, key", [(errno.ENOSPC, "core_stage_no_space"), (errno.EIO, "core_stage_failed")]
)
def test_core_staging_classifies_copytree_errors(tmp_path, code, key):
    source = tmp_path / "Chromium.app"
    source.mkdir()
    (source / "Chromium").touch()

    def fail(*_args):
        raise OSError(code, "copy failed")

    with pytest.raises(shutil.Error) as caught:
        shutil.copytree(source, tmp_path / "staging", copy_function=fail, symlinks=True)

    assert cores._stage_error_key(caught.value) == key


def test_dropdown_escape_seen_only_by_page_preserves_the_unsaved_profile(monkeypatch):
    page = DialogPage()
    original_keyboard = page.on_keyboard_event
    save = Mock()
    open_profile_dialog(page, None, save, Profile("Chromium demo", engine="chrome"))
    (dialog,) = page.dialogs
    name = next(c for c in _walk(dialog.content) if isinstance(c, ft.TextField))
    name.value = "Unsaved draft"
    focus = AsyncMock()
    monkeypatch.setattr(dialog.content, "focus", focus)
    monkeypatch.setattr(type(ft.context), "disable_auto_update", Mock())

    escape = SimpleNamespace(key="Escape")
    asyncio.run(page.on_keyboard_event(escape))
    assert page.dialogs == [dialog]
    assert name.value == "Unsaved draft"
    focus.assert_awaited_once()
    save.assert_not_called()

    dialog.content.on_key_down(escape)
    assert not page.dialogs
    assert page.on_keyboard_event is original_keyboard


def test_modal_focus_failure_does_not_disable_auto_update(monkeypatch):
    page = DialogPage()
    dialog = ft.AlertDialog(content=ft.Text("Dialog"))
    show_modal_dialog(page, dialog, local_keyboard=True)
    focus = AsyncMock(side_effect=RuntimeError("focus failed"))
    monkeypatch.setattr(dialog.content, "focus", focus)
    disable_auto_update = Mock()
    monkeypatch.setattr(type(ft.context), "disable_auto_update", disable_auto_update)

    with pytest.raises(RuntimeError, match="focus failed"):
        asyncio.run(page.on_keyboard_event(SimpleNamespace(key="Escape")))

    focus.assert_awaited_once()
    disable_auto_update.assert_not_called()


@pytest.mark.parametrize("action", ["cancel", "save", "escape", "dismiss"])
def test_profile_close_stops_totp_timer(monkeypatch, action):
    page = DialogPage(notify_dismiss=False)
    timer = Mock()
    timer_factory = Mock(return_value=timer)
    monkeypatch.setattr("src.ui.dialogs.profile.threading.Timer", timer_factory)
    open_profile_dialog(
        page, None, Mock(return_value=None), Profile("Chromium demo", engine="chrome")
    )
    (dialog,) = page.dialogs
    timer.start.assert_called_once_with()
    timer.cancel.assert_not_called()

    if action == "escape":
        _send_key(page, "Escape")
    elif action == "dismiss":
        page.pop_dialog()
        dialog.on_dismiss(None)
    else:
        _click_action(dialog, action == "save")

    assert not page.dialogs
    timer.cancel.assert_called_once_with()
    timer_factory.call_args.args[1]()
    timer_factory.assert_called_once()


def test_profile_close_prevents_in_flight_totp_tick_from_restarting_timer(monkeypatch):
    page = DialogPage(notify_dismiss=False)
    current_timer, next_timer = Mock(), Mock()
    timer_factory = Mock(return_value=current_timer)
    monkeypatch.setattr("src.ui.dialogs.profile.threading.Timer", timer_factory)
    open_profile_dialog(
        page, None, Mock(return_value=None), Profile("Chromium demo", engine="chrome")
    )
    tick = timer_factory.call_args.args[1]

    def close_editor(*_):
        _send_key(page, "Escape")
        return next_timer

    timer_factory.side_effect = close_editor
    tick()

    assert not page.dialogs
    current_timer.cancel.assert_called_once_with()
    next_timer.start.assert_not_called()


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


def test_unclosed_previous_profile_dialog_does_not_pollute_next_totp_timer(monkeypatch):
    import threading
    import time

    real_timer = threading.Timer
    first_timers = []

    def short_real_timer(_interval, fn):
        t = real_timer(0.01, fn)
        first_timers.append(t)
        return t

    monkeypatch.setattr("src.ui.dialogs.profile.threading.Timer", short_real_timer)
    first_page = DialogPage(notify_dismiss=False)
    open_profile_dialog(
        first_page, None, Mock(return_value=None), Profile("Old dialog", engine="chrome")
    )

    second_timer = Mock()
    second_factory = Mock(return_value=second_timer)
    monkeypatch.setattr("src.ui.dialogs.profile.threading.Timer", second_factory)
    second_page = DialogPage(notify_dismiss=False)
    open_profile_dialog(
        second_page, None, Mock(return_value=None), Profile("New dialog", engine="chrome")
    )

    time.sleep(0.03)
    second_factory.assert_called_once()
    second_timer.start.assert_called_once_with()
    _send_key(second_page, "Escape")
    assert all(not t.is_alive() for t in first_timers)
