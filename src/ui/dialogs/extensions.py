from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DANGER_ICON_STYLE, ICON_STYLE, OUTLINE_STYLE
from .responsive import bind_responsive_dialog

if TYPE_CHECKING:
    from ...interfaces.protocols import IProfileManager
    from ...services.browser.extensions import ExtensionEntry, ExtensionService


def open_extension_settings_dialog(
    page: ft.Page,
    entry: ExtensionEntry,
    extension_service: ExtensionService,
    pm: IProfileManager,
    on_saved: Callable[[], None] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Open the per-extension Settings modal (Pinning mode + Profile assignment, matching Ảnh 3)."""
    log = log_fn or (lambda _: None)

    pin_group = ft.RadioGroup(
        value=entry.pin_mode or "custom",
        content=ft.Column(
            spacing=6,
            tight=True,
            controls=[
                ft.Radio(
                    value="custom",
                    label=get_string("ext_pin_custom"),
                    fill_color=COLORS["accent"],
                ),
                ft.Radio(
                    value="force_pinned",
                    label=get_string("ext_pin_always"),
                    fill_color=COLORS["accent"],
                ),
                ft.Radio(
                    value="force_unpinned",
                    label=get_string("ext_pin_never"),
                    fill_color=COLORS["accent"],
                ),
            ],
        ),
    )

    apply_all_switch = ft.Switch(
        value=bool(entry.apply_to_all_chrome),
        label=get_string("ext_apply_all_chrome"),
        active_color=COLORS["accent"],
    )

    profiles = pm.list_profiles()
    profile_checks: dict[str, ft.Checkbox] = {}
    profile_rows: list[ft.Control] = []
    for p in profiles:
        checked = entry.id in (p.extension_ids or [])
        cb = ft.Checkbox(
            value=checked,
            label=f"{p.name}  ({p.engine.upper()})",
            fill_color=COLORS["accent"],
        )
        profile_checks[p.name] = cb
        profile_rows.append(cb)

    profiles_box = ft.Container(
        height=150 if profile_rows else 48,
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=8,
        bgcolor=COLORS["bg"],
        border=ft.Border.all(1, COLORS["card_border"]),
        content=ft.Column(
            spacing=2,
            scroll=ft.ScrollMode.AUTO,
            controls=profile_rows
            or [
                ft.Text(
                    get_string("no_profiles_yet"),
                    size=12,
                    color=COLORS["text_dim"],
                )
            ],
        ),
    )

    def _on_confirm(_: ft.ControlEvent) -> None:
        new_pin = pin_group.value or "custom"
        all_chrome = bool(apply_all_switch.value)
        extension_service.update_extension(
            entry.id,
            pin_mode=new_pin,
            apply_to_all_chrome=all_chrome,
        )
        selected_profiles = [name for name, cb in profile_checks.items() if cb.value]
        extension_service.assign_profiles(entry.id, selected_profiles, pm)
        msg = get_string("ext_saved_toast", name=entry.name)
        log(msg)
        page.pop_dialog()
        if on_saved:
            on_saved()

    body_container = ft.Container(
        width=460,
        height=390,
        content=ft.Column(
            spacing=10,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Divider(height=4, color=COLORS["card_border"]),
                ft.Text(
                    get_string("ext_pin_mode_title"),
                    size=14,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                pin_group,
                ft.Divider(height=10, color=COLORS["card_border"]),
                ft.Text(
                    get_string("ext_assign_profiles_title"),
                    size=14,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                apply_all_switch,
                ft.Text(
                    get_string("ext_assign_profiles_hint"),
                    size=11.5,
                    color=COLORS["text_sub"],
                ),
                profiles_box,
            ],
        ),
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=18, top=18, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=8, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=16),
        title=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Column(
                    spacing=1,
                    tight=True,
                    controls=[
                        ft.Text(
                            get_string("ext_settings_title"),
                            size=18,
                            font_family=FONT_BOLD,
                            color=COLORS["text_main"],
                        ),
                        ft.Text(
                            entry.name,
                            size=12,
                            color=COLORS["text_sub"],
                        ),
                    ],
                ),
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=18,
                    icon_color=COLORS["text_sub"],
                    style=ICON_STYLE,
                    on_click=lambda _: page.pop_dialog(),
                ),
            ],
        ),
        content=body_container,
        actions=[
            ft.OutlinedButton(
                get_string("cancel"),
                height=36,
                style=OUTLINE_STYLE,
                on_click=lambda _: page.pop_dialog(),
            ),
            ft.Button(
                get_string("ext_confirm_btn"),
                height=36,
                style=ACCENT_STYLE,
                on_click=_on_confirm,
            ),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=460,
        base_height=390,
        width_ratio=0.56,
        height_ratio=0.68,
        min_width=360,
        max_width=620,
        min_height=310,
        max_height=560,
    )
    page.show_dialog(dlg)


def open_extensions_dialog(
    page: ft.Page,
    extension_service: ExtensionService,
    pm: IProfileManager,
    file_picker: ft.FilePicker | None = None,
    log_fn: Callable[[str], None] | None = None,
    refresh_fn: Callable[[], None] | None = None,
) -> None:
    """Open the Chromium Extensions (.zip) Manager dialog (matching Ảnh 2)."""
    log = log_fn or (lambda _: None)
    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    subtitle_text = ft.Text("", size=13, color=COLORS["text_sub"])
    cards_grid = ft.Row(
        wrap=True,
        spacing=12,
        run_spacing=12,
        alignment=ft.MainAxisAlignment.START,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
    scroll_box = ft.Column(
        spacing=10,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        controls=[cards_grid],
    )

    def _icon_widget(entry: ExtensionEntry) -> ft.Container:
        ext_dir = extension_service.extension_dir(entry.id)
        icon_path = os.path.join(ext_dir, entry.icon_rel_path) if entry.icon_rel_path else ""
        if icon_path and os.path.isfile(icon_path):
            content: ft.Control = ft.Image(
                src=icon_path,
                width=34,
                height=34,
            )
        else:
            content = ft.Icon(
                ft.Icons.EXTENSION_ROUNDED,
                size=20,
                color=COLORS["accent"],
            )
        return ft.Container(
            width=40,
            height=40,
            border_radius=8,
            bgcolor=COLORS["card_bg"],
            border=ft.Border.all(1, COLORS["rule_strong"]),
            alignment=ft.Alignment(0, 0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=content,
        )

    def _toggle_enabled(ext_id: str, enabled: bool) -> None:
        extension_service.update_extension(ext_id, enabled=enabled)
        _rebuild_cards(notify=True)
        if refresh_fn:
            refresh_fn()

    def _delete_ext(entry: ExtensionEntry) -> None:
        extension_service.remove_extension(entry.id, pm=pm)
        msg = get_string("ext_deleted_toast", name=entry.name)
        feedback_text.value = msg
        feedback_text.color = COLORS["text_sub"]
        feedback_text.visible = True
        log(msg)
        _rebuild_cards(notify=True)
        if refresh_fn:
            refresh_fn()

    def _open_settings(entry: ExtensionEntry) -> None:
        def _after_settings() -> None:
            _rebuild_cards(notify=True)
            if refresh_fn:
                refresh_fn()

        open_extension_settings_dialog(
            page,
            entry,
            extension_service,
            pm,
            on_saved=_after_settings,
            log_fn=log_fn,
        )

    def _rebuild_cards(notify: bool = False) -> None:
        entries = extension_service.list_entries()
        usage = extension_service.usage_counts(pm)
        subtitle_text.value = get_string("ext_dialog_subtitle", count=len(entries))
        if not entries:
            cards_grid.controls = [
                ft.Container(
                    padding=32,
                    expand=True,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Column(
                        spacing=8,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(
                                ft.Icons.EXTENSION_OFF_OUTLINED,
                                size=32,
                                color=COLORS["text_dim"],
                            ),
                            ft.Text(
                                get_string("ext_empty_hint"),
                                size=13,
                                color=COLORS["text_dim"],
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                    ),
                )
            ]
        else:
            cards: list[ft.Control] = []
            for e in entries:
                used_cnt = usage.get(e.id, 0)
                pin_badge = (
                    "📌"
                    if e.pin_mode == "force_pinned"
                    else ("🚫" if e.pin_mode == "force_unpinned" else "")
                )
                cards.append(
                    ft.Container(
                        width=268,
                        padding=14,
                        border_radius=10,
                        bgcolor=COLORS["bg"],
                        border=ft.Border.all(1, COLORS["card_border"]),
                        content=ft.Column(
                            spacing=10,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                            controls=[
                                ft.Row(
                                    spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    controls=[
                                        _icon_widget(e),
                                        ft.Container(
                                            expand=True,
                                            content=ft.Text(
                                                e.name,
                                                size=14,
                                                font_family=FONT_BOLD,
                                                color=COLORS["text_main"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                                tooltip=f"{e.name} ({e.chrome_ext_id or e.id})",
                                            ),
                                        ),
                                        ft.Switch(
                                            value=e.enabled,
                                            active_color=COLORS["accent"],
                                            scale=0.82,
                                            on_change=lambda ev, eid=e.id: _toggle_enabled(
                                                eid, bool(ev.control.value)
                                            ),
                                        ),
                                    ],
                                ),
                                ft.Container(
                                    height=36,
                                    content=ft.Text(
                                        e.description or f"ID: {e.chrome_ext_id or e.id}",
                                        size=12,
                                        color=COLORS["text_sub"],
                                        max_lines=2,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        tooltip=e.description or e.name,
                                    ),
                                ),
                                ft.Row(
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    controls=[
                                        ft.Row(
                                            spacing=6,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                            controls=[
                                                ft.Text(
                                                    e.version,
                                                    size=12,
                                                    font_family=FONT_DATA,
                                                    color=COLORS["text_sub"],
                                                ),
                                                ft.Container(
                                                    padding=ft.Padding.symmetric(
                                                        horizontal=7, vertical=2
                                                    ),
                                                    border_radius=5,
                                                    bgcolor=COLORS["card_bg"],
                                                    border=ft.Border.all(1, COLORS["rule_strong"]),
                                                    content=ft.Row(
                                                        spacing=3,
                                                        tight=True,
                                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                                        controls=[
                                                            ft.Text(
                                                                get_string("ext_local_badge"),
                                                                size=11,
                                                                font_family=FONT_SEMIBOLD,
                                                                color=COLORS["text_sub"],
                                                            ),
                                                            ft.Icon(
                                                                ft.Icons.DESCRIPTION_OUTLINED,
                                                                size=12,
                                                                color=COLORS["text_sub"],
                                                            ),
                                                        ],
                                                    ),
                                                ),
                                                ft.Text(
                                                    f"{pin_badge} {used_cnt}p".strip(),
                                                    size=11,
                                                    font_family=FONT_DATA,
                                                    color=COLORS["text_dim"],
                                                ),
                                            ],
                                        ),
                                        ft.Row(
                                            spacing=4,
                                            controls=[
                                                ft.Container(
                                                    width=30,
                                                    height=30,
                                                    border_radius=6,
                                                    border=ft.Border.all(1, COLORS["card_border"]),
                                                    content=ft.IconButton(
                                                        icon=ft.Icons.DELETE_OUTLINE,
                                                        icon_size=15,
                                                        icon_color=COLORS["text_sub"],
                                                        tooltip=get_string("delete"),
                                                        style=DANGER_ICON_STYLE,
                                                        on_click=lambda _, ent=e: _delete_ext(ent),
                                                    ),
                                                ),
                                                ft.Container(
                                                    width=30,
                                                    height=30,
                                                    border_radius=6,
                                                    border=ft.Border.all(1, COLORS["card_border"]),
                                                    content=ft.IconButton(
                                                        icon=ft.Icons.MORE_HORIZ,
                                                        icon_size=16,
                                                        icon_color=COLORS["text_sub"],
                                                        tooltip=get_string("ext_settings_title"),
                                                        style=ICON_STYLE,
                                                        on_click=lambda _, ent=e: _open_settings(
                                                            ent
                                                        ),
                                                    ),
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    )
                )
            cards_grid.controls = cards

        if notify:
            with contextlib.suppress(Exception):
                page.update()

    async def _on_upload(_: ft.ControlEvent) -> None:
        picker = file_picker or next(
            (s for s in getattr(page, "services", None) or [] if isinstance(s, ft.FilePicker)),
            None,
        )
        if picker is None:
            return
        files = await picker.pick_files(
            allow_multiple=True,
            allowed_extensions=["zip"],
            dialog_title=get_string("ext_upload_btn"),
        )
        if not files:
            return
        for f in files:
            if not f.path:
                continue
            try:
                entry = extension_service.install_from_zip(f.path)
                msg = get_string("ext_installed_toast", name=entry.name, version=entry.version)
                feedback_text.value = msg
                feedback_text.color = COLORS["success"]
                feedback_text.visible = True
                log(msg)
            except Exception as exc:
                feedback_text.value = str(exc)
                feedback_text.color = COLORS["error"]
                feedback_text.visible = True
                log(f"[Extensions] Error: {exc}")
        _rebuild_cards(notify=True)
        if refresh_fn:
            refresh_fn()

    _rebuild_cards(notify=False)

    tab_bar = ft.Container(
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=20,
            controls=[
                ft.Container(
                    padding=ft.Padding.only(bottom=8),
                    border=ft.Border.only(bottom=ft.BorderSide(2, COLORS["text_main"])),
                    content=ft.Text(
                        get_string("ext_tab_mine"),
                        size=13.5,
                        font_family=FONT_BOLD,
                        color=COLORS["text_main"],
                    ),
                ),
            ],
        ),
    )

    upload_btn = ft.OutlinedButton(
        f"+ {get_string('ext_upload_btn')}",
        height=36,
        style=OUTLINE_STYLE,
        on_click=_on_upload,
    )

    body_container = ft.Container(
        width=880,
        height=540,
        content=ft.Column(
            spacing=12,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                tab_bar,
                ft.Row(
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[upload_btn, feedback_text],
                ),
                scroll_box,
            ],
        ),
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=16),
        title=ft.Column(
            spacing=3,
            tight=True,
            controls=[
                ft.Text(
                    get_string("ext_dialog_title"),
                    size=19,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                subtitle_text,
            ],
        ),
        content=body_container,
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=880,
        base_height=540,
        width_ratio=0.88,
        height_ratio=0.80,
        min_width=480,
        max_width=1080,
        min_height=360,
        max_height=780,
    )
    page.show_dialog(dlg)
