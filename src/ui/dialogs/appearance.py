import contextlib
from collections.abc import Callable

import flet as ft

from ...core.strings import current_language, get_string
from ..theme.colors import (
    ACCENT_PRESETS,
    COLORS,
    FONT_MEDIUM,
    FONT_SEMIBOLD,
    current_accent_color,
    current_theme_mode,
)


def _flag_badge(code: str) -> ft.Container:
    if code == "vi":
        return ft.Container(
            width=26,
            height=18,
            border_radius=4,
            bgcolor="#DA251D",
            alignment=ft.Alignment(0, 0),
            content=ft.Text(
                "★",
                size=10,
                color="#FFFF00",
                font_family=FONT_SEMIBOLD,
                weight=ft.FontWeight.W_700,
            ),
        )
    return ft.Container(
        width=26,
        height=18,
        border_radius=4,
        bgcolor="#1D4ED8",
        alignment=ft.Alignment(0, 0),
        content=ft.Text(
            "US",
            size=9,
            color="#FFFFFF",
            font_family=FONT_SEMIBOLD,
            weight=ft.FontWeight.W_700,
        ),
    )


def _theme_preview_card(
    mode: str,
    *,
    active: bool,
    on_select: Callable[[str], None],
) -> ft.Container:
    is_light = mode == "light"
    outer_bg = "#FFFFFF" if is_light else "#14161A"
    inner_bg = "#F8FAFC" if is_light else "#1E222A"
    inner_border = "#CBD5E1" if is_light else "#323846"
    aa_color = "#1E293B" if is_light else "#F3F4F6"
    outer_border_color = COLORS["accent"] if active else ("#CBD5E1" if is_light else "#2A2E37")
    dot_bg = "#2563EB" if active else ("#0F172A" if is_light else "#0B0C0E")
    label = get_string("theme_light") if is_light else get_string("theme_dark")

    preview_box = ft.Container(
        width=142,
        height=90,
        border_radius=12,
        bgcolor=outer_bg,
        border=ft.Border.all(1.6 if active else 1, outer_border_color),
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        padding=ft.Padding.only(left=24, top=20),
        content=ft.Container(
            expand=True,
            bgcolor=inner_bg,
            border=ft.Border(
                top=ft.BorderSide(1, inner_border),
                left=ft.BorderSide(1, inner_border),
            ),
            border_radius=ft.BorderRadius.only(top_left=10),
            padding=ft.Padding.only(left=12, top=8, right=8, bottom=8),
            content=ft.Column(
                spacing=0,
                expand=True,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(
                        "Aa",
                        size=19,
                        font_family=FONT_SEMIBOLD,
                        weight=ft.FontWeight.W_700,
                        color=aa_color,
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[
                            ft.Container(
                                width=20,
                                height=20,
                                border_radius=999,
                                bgcolor=dot_bg,
                                alignment=ft.Alignment(0, 0),
                                content=ft.Icon(
                                    ft.Icons.CHECK_ROUNDED,
                                    size=13,
                                    color="#FFFFFF",
                                )
                                if active
                                else None,
                            ),
                        ],
                    ),
                ],
            ),
        ),
    )

    return ft.Container(
        key=f"appearance_theme_{mode}",
        border_radius=12,
        ink=True,
        on_click=lambda _: on_select(mode),
        content=ft.Column(
            tight=True,
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                preview_box,
                ft.Text(
                    label,
                    size=13,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_main"],
                ),
            ],
        ),
    )


def open_appearance_dialog(
    page: ft.Page,
    *,
    on_language: Callable[[str], None],
    on_theme: Callable[[str], None],
    on_accent: Callable[[str], None],
) -> ft.AlertDialog:
    lang_menu_open = False
    body_host = ft.Container()

    dlg = ft.AlertDialog(
        modal=False,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(
            radius=16,
            side=ft.BorderSide(1, COLORS["card_border"]),
        ),
        content_padding=ft.Padding.symmetric(horizontal=22, vertical=20),
        content=body_host,
    )

    def _sync_dialog() -> None:
        dlg.bgcolor = COLORS["card_bg"]
        dlg.shape = ft.RoundedRectangleBorder(
            radius=16,
            side=ft.BorderSide(1, COLORS["card_border"]),
        )
        body_host.content = _build_body()
        with contextlib.suppress(Exception):
            page.update()

    def _toggle_lang_menu(_: object = None) -> None:
        nonlocal lang_menu_open
        lang_menu_open = not lang_menu_open
        _sync_dialog()

    def _pick_language(code: str) -> None:
        nonlocal lang_menu_open
        lang_menu_open = False
        on_language(code)
        _sync_dialog()

    def _pick_theme(mode: str) -> None:
        on_theme(mode)
        _sync_dialog()

    def _pick_accent(preset: str) -> None:
        on_accent(preset)
        _sync_dialog()

    def _lang_option_row(code: str, label_key: str) -> ft.Container:
        active = current_language() == code
        return ft.Container(
            key=f"appearance_lang_{code}",
            height=40,
            padding=ft.Padding.symmetric(horizontal=12),
            border_radius=8,
            bgcolor=COLORS["accent_soft"] if active else ft.Colors.TRANSPARENT,
            ink=True,
            on_click=lambda _: _pick_language(code),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    _flag_badge(code),
                    ft.Text(
                        get_string(label_key),
                        size=13.5,
                        font_family=FONT_SEMIBOLD if active else FONT_MEDIUM,
                        color=COLORS["text_main"],
                        expand=True,
                    ),
                    ft.Icon(
                        ft.Icons.CHECK_ROUNDED,
                        size=16,
                        color=COLORS["accent"],
                    )
                    if active
                    else ft.Container(width=16),
                ],
            ),
        )

    def _build_body() -> ft.Column:
        lang = current_language()
        theme = current_theme_mode()
        accent = current_accent_color()
        lang_label = get_string("lang_vi_label") if lang == "vi" else get_string("lang_en_label")

        lang_controls: list[ft.Control] = [
            ft.Text(
                get_string("prefs_lang_title"),
                size=17,
                font_family=FONT_SEMIBOLD,
                weight=ft.FontWeight.W_700,
                color=COLORS["text_main"],
            ),
            ft.Text(
                get_string("prefs_lang_desc"),
                size=12.5,
                font_family=FONT_MEDIUM,
                color=COLORS["text_sub"],
            ),
            ft.Container(
                key="appearance_lang_select",
                height=44,
                padding=ft.Padding.symmetric(horizontal=12),
                border_radius=10,
                bgcolor=COLORS["input_bg"],
                border=ft.Border.all(
                    1.2,
                    COLORS["accent"] if lang_menu_open else COLORS["rule_strong"],
                ),
                ink=True,
                on_click=_toggle_lang_menu,
                content=ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        _flag_badge(lang),
                        ft.Text(
                            lang_label,
                            size=14,
                            font_family=FONT_SEMIBOLD,
                            color=COLORS["text_main"],
                            expand=True,
                        ),
                        ft.Icon(
                            ft.Icons.UNFOLD_MORE_ROUNDED,
                            size=18,
                            color=COLORS["text_sub"],
                        ),
                    ],
                ),
            ),
        ]
        if lang_menu_open:
            lang_controls.append(
                ft.Container(
                    padding=4,
                    border_radius=10,
                    bgcolor=COLORS["input_bg"],
                    border=ft.Border.all(1, COLORS["rule_strong"]),
                    content=ft.Column(
                        tight=True,
                        spacing=2,
                        controls=[
                            _lang_option_row("vi", "lang_vi_label"),
                            _lang_option_row("en", "lang_en_label"),
                        ],
                    ),
                )
            )

        swatches: list[ft.Control] = []
        for preset, meta in ACCENT_PRESETS.items():
            is_active = accent == preset
            swatches.append(
                ft.Container(
                    key=f"appearance_color_{preset}",
                    width=54,
                    height=48,
                    padding=5,
                    border_radius=10,
                    bgcolor=COLORS["card_bg"],
                    border=ft.Border.all(
                        2.0 if is_active else 1.2,
                        COLORS["text_main"] if is_active else COLORS["rule_strong"],
                    ),
                    ink=True,
                    tooltip=get_string(f"color_{preset}"),
                    on_click=lambda _, p=preset: _pick_accent(p),
                    content=ft.Container(
                        expand=True,
                        border_radius=6,
                        bgcolor=str(meta["swatch"]),
                    ),
                )
            )

        return ft.Column(
            width=306,
            tight=True,
            spacing=18,
            controls=[
                ft.Column(
                    tight=True,
                    spacing=8,
                    controls=lang_controls,
                ),
                ft.Column(
                    tight=True,
                    spacing=10,
                    controls=[
                        ft.Text(
                            get_string("prefs_theme_title"),
                            size=17,
                            font_family=FONT_SEMIBOLD,
                            weight=ft.FontWeight.W_700,
                            color=COLORS["text_main"],
                        ),
                        ft.Text(
                            get_string("prefs_theme_desc"),
                            size=12.5,
                            font_family=FONT_MEDIUM,
                            color=COLORS["text_sub"],
                        ),
                        ft.Row(
                            spacing=14,
                            controls=[
                                _theme_preview_card(
                                    "light",
                                    active=theme == "light",
                                    on_select=_pick_theme,
                                ),
                                _theme_preview_card(
                                    "dark",
                                    active=theme == "dark",
                                    on_select=_pick_theme,
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Column(
                    tight=True,
                    spacing=10,
                    controls=[
                        ft.Text(
                            get_string("prefs_color_title"),
                            size=17,
                            font_family=FONT_SEMIBOLD,
                            weight=ft.FontWeight.W_700,
                            color=COLORS["text_main"],
                        ),
                        ft.Text(
                            get_string("prefs_color_desc"),
                            size=12.5,
                            font_family=FONT_MEDIUM,
                            color=COLORS["text_sub"],
                        ),
                        ft.Row(
                            spacing=12,
                            controls=swatches,
                        ),
                    ],
                ),
            ],
        )

    body_host.content = _build_body()
    page.show_dialog(dlg)
    return dlg
