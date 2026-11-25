"""Regression coverage for native dialog sizing and Chromium GPU identity."""

from types import SimpleNamespace
from unittest.mock import Mock

import flet as ft
import pytest

from src.core.strings import current_language, get_string, set_language
from src.models.profile import Profile
from src.ui.dialogs.appearance import open_appearance_dialog
from src.ui.dialogs.cookies import open_cookie_dialog
from src.ui.dialogs.extensions import open_extensions_dialog
from src.ui.dialogs.profile import _gpu_key, open_profile_dialog
from src.ui.dialogs.profile_field import FIELD_LABELS, open_profile_field_dialog


@pytest.mark.parametrize("language,heading", [("vi", "Giao diện"), ("en", "Theme")])
def test_appearance_theme_heading_is_localized(language, heading):
    original = current_language()
    try:
        set_language(language)
        dialog = open_appearance_dialog(
            page_fake(), on_language=Mock(), on_theme=Mock(), on_accent=Mock()
        )
        assert any(isinstance(c, ft.Text) and c.value == heading for c in walk(dialog.content))
    finally:
        set_language(original)


def page_fake(width=900, height=600):
    return SimpleNamespace(
        width=width,
        height=height,
        on_resize=None,
        update=Mock(),
        pop_dialog=Mock(),
        show_dialog=Mock(),
    )


def walk(control):
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from walk(content)


@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("field", ["backup_codes", "proxy", "tags"])
def test_floating_field_label_has_room_inside_scroll_viewport(language, field):
    original = current_language()
    try:
        set_language(language)
        page = page_fake()
        open_profile_field_dialog(page, Profile("Chromium demo", engine="chrome"), field, Mock())
        dialog = page.show_dialog.call_args.args[0]
        viewport = dialog.content.content
        assert viewport.scroll == ft.ScrollMode.AUTO
        label = get_string(FIELD_LABELS[field])
        wrapper = next(
            (
                control
                for control in walk(viewport)
                if isinstance(control, ft.Container)
                and isinstance(control.content, ft.TextField)
                and control.content.label == label
            ),
            None,
        )
        assert wrapper is not None and wrapper.padding is not None and wrapper.padding.top > 0
        assert wrapper.content.autofocus
    finally:
        set_language(original)


@pytest.mark.parametrize("explicit_os", [None, "windows", "macos", "linux"])
def test_create_chrome_uses_host_os_unless_user_picked_os(explicit_os):
    from chrome_engine.release import host_os

    page = page_fake()
    open_profile_dialog(page, None, Mock())
    dialog = page.show_dialog.call_args.args[0]
    dropdowns = [c for c in walk(dialog.content) if isinstance(c, ft.Dropdown)]
    engine = next(c for c in dropdowns if {o.key for o in c.options} == {"camoufox", "chrome"})
    os_picker = next(
        c for c in dropdowns if {o.key for o in c.options} == {"windows", "macos", "linux"}
    )
    try:
        assert os_picker.value == "macos"
        if explicit_os is not None:
            os_picker.value = explicit_os
            os_picker.on_select(None)
        engine.value = "chrome"
        engine.on_select(None)
        assert os_picker.value == (explicit_os or host_os())
        engine.value = "camoufox"
        engine.on_select(None)
        assert os_picker.value == (explicit_os or "macos")
    finally:
        dialog.on_dismiss(None)


def test_empty_extensions_do_not_expand_inside_a_wrapping_row():
    page = page_fake()
    service = SimpleNamespace(list_entries=lambda: [], usage_counts=lambda _: {})
    open_extensions_dialog(page, service, SimpleNamespace())
    dialog = page.show_dialog.call_args.args[0]
    empty = next(
        c
        for c in walk(dialog.content)
        if isinstance(c, ft.Text) and c.value == get_string("ext_empty_hint")
    )
    assert empty.visible
    for row in (c for c in walk(dialog.content) if isinstance(c, ft.Row) and c.wrap):
        assert not any(c.expand for c in row.controls)


@pytest.mark.parametrize("width,height", [(900, 600), (1440, 880)])
def test_cookie_dialog_shrink_wraps_but_can_scroll(width, height):
    page = page_fake(width, height)
    open_cookie_dialog(page, "Demo Chromium", "No fingerprint yet", Mock(), Mock(), Mock())
    dialog = page.show_dialog.call_args.args[0]
    assert dialog.scrollable
    assert dialog.content.height is None
    assert dialog.content.content.tight
    assert dialog.content.content.scroll is None
    buttons = [c for c in walk(dialog.content) if isinstance(c, ft.OutlinedButton)]
    assert len(buttons) == 4 and all(c.on_click for c in buttons)


def test_appearance_menu_can_scroll_without_losing_color_selection():
    page = page_fake()
    select = Mock()
    dialog = open_appearance_dialog(page, on_language=Mock(), on_theme=Mock(), on_accent=select)
    menu = next(c for c in walk(dialog.content) if c.key == "appearance_lang_select")
    menu.on_click(None)
    assert any(c.key == "appearance_lang_vi" for c in walk(dialog.content))
    assert dialog.scrollable
    swatches = next(
        c
        for c in walk(dialog.content)
        if isinstance(c, ft.Row)
        and c.controls
        and str(c.controls[0].key).startswith("appearance_color_")
    )
    swatches.controls[-1].on_click(None)
    select.assert_called_once_with(swatches.controls[-1].key.removeprefix("appearance_color_"))


def test_chrome_picker_uses_saved_launch_persona_and_updates_after_roll(monkeypatch):
    from chrome_engine.persona import Persona
    from src.ui.dialogs import profile as ui

    persona = Persona(
        seed=18, platform="windows", platform_version="15.0.0", hardware_concurrency=8
    ).to_json()
    monkeypatch.setattr(ui.fp_service, "load_saved_chrome_persona", lambda _: persona)
    monkeypatch.setattr(ui.fp_service, "generate_chrome_persona", lambda *a, **k: persona)
    page = page_fake()
    open_profile_dialog(
        page, None, Mock(), Profile("Demo Chromium", engine="chrome", os_type="windows")
    )
    dialog = page.show_dialog.call_args.args[0]

    def assert_gpu_matches_launch():
        loaded = Persona(**persona)
        assert f"--fingerprint={persona['seed']}" in loaded.to_args()
        details = ui.fp_service.extract_chrome_details(persona, "windows")
        expected = _gpu_key(details["gpu_vendor"], details["gpu_renderer"])
        picker = next(
            c for c in walk(dialog.content) if isinstance(c, ft.Dropdown) and c.value == expected
        )
        assert picker.disabled
        assert len(picker.options) == 1
        assert details["gpu_renderer"].split(" · ")[0] in picker.options[0].text

    try:
        assert_gpu_matches_launch()
        persona["seed"] = 21
        next(
            c
            for c in walk(dialog.content)
            if isinstance(c, ft.OutlinedButton) and c.icon == ft.Icons.CASINO_OUTLINED
        ).on_click(None)
        assert_gpu_matches_launch()
    finally:
        dialog.on_dismiss(None)


@pytest.mark.parametrize("dpr", [None, 1.5])
def test_chrome_display_summary_only_shows_pinned_dpr(monkeypatch, dpr):
    from chrome_engine.persona import Persona
    from src.ui.dialogs import profile as ui

    persona = Persona(
        seed=18, platform="macos", platform_version="15.0.0", device_scale_factor=dpr
    ).to_json()
    monkeypatch.setattr(ui.fp_service, "load_saved_chrome_persona", lambda _: persona)
    page = page_fake()
    open_profile_dialog(
        page, None, Mock(), Profile("Scout Chromium", engine="chrome", os_type="macos")
    )
    dialog = page.show_dialog.call_args.args[0]
    try:
        display_row = next(
            c
            for c in walk(dialog.content)
            if isinstance(c, ft.Row)
            and c.controls
            and isinstance(c.controls[0], ft.Text)
            and c.controls[0].value == "Screen / Display"
        )
        summary = display_row.controls[1].value
        assert summary == (
            "C++ Seed / Viewport · 24-bit"
            if dpr is None
            else "C++ Seed / Viewport · DPR 1.5 · 24-bit"
        )
    finally:
        dialog.on_dismiss(None)


def _dialog_from(page):
    return page.show_dialog.call_args.args[0]


def _open_warming(tmp_path, names):
    from src.services.warming.scheduler import WarmingSchedulerService
    from src.ui.dialogs.warming import open_warming_dialog

    page = page_fake()
    pm = SimpleNamespace(list_profiles=lambda: [SimpleNamespace(name=n) for n in names])
    service = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))
    open_warming_dialog(page, service, pm, SimpleNamespace())
    return list(walk(_dialog_from(page).content))


@pytest.mark.parametrize("language,name", [("vi", "Ngâm 2 hồ sơ"), ("en", "Warm 2 profile(s)")])
def test_warming_default_task_name_is_localized(tmp_path, language, name):
    original = current_language()
    try:
        set_language(language)
        fields = [c for c in _open_warming(tmp_path, ["a", "b"]) if isinstance(c, ft.TextField)]
        assert fields[0].value == name
    finally:
        set_language(original)


@pytest.mark.parametrize("language", ["vi", "en"])
def test_warming_schedule_hint_fits_its_field(tmp_path, language):
    original = current_language()
    try:
        set_language(language)
        hint = get_string("warming_schedule_hint")
        field = next(
            c
            for c in _open_warming(tmp_path, ["a"])
            if isinstance(c, ft.TextField) and c.hint_text == hint
        )
        # JetBrains Mono advances every glyph by 0.6 em; leave 16 px of padding per side.
        assert field.width >= len(hint) * 0.6 * field.text_style.size + 32
    finally:
        set_language(original)


@pytest.mark.parametrize("language", ["vi", "en"])
def test_proxy_pool_tag_field_fills_its_row(tmp_path, language):
    from src.services.proxy.pool import ProxyPoolService
    from src.ui.dialogs.proxy_pool import open_proxy_pool_dialog

    original = current_language()
    try:
        set_language(language)
        page = page_fake()
        pm = SimpleNamespace(list_profiles=lambda: [])
        pool = ProxyPoolService(file_path=str(tmp_path / "proxies.json"))
        open_proxy_pool_dialog(page, pool, SimpleNamespace(), pm)
        hint = get_string("proxy_pool_tag_hint")
        row = next(
            c
            for c in walk(_dialog_from(page).content)
            if isinstance(c, ft.Row)
            and any(isinstance(x, ft.TextField) and x.hint_text == hint for x in c.controls)
        )
        field = next(x for x in row.controls if isinstance(x, ft.TextField))
        assert not row.wrap
        assert field.width is None and field.expand
    finally:
        set_language(original)
