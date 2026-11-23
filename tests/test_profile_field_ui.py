"""Field-specific native editors use local drafts and the existing commit callback."""

from types import SimpleNamespace

import flet as ft
import pytest

from src.models.profile import Profile
from src.ui.components.profile_card import build_profile_card
from src.ui.dialogs.profile_field import open_profile_field_dialog


class Page:
    def __init__(self):
        self.dialog = None
        self.closed = False

    def show_dialog(self, dialog):
        dialog.open = True
        self.dialog = dialog

    def pop_dialog(self):
        self.dialog.open = False
        self.closed = True

    def update(self):
        pass


@pytest.mark.parametrize("field_editor", [False, True])
def test_rename_warming_save_error_is_localized(monkeypatch, field_editor):
    from src.core.strings import get_string
    from src.services.warming.scheduler import WarmingTargetSaveError
    from src.ui.actions import profile as profile_actions
    from src.ui.app import App
    from src.ui.dialogs import profile_field

    def reject(*_args, **_kwargs):
        raise WarmingTargetSaveError("read-only warming store")

    profile = Profile(name="Before")
    pm = SimpleNamespace(
        profiles={profile.name: profile}, update_profile=reject, list_profiles=lambda: [profile]
    )
    bl = SimpleNamespace(is_running=lambda _: False)
    callbacks = []
    if field_editor:

        def capture(_page, _profile, _field, save, **_kwargs):
            callbacks.append(save)

        monkeypatch.setattr(profile_field, "open_profile_field_dialog", capture)
        App._on_edit_field(SimpleNamespace(pm=pm, bl=bl, page=Page()), profile.name, "name")
        error = callbacks[0]("After")
    else:

        def capture(_page, _ps, save, *_args, **_kwargs):
            callbacks.append(save)

        monkeypatch.setattr(profile_actions, "open_profile_dialog", capture)
        profile_actions.edit_profile(Page(), profile.name, pm, bl, None, None, None)
        error = callbacks[0]("After", "", "linux", "", "", "chrome")
    assert error == get_string("warming_rename_save_error")


def walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from walk(child)
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from walk(content)


@pytest.mark.parametrize("initial,target", [("light", "dark"), ("dark", "light")])
def test_chromium_warning_uses_current_theme(initial, target):
    from src.core.strings import get_string
    from src.ui.dialogs.profile import open_profile_dialog
    from src.ui.theme.colors import COLORS, current_theme_mode, set_theme_mode
    from src.ui.theme.styles import refresh_styles

    previous = current_theme_mode()
    try:
        set_theme_mode(initial)
        refresh_styles()
        set_theme_mode(target)
        refresh_styles()
        page = Page()
        open_profile_dialog(page, None, lambda *args, **kwargs: None)
        warning = next(
            c
            for c in walk(page.dialog.content)
            if c.__class__.__name__ == "Container"
            and any(
                getattr(child, "value", None) == get_string("engine_chrome_note")
                for child in getattr(getattr(c, "content", None), "controls", [])
            )
        )
        assert warning.bgcolor == COLORS["warn_tint"]
    finally:
        set_theme_mode(previous)
        refresh_styles()


def test_chrome_m2_desktop_cpu_picker_uses_chip_options(monkeypatch):
    from chrome_engine.persona import generate
    from src.services.browser import fingerprint
    from src.ui.dialogs.profile import open_profile_dialog

    persona = generate("macos", seed=495, coherence_026=True).to_json()
    persona["hardware_concurrency"] = 14
    monkeypatch.setattr(fingerprint, "load_saved_chrome_persona", lambda _: persona)
    page = Page()
    open_profile_dialog(
        page, None, lambda *_args: None, Profile("m2", engine="chrome", os_type="macos")
    )
    cpu = next(
        control
        for control in walk(page.dialog)
        if isinstance(control, ft.Dropdown) and any(option.key == "8" for option in control.options)
    )
    assert [option.key for option in cpu.options] == ["8"]
    assert cpu.value == "8"


def test_native_field_dialog_changes_name_not_account():
    page, saved = Page(), []
    profile = Profile("old", account_uid="fixed@example.test")
    open_profile_field_dialog(page, profile, "name", lambda value: saved.append(value))
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = "new"
    page.dialog.actions[-1].on_click(None)
    assert saved == ["new"] and page.closed
    assert profile.name == "old" and profile.account_uid == "fixed@example.test"


def test_cancel_and_save_error_preserve_draft():
    page = Page()
    profile = Profile("fixture", notes="before")
    open_profile_field_dialog(page, profile, "notes", lambda _: "Store busy")
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = "after"
    page.dialog.actions[-1].on_click(None)
    assert not page.closed and profile.notes == "before"
    assert any(getattr(c, "value", None) == "Store busy" for c in walk(page.dialog.content))
    page.dialog.actions[0].on_click(None)
    assert page.closed and profile.notes == "before"


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "1000001"])
def test_order_rejects_invalid_values(value):
    page, saved = Page(), []
    open_profile_field_dialog(page, Profile("fixture"), "display_order", saved.append)
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = value
    page.dialog.actions[-1].on_click(None)
    assert not page.closed and saved == []


def test_tags_are_local_and_removable():
    page, saved = Page(), []
    p = Profile("fixture", group="Gmail")
    open_profile_field_dialog(page, p, "tags", saved.append)
    controls = list(walk(page.dialog.content))
    field = next(c for c in controls if c.__class__.__name__ == "TextField")
    field.value = "AGY"
    field.on_submit(None)
    remove = next(
        c for c in walk(page.dialog.content) if getattr(c, "tooltip", None) == "Remove Gmail"
    )
    remove.on_click(None)
    assert p.group == "Gmail" and not p.tags
    page.dialog.actions[-1].on_click(None)
    assert saved == [["AGY"]]


def test_card_exposes_copy_and_five_field_editors():
    clicked = []
    p = Profile("fixture", account_uid="user@example.test")
    card = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_copy_user=lambda n: clicked.append(n),
        on_edit_field=lambda n, f, i: clicked.append((n, f, i)),
    )
    buttons = [c for c in walk(card) if c.__class__.__name__ == "IconButton"]
    edits = [
        c
        for c in buttons
        if getattr(c, "icon_size", None) == 14 and c.width == 28 and c.height == 36
    ]
    assert len(edits) == 5
    for button in edits:
        button.on_click(SimpleNamespace())
    assert {v[1] for v in clicked} == {"name", "display_order", "tags", "proxy", "notes"}
    copy = next(c for c in buttons if c.height == 28)
    copy.on_click(None)
    assert clicked[-1] == "fixture"


@pytest.mark.parametrize(("engine", "label"), [("camoufox", "Camoufox"), ("chrome", "Chrome")])
def test_card_engine_badge_does_not_wrap(engine, label):
    card = build_profile_card(
        Profile("fixture", engine=engine),
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_edit_field=lambda *_: None,
    )
    engine_badge = next(c for c in walk(card) if getattr(c, "value", None) == label)
    assert engine_badge.no_wrap


def test_card_active_feedback_renders_checkmarks():
    import flet as ft

    from src.ui.theme.colors import COLORS

    p = Profile(
        "fixture",
        account_uid="user@example.test",
        account_password="pw",
        two_fa_secret="JBSWY3DPEHPK3PXP",
    )
    card = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_copy_user=lambda _: None,
        on_copy_pass=lambda _: None,
        on_copy_2fa=lambda _: None,
        on_edit_field=lambda *_: None,
        active_feedback={"copy_user", "copy_pass", "copy_2fa", "edit_name", "edit_profile"},
    )
    buttons = [c for c in walk(card) if c.__class__.__name__ == "IconButton"]
    checked = [
        b for b in buttons if b.icon == ft.Icons.CHECK_ROUNDED and b.icon_color == COLORS["live"]
    ]
    assert len(checked) == 5


def test_appearance_dialog_switches_language_theme_and_hallmark_accent():
    from src.core.strings import current_language, set_language
    from src.ui.dialogs.appearance import open_appearance_dialog
    from src.ui.theme.colors import (
        COLORS,
        current_accent_color,
        current_theme_mode,
        set_accent_color,
        set_theme_mode,
    )

    prev_lang = current_language()
    prev_theme = current_theme_mode()
    prev_accent = current_accent_color()
    page = Page()
    try:
        open_appearance_dialog(
            page,
            on_language=set_language,
            on_theme=set_theme_mode,
            on_accent=set_accent_color,
        )
        by_key = {
            getattr(c, "key", None): c for c in walk(page.dialog.content) if getattr(c, "key", None)
        }
        assert {
            "appearance_lang_select",
            "appearance_theme_light",
            "appearance_theme_dark",
            "appearance_color_slate",
            "appearance_color_blue",
            "appearance_color_green",
            "appearance_color_orange",
        }.issubset(by_key)

        by_key["appearance_lang_select"].on_click(None)
        by_key = {
            getattr(c, "key", None): c for c in walk(page.dialog.content) if getattr(c, "key", None)
        }
        by_key["appearance_lang_vi"].on_click(None)
        assert current_language() == "vi"

        by_key = {
            getattr(c, "key", None): c for c in walk(page.dialog.content) if getattr(c, "key", None)
        }
        by_key["appearance_theme_light"].on_click(None)
        assert current_theme_mode() == "light"

        for preset, expected_cta in (
            ("slate", "#0F172A"),
            ("blue", "#2563EB"),
            ("green", "#059669"),
            ("orange", "#EA580C"),
        ):
            by_key = {
                getattr(c, "key", None): c
                for c in walk(page.dialog.content)
                if getattr(c, "key", None)
            }
            by_key[f"appearance_color_{preset}"].on_click(None)
            assert current_accent_color() == preset
            assert COLORS["primary_cta_bg"] == expected_cta
    finally:
        set_language(prev_lang)
        set_accent_color(prev_accent)
        set_theme_mode(prev_theme)


def test_flet_all_tags_scrollable_notes_backup_codes_pagination_and_running_filter(
    tmp_path, monkeypatch
):
    import asyncio
    import logging

    from src.core import logging as log_mod
    from src.services.profile import manager as pm_mod
    from src.ui.app import App
    from src.ui.components.content_area import build_content_area
    from src.ui.components.factory import build_ui_refs
    from src.ui.dialogs.export import open_export_dialog
    from src.ui.dialogs.profile import open_profile_dialog
    from src.ui.state import PAGE_SIZE_OPTIONS

    # 1. All tags render without "+1/+2" truncation, notes row scrolls horizontally, and backup codes button works
    bkp_clicked = []
    canary = "1111 0001"
    sheet_a = f"1. {canary}\n2. 1111 0002"
    sheet_b = "1. 2222 0001\n2. 2222 0002"
    long_note = "Fam AGY Pixel fixture-long@example.test full note without ellipsis cutoff"
    p = Profile(
        "multi-tag-prof",
        tags=["Gmail", "Gemini Pixel Fam", "Antigravity", "Muse"],
        notes=long_note,
        backup_codes=sheet_a,
    )
    card = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_edit_field=lambda n, f, i: bkp_clicked.append((n, f, i)),
        row_index=3,
    )
    texts = [
        getattr(c, "value", "")
        for c in walk(card)
        if c.__class__.__name__ == "Text" and getattr(c, "value", None)
    ]
    for expected_tag in ("Gmail", "Gemini Pixel Fam", "Antigravity", "Muse"):
        assert expected_tag in texts
    assert not any(t.startswith("+") and t[1:].isdigit() for t in texts)
    assert long_note in texts

    by_key = {getattr(c, "key", None): c for c in walk(card) if getattr(c, "key", None)}
    assert "notes_scroll_multi-tag-prof" in by_key
    assert "backup_codes_btn_multi-tag-prof" in by_key
    by_key["backup_codes_btn_multi-tag-prof"].on_click(None)
    assert bkp_clicked == [("multi-tag-prof", "backup_codes", 3)]

    # 2. Backup codes quick dialog renders extracted code chips, Copy All button, and commits edit
    page, saved, copied = Page(), [], []
    open_profile_field_dialog(
        page,
        p,
        "backup_codes",
        lambda val: saved.append(val),
        on_copy_code=lambda code: copied.append(code),
    )
    dlg_by_key = {
        getattr(c, "key", None): c for c in walk(page.dialog.content) if getattr(c, "key", None)
    }
    assert "backup_code_chip_1" in dlg_by_key
    assert "backup_code_chip_2" in dlg_by_key
    assert "copy_all_backup_codes_btn" in dlg_by_key
    dlg_by_key["backup_code_chip_1"].on_click(None)
    assert copied == [canary]
    dlg_by_key["copy_all_backup_codes_btn"].on_click(None)
    assert copied[-1] == sheet_a

    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = "1. 1111 0003\n2. 1111 0004\n3. 1111 0005"
    field.on_change(None)
    dlg_by_key = {
        getattr(c, "key", None): c for c in walk(page.dialog.content) if getattr(c, "key", None)
    }
    assert "backup_code_chip_3" in dlg_by_key
    page.dialog.actions[-1].on_click(None)
    assert saved == ["1. 1111 0003\n2. 1111 0004\n3. 1111 0005"] and page.closed

    # 3. Single-code & full-sheet copy must NEVER leak codes into UI log, toast, or disk log file
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    assert pm.add_profile("canary-prof", "", "macos", backup_codes=sheet_a, notes="initial-note")

    root_log = logging.getLogger("foxprofile")
    saved_handlers = list(root_log.handlers)
    for handler in saved_handlers:
        root_log.removeHandler(handler)
    log_dir = tmp_path / "logs"
    log_mod.setup_logging(str(log_dir))
    try:

        class StubClipboard:
            def __init__(self):
                self.value = None

            async def set(self, val):
                self.value = val

        class StubPage(Page):
            def __init__(self):
                super().__init__()
                self.on_resize = None
                self.width = 1280
                self.height = 800

            def run_task(self, fn):
                asyncio.run(fn())

        container = SimpleNamespace(
            profile_manager=pm,
            browser_launcher=SimpleNamespace(
                is_running=lambda _: False,
                running_profile_names=lambda: set(),
                running_count=lambda: 0,
            ),
            proxy_service=SimpleNamespace(),
            proxy_pool=SimpleNamespace(list_entries=lambda: []),
            extension_service=SimpleNamespace(list_entries=lambda: []),
            warming_scheduler=SimpleNamespace(list_tasks=lambda: []),
            event_bus=SimpleNamespace(subscribe=lambda *_: None),
        )
        gui = App(container)
        gui.page = StubPage()
        gui.clipboard = StubClipboard()
        gui.refs = build_ui_refs(
            pm=pm,
            on_change_page=lambda _: None,
            file_picker=None,
            on_search=lambda _: None,
        )
        gui._refresh_profiles = lambda: None
        gui._on_edit_field("canary-prof", "backup_codes", 1)
        chip_btn = next(
            c
            for c in walk(gui.page.dialog.content)
            if getattr(c, "key", None) == "backup_code_chip_1"
        )
        chip_btn.on_click(None)
        assert gui.clipboard.value == canary
        copy_all_btn = next(
            c
            for c in walk(gui.page.dialog.content)
            if getattr(c, "key", None) == "copy_all_backup_codes_btn"
        )
        copy_all_btn.on_click(None)
        assert gui.clipboard.value == sheet_a
        for handler in root_log.handlers:
            handler.flush()
        disk_logs = "".join(p.read_text(encoding="utf-8") for p in log_dir.glob("*.log"))
        assert canary not in (gui._toast_text.value or "")
        assert not any(canary in line for line in gui.state.get_all_log_lines())
        assert canary not in disk_logs

        # 3b. Quick editor no-op Save (including edit-then-revert with CRLF/whitespace) skips write and preserves concurrent sheet B
        assert pm.update_profile("canary-prof", "canary-prof", "", "macos", backup_codes=sheet_b)
        bkp_entry = next(
            c for c in walk(gui.page.dialog.content) if c.__class__.__name__ == "TextField"
        )
        bkp_entry.value = "1. 9999 0001"
        bkp_entry.on_change(None)
        bkp_entry.value = f"  {sheet_a.replace(chr(10), chr(13) + chr(10))}  \n"
        bkp_entry.on_change(None)
        gui.page.dialog.actions[-1].on_click(None)
        assert gui.page.closed is True
        assert pm.profiles["canary-prof"].backup_codes == sheet_b
        assert ("canary-prof", "edit_backup_codes") not in gui._feedback_until

        # 3c. Quick editor real edit and intentional clear still persist via callback + ProfileManager
        sheet_c = "1. 3333 0001\n2. 3333 0002"
        gui.page.closed = False
        gui._on_edit_field("canary-prof", "backup_codes", 1)
        bkp_entry = next(
            c for c in walk(gui.page.dialog.content) if c.__class__.__name__ == "TextField"
        )
        bkp_entry.value = sheet_c
        gui.page.dialog.actions[-1].on_click(None)
        assert gui.page.closed is True
        assert pm.profiles["canary-prof"].backup_codes == sheet_c

        gui.page.closed = False
        gui._on_edit_field("canary-prof", "backup_codes", 1)
        bkp_entry = next(
            c for c in walk(gui.page.dialog.content) if c.__class__.__name__ == "TextField"
        )
        bkp_entry.value = "   "
        gui.page.dialog.actions[-1].on_click(None)
        assert gui.page.closed is True
        assert pm.profiles["canary-prof"].backup_codes is None
        assert pm.update_profile("canary-prof", "canary-prof", "", "macos", backup_codes=sheet_a)
    finally:
        for handler in list(root_log.handlers):
            root_log.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root_log.addHandler(handler)

    # 4. Flet full dialog notes-only edit omits unchanged backup_codes & preserves concurrent sheet B
    edit_page = Page()
    captured_extra = {}

    def on_full_save(name, proxy, os_type, tz, loc, _eng, **extra):
        captured_extra.clear()
        captured_extra.update(extra)
        pm.update_profile("canary-prof", name, proxy, os_type, tz, loc, **extra)
        return None

    open_profile_dialog(
        edit_page, None, on_full_save, pm.profiles["canary-prof"], profile_manager=pm
    )
    assert pm.update_profile("canary-prof", "canary-prof", "", "macos", backup_codes=sheet_b)
    notes_box = next(
        c for c in walk(edit_page.dialog.content) if getattr(c, "value", None) == "initial-note"
    )
    notes_box.value = "notes-only-update"
    edit_page.dialog.actions[-1].content.on_click(None)
    assert "backup_codes" not in captured_extra
    assert pm.profiles["canary-prof"].notes == "notes-only-update"
    assert pm.profiles["canary-prof"].backup_codes == sheet_b
    edit_page.dialog.on_dismiss(None)

    # Explicit clear via Flet full dialog clears backup_codes
    clear_page = Page()
    open_profile_dialog(
        clear_page, None, on_full_save, pm.profiles["canary-prof"], profile_manager=pm
    )
    bkp_box = next(
        c
        for c in walk(clear_page.dialog.content)
        if getattr(c, "key", None) == "dialog_backup_codes_field"
    )
    bkp_box.value = ""
    clear_page.dialog.actions[-1].content.on_click(None)
    assert captured_extra["backup_codes"] == ""
    assert pm.profiles["canary-prof"].backup_codes is None
    clear_page.dialog.on_dismiss(None)

    # 5. Export dialog surfaces ZIP credential warning even when browser data is unchecked
    exp_page = Page()
    open_export_dialog(exp_page, None, [p], lambda *_: None)
    assert any(
        getattr(c, "key", None) == "export_credentials_warning"
        for c in walk(exp_page.dialog.content)
    )

    # 6. Content area exposes horizontal scroll row, rows-per-page dropdown (5..200), first/last page, and running filter button
    sizes, pages_nav, run_toggled = [], [], []
    refs = build_ui_refs(
        pm=SimpleNamespace(profiles={"a": p}),
        on_change_page=lambda d: pages_nav.append(d),
        file_picker=None,
        on_search=lambda _: None,
        on_go_page=lambda target: pages_nav.append(target),
        on_change_page_size=lambda s: sizes.append(s),
        on_toggle_running_filter=lambda: run_toggled.append(True),
    )
    area = build_content_area(
        refs.content_subtitle,
        refs.profile_list_area,
        refs.prev_btn,
        refs.next_btn,
        refs.page_label,
        refs.bulk_bar,
        refs.search_field,
        first_btn=refs.first_btn,
        last_btn=refs.last_btn,
        page_size_dropdown=refs.page_size_dropdown,
        running_filter_btn=refs.running_filter_btn,
    )
    area_keys = {getattr(c, "key", None): c for c in walk(area) if getattr(c, "key", None)}
    assert {
        "profiles_horizontal_scroll_row",
        "page_size_dropdown",
        "page_first_btn",
        "page_last_btn",
        "toolbar_running_filter_btn",
    }.issubset(area_keys)
    assert area_keys["page_size_dropdown"].width == 100
    assert [int(opt.key) for opt in refs.page_size_dropdown.options] == list(PAGE_SIZE_OPTIONS)
    refs.page_size_dropdown.value = "50"
    refs.page_size_dropdown.on_select(SimpleNamespace(control=refs.page_size_dropdown))
    assert sizes == [50]
    refs.running_filter_btn.on_click(None)
    assert run_toggled == [True]
    refs.first_btn.on_click(None)
    refs.last_btn.on_click(None)
    assert pages_nav == ["first", "last"]
