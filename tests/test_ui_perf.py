"""Regression tests for desktop UI diffing, virtualization, caching, and debounce."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
from weakref import ref

import flet as ft
import msgpack
from flet.controls.object_patch import ObjectPatch
from flet.messaging.protocol import configure_encode_object_for_msgpack

from src.core import config as app_config
from src.core.container import Container
from src.core.strings import get_string
from src.models.profile import Profile
from src.services.profile import manager as pm_mod
from src.services.proxy import pool as pool_mod
from src.services.proxy.pool import ProxyPoolService
from src.ui.app import App
from src.ui.components.content_area import build_content_area
from src.ui.components.factory import build_ui_refs
from src.ui.components.profile_card import ProfileRow, build_header_row, build_profile_card
from src.ui.dialogs.profile import open_profile_dialog
from src.ui.dialogs.responsive import bind_responsive_dialog
from src.ui.theme.styles import StableButton, StableIconButton


def _walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from _walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from _walk(content)


def test_stable_buttons_emit_zero_phantom_style_ops():
    enc = configure_encode_object_for_msgpack(ft.BaseControl)
    icon_btn = StableIconButton(
        icon=ft.Icons.EDIT_OUTLINED,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8), overlay_color="#181B21"),
    )
    action_btn = StableButton(
        "Launch",
        icon=ft.Icons.LANGUAGE,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=7),
            bgcolor="#181B21",
            color="#F3F4F6",
            elevation=0,
        ),
    )

    for btn in (icon_btn, action_btn):
        p0, _, _ = ObjectPatch.from_diff(None, btn, control_cls=ft.BaseControl)
        msgpack.packb(p0.to_message(), default=enc)

        p1, _, _ = ObjectPatch.from_diff(btn, btn, control_cls=ft.BaseControl)
        assert p1.patch == []

        btn.style = ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=4),
            bgcolor="#123456",
            color="#FFFFFF",
        )
        p2, _, _ = ObjectPatch.from_diff(btn, btn, control_cls=ft.BaseControl)
        assert p2.patch

        msgpack.packb(p2.to_message(), default=enc)
        btn.style.bgcolor = "#654321"
        p3, _, _ = ObjectPatch.from_diff(btn, btn, control_cls=ft.BaseControl)
        assert p3.patch


def test_listview_virtualization_and_dynamic_profile_row_isolation():
    refs = build_ui_refs(
        pm=SimpleNamespace(profiles={}),
        on_change_page=lambda _: None,
        file_picker=None,
        on_search=lambda _: None,
    )
    assert isinstance(refs.profile_list_area, ft.ListView)
    assert refs.profile_list_area.build_controls_on_demand is True

    def noop(*_):
        pass

    cards = [
        build_profile_card(
            Profile(
                f"Demo {i}",
                engine="chrome",
                proxy="http://u:p@1.2.3.4:8080",
                account_uid=f"u{i}@example.test",
                account_password="pw",
                two_fa_secret="JBSWY3DPEHPK3PXP",
            ),
            False,
            False,
            noop,
            noop,
            noop,
            on_select=noop,
            on_cookies=noop,
            on_copy_user=noop,
            on_copy_recovery=noop,
            on_edit_field=noop,
            on_copy_pass=noop,
            on_copy_2fa=noop,
            layout_width=1400,
        )
        for i in range(200)
    ]
    header = build_header_row(False, False, noop, layout_width=1400)
    assert all(row.is_isolated() for row in [header, *cards])

    refs.profile_list_area.controls = cards
    area = build_content_area(
        ft.Text(""),
        refs.profile_list_area,
        refs.prev_btn,
        refs.next_btn,
        refs.page_label,
        header_slot=ft.Container(content=header),
        initial_table_width=1400,
    )
    controls = {getattr(c, "key", None): c for c in _walk(area)}
    sheet = controls["profiles_sheet_container"]
    table = controls["profiles_table_container"]
    assert sheet.size_change_interval == 80

    enc = configure_encode_object_for_msgpack(ft.BaseControl)
    p0, _, _ = ObjectPatch.from_diff(None, table, control_cls=ft.BaseControl)
    msgpack.packb(p0.to_message(), default=enc)

    patches = []

    def fake_update(self):
        p, _, _ = ObjectPatch.from_diff(self, self, control_cls=ft.BaseControl)
        msgpack.packb(p.to_message(), default=enc)
        patches.append(p.patch)

    with patch.object(ft.Container, "update", fake_update):
        sheet.on_size_change(SimpleNamespace(width=1403))
        assert len(patches) == 1
        assert patches[-1] == [{"op": "replace", "path": ["width"], "value": 1401}]
        assert all(row.is_isolated() for row in [header, *cards])

        sheet.on_size_change(SimpleNamespace(width=1002))
        assert len(patches) == 2
        assert len(patches[-1]) > 1
        assert all(row.is_isolated() for row in [header, *cards])


def test_app_refresh_caches_rows_and_debounces_search(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    for i in range(3):
        container.profile_manager.add_profile(f"prof-{i}", "", "macos", engine="chrome")

    app = App(container)

    class DummyPage:
        def __init__(self):
            self.title = ""
            self.window = SimpleNamespace(width=1200, height=800, min_width=0, min_height=0)
            self.padding = 0
            self.spacing = 0
            self.fonts = {}
            self.theme_mode = None
            self.bgcolor = ""
            self.theme = None
            self.services = []
            self.controls = []

        def add(self, ctrl):
            self.controls.append(ctrl)

        def update(self):
            pass

        def show_dialog(self, dialog):
            self.dialog = dialog
            dialog.open = True
            self.dialog_stack = ft.Column(controls=[dialog])
            dialog._parent = ref(self.dialog_stack)

        def pop_dialog(self):
            self.dialog.open = False

    app.page = DummyPage()
    app._file_picker = None
    app._build_ui()

    list_calls = 0
    orig_list = app.pm.list_profiles

    def counted_list():
        nonlocal list_calls
        list_calls += 1
        return orig_list()

    monkeypatch.setattr(app.pm, "list_profiles", counted_list)

    app._refresh_profiles()
    assert list_calls == 1
    first_rows = list(app.refs.profile_list_area.controls)
    assert len(first_rows) == 3

    app.state.toggle_selection("prof-1")
    app._refresh_profiles()
    assert list_calls == 2
    second_rows = list(app.refs.profile_list_area.controls)
    assert second_rows[0] is first_rows[0]
    assert second_rows[1] is not first_rows[1]
    assert second_rows[2] is first_rows[2]

    # Toast-only expiry skips full _refresh_profiles()
    app._toast_banner.visible = True
    app._toast_until = 0.0
    refresh_mock = Mock()
    toast_mock = Mock()
    monkeypatch.setattr(app, "_refresh_profiles", refresh_mock)
    monkeypatch.setattr(app, "_update_toast_only", toast_mock)

    async def stop_after_one(_):
        app.page = None

    monkeypatch.setattr("src.ui.app.asyncio.sleep", stop_after_one)
    asyncio.run(app._ui_reconcile_loop())
    toast_mock.assert_called_once()
    refresh_mock.assert_not_called()

    # Dummy page refreshes synchronously on _on_search
    app.page = DummyPage()
    refresh_mock.reset_mock()
    app._on_search("prof-2")
    refresh_mock.assert_called_once()

    # Real ft.Page disables auto-update and debounces via run_task
    tasks = []
    real_like_page = Mock(spec=ft.Page)
    real_like_page.run_task = lambda fn, *args: tasks.append((fn, args))
    app.page = real_like_page
    refresh_mock.reset_mock()
    disable_auto = Mock()
    monkeypatch.setattr(ft.context, "disable_auto_update", disable_auto)
    app._on_search("p")
    app._on_search("prof-1")
    assert disable_auto.call_count == 2
    refresh_mock.assert_not_called()
    assert len(tasks) == 2

    async def fast_sleep(_):
        return None

    monkeypatch.setattr("src.ui.app.asyncio.sleep", fast_sleep)
    stale_fn, stale_args = tasks[0]
    latest_fn, latest_args = tasks[1]
    asyncio.run(stale_fn(*stale_args))
    refresh_mock.assert_not_called()
    asyncio.run(latest_fn(*latest_args))
    refresh_mock.assert_called_once()

    app.page = DummyPage()
    app.state.search_query = "prof-2"
    App._refresh_profiles(app)
    assert app.refs.profile_list_area.controls == [first_rows[2]]

    edit_id = get_string("edit_field", field=get_string("col_id"))
    pencil = next(c for c in _walk(first_rows[2]) if getattr(c, "tooltip", None) == edit_id)
    pencil.on_click(None)
    field = next(c for c in _walk(app.page.dialog.content) if isinstance(c, ft.TextField))
    assert field.value == "3"
    app.page.dialog.actions[-1].on_click(None)

    assert not app.page.dialog.open
    saved_profiles = pm_mod.ProfileManager().list_profiles()
    assert [profile.name for profile in saved_profiles] == ["prof-0", "prof-1", "prof-2"]
    assert all(profile.display_order is None for profile in saved_profiles)


def test_proxy_pool_reload_caches_unchanged_bytes(tmp_path, monkeypatch):
    path = tmp_path / "proxies.json"
    pool = ProxyPoolService(file_path=str(path))
    pool.add_proxy("198.51.100.10:8080", label="A")

    real_loads = pool_mod.json.loads
    loads_calls = 0

    def counting_loads(*args, **kwargs):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(pool_mod.json, "loads", counting_loads)
    pool.reload()
    pool.reload()
    assert loads_calls == 0

    # Same-length, same-mtime overwrite still triggers a reload
    stat = path.stat()
    updated = path.read_text(encoding="utf-8").replace("198.51.100.10", "198.51.100.20")
    assert len(updated.encode("utf-8")) == stat.st_size
    path.write_text(updated, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    pool.reload()
    assert loads_calls == 1
    assert [e.proxy for e in pool.list_entries()] == ["198.51.100.20:8080"]


def test_profile_totp_tick_and_responsive_dialog_skip_noop_updates(monkeypatch):
    page = SimpleNamespace(
        width=900,
        height=600,
        on_resize=None,
        on_keyboard_event=None,
        dialogs=[],
        update=Mock(),
        show_dialog=lambda d: page.dialogs.append(d),
        pop_dialog=lambda: page.dialogs.pop(),
    )
    timer = Mock()
    timer_factory = Mock(return_value=timer)
    monkeypatch.setattr("src.ui.dialogs.profile.threading.Timer", timer_factory)

    open_profile_dialog(page, None, Mock(), Profile("NoTOTP", engine="chrome"))
    page.update.reset_mock()
    tick = timer_factory.call_args.args[1]
    tick()
    page.update.assert_not_called()
    page.dialogs[-1].on_dismiss(None)

    # With a valid 2FA secret, tick updates when text changes
    page2 = SimpleNamespace(
        width=900,
        height=600,
        on_resize=None,
        on_keyboard_event=None,
        dialogs=[],
        update=Mock(),
        show_dialog=lambda d: page2.dialogs.append(d),
        pop_dialog=lambda: page2.dialogs.pop(),
    )
    timer_factory.reset_mock()
    with patch("src.utils.totp.time.time", return_value=29):
        open_profile_dialog(
            page2,
            None,
            Mock(),
            Profile("WithTOTP", engine="chrome", two_fa_secret="JBSWY3DPEHPK3PXP"),
        )
    page2.update.reset_mock()
    tick2 = timer_factory.call_args.args[1]
    with patch("src.utils.totp.time.time", return_value=30):
        tick2()
    page2.update.assert_called_once()
    page2.dialogs[-1].on_dismiss(None)

    # Responsive dialog skips update on unchanged dimensions
    r_page = SimpleNamespace(width=900, height=600, on_resize=None, update=Mock())
    dlg = ft.AlertDialog()
    body = ft.Container()
    bind_responsive_dialog(r_page, dlg, body, base_width=800, base_height=500)
    disable_auto = Mock()
    monkeypatch.setattr(ft.context, "disable_auto_update", disable_auto)
    r_page.on_resize(None)
    disable_auto.assert_called_once()
    r_page.update.assert_not_called()

    r_page.width = 1100
    r_page.on_resize(None)
    r_page.update.assert_called_once()


def test_bulk_header_cache_transitions_and_row_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))

    container = Container()
    for name in ("Atlas", "Boreal", "Cedar"):
        assert container.profile_manager.add_profile(name, "", "macos", engine="chrome")

    app = App(container)
    page = SimpleNamespace(
        title="",
        window=SimpleNamespace(width=1440, height=800, min_width=0, min_height=0),
        padding=0,
        spacing=0,
        fonts={},
        theme_mode=None,
        bgcolor="",
        theme=None,
        services=[],
        controls=[],
        add=lambda ctrl: page.controls.append(ctrl),
        update=lambda: None,
    )
    app.page = page
    app._file_picker = None
    app._profile_table_width = 1400
    app._build_ui()

    initial_header = app._header_slot.content
    assert isinstance(initial_header, ProfileRow)
    assert initial_header.height == 40
    assert initial_header.is_isolated()
    rows_v1 = list(app.refs.profile_list_area.controls)

    # Selecting 1 profile swaps header to non-ProfileRow bulk container of height 40.
    app.state.toggle_selection("Atlas")
    app._refresh_profiles()
    bulk_header_1 = app._header_slot.content
    assert bulk_header_1 is not initial_header
    assert not isinstance(bulk_header_1, ProfileRow)
    assert bulk_header_1.height == 40
    rows_v2 = list(app.refs.profile_list_area.controls)
    assert rows_v2[0] is not rows_v1[0]
    assert rows_v2[1] is rows_v1[1]
    assert rows_v2[2] is rows_v1[2]

    # Selecting a 2nd profile (not yet all selected) reuses unaffected row and updates bulk count.
    app.state.toggle_selection("Boreal")
    app._refresh_profiles()
    assert app._header_slot.content is bulk_header_1
    assert "2" in app.refs.bulk_bar.controls[0].controls[0].value
    rows_v3 = list(app.refs.profile_list_area.controls)
    assert rows_v3[0] is rows_v2[0]
    assert rows_v3[1] is not rows_v2[1]
    assert rows_v3[2] is rows_v2[2]

    # Searching hides selected profiles while keeping bulk header visible.
    app._on_search("NoSuchProfile")
    assert app._header_slot.content is not None
    assert not isinstance(app._header_slot.content, ProfileRow)
    assert app._header_slot.content.height == 40
    assert app.refs.bulk_bar.visible is True
    assert app.refs.bulk_bar.controls[0].controls[1].visible is True

    # Clearing filter & selection restores an isolated ProfileRow header.
    app.state.clear_selection()
    app._clear_filters()
    restored_header = app._header_slot.content
    assert isinstance(restored_header, ProfileRow)
    assert restored_header.height == 40
    assert restored_header.is_isolated()


def test_reconcile_loop_dispatches_extensions_profiles_and_toast_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))

    app = App(Container())
    app.page = SimpleNamespace(
        title="",
        window=SimpleNamespace(width=1200, height=800, min_width=0, min_height=0),
        padding=0,
        spacing=0,
        fonts={},
        theme_mode=None,
        bgcolor="",
        theme=None,
        services=[],
        controls=[],
        add=lambda ctrl: app.page.controls.append(ctrl),
        update=lambda: None,
    )
    app._file_picker = None
    app._build_ui()

    ext_mock = Mock()
    prof_mock = Mock()
    toast_mock = Mock()
    monkeypatch.setattr(app, "_refresh_profiles_and_extensions", ext_mock)
    monkeypatch.setattr(app, "_refresh_profiles", prof_mock)
    monkeypatch.setattr(app, "_update_toast_only", toast_mock)

    async def stop_after_one(_):
        app.page = None

    monkeypatch.setattr("src.ui.app.asyncio.sleep", stop_after_one)

    # 1. refresh_requested=True -> _refresh_profiles_and_extensions() only
    saved_page = app.page
    app.state.schedule_refresh()
    asyncio.run(app._ui_reconcile_loop())
    ext_mock.assert_called_once()
    prof_mock.assert_not_called()
    toast_mock.assert_not_called()

    # 2. changed=True (running snapshot changed) -> _refresh_profiles() only
    ext_mock.reset_mock()
    app.page = saved_page
    monkeypatch.setattr(app.bl, "running_profile_names", lambda: {"Atlas"})
    app.state._last_running_snapshot = set()
    asyncio.run(app._ui_reconcile_loop())
    prof_mock.assert_called_once()
    ext_mock.assert_not_called()
    toast_mock.assert_not_called()

    # 3. toast_expired=True alone -> _update_toast_only() only
    prof_mock.reset_mock()
    app.page = saved_page
    app.state._last_running_snapshot = {"Atlas"}
    app._toast_banner.visible = True
    app._toast_until = 0.0
    asyncio.run(app._ui_reconcile_loop())
    toast_mock.assert_called_once()
    prof_mock.assert_not_called()
    ext_mock.assert_not_called()
