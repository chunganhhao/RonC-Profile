from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS, FONT_SEMIBOLD, current_theme_mode

_WIDTH, _HEIGHT = 96, 34


def build_launch_button(
    name: str,
    is_loading: bool,
    is_running: bool,
    on_launch: Callable,
) -> ft.Button:
    """Create the PionLogin-style Launch ('🌐 Mở') / Stop ('✕ Đóng') / Loading button."""
    is_dark = current_theme_mode() == "dark"
    shape = ft.RoundedRectangleBorder(radius=7)
    label_style = ft.TextStyle(font_family=FONT_SEMIBOLD, size=13)

    if is_loading:
        return ft.Button(
            content=ft.ProgressRing(width=15, height=15, stroke_width=2, color=COLORS["text_sub"]),
            width=_WIDTH,
            height=_HEIGHT,
            disabled=True,
            tooltip=get_string("loading"),
            style=ft.ButtonStyle(
                shape=shape,
                bgcolor=COLORS["paper_sunk"],
            ),
        )
    if is_running:
        # In PionLogin dark mode, running stop button is crisp white with dark ink
        stop_bg = "#F9FAFB" if is_dark else COLORS["ink_button"]
        stop_fg = "#0B0C0E" if is_dark else "#FFFFFF"
        return ft.Button(
            get_string("stop"),
            icon=ft.Icons.CLOSE_ROUNDED,
            width=_WIDTH,
            height=_HEIGHT,
            style=ft.ButtonStyle(
                shape=shape,
                bgcolor={
                    ft.ControlState.DEFAULT: stop_bg,
                    ft.ControlState.HOVERED: "#E5E7EB" if is_dark else "#000000",
                },
                color=stop_fg,
                text_style=label_style,
                padding=ft.Padding.symmetric(horizontal=10),
                elevation=0,
            ),
            on_click=lambda _, n=name: on_launch(n),
        )

    launch_bg = COLORS["paper_sunk"] if is_dark else "#FFFFFF"
    launch_hover = COLORS["rule_strong"] if is_dark else COLORS["paper_sunk"]
    launch_side = ft.BorderSide(1, COLORS["rule_strong"])
    return ft.Button(
        get_string("launch"),
        icon=ft.Icons.LANGUAGE,
        width=_WIDTH,
        height=_HEIGHT,
        style=ft.ButtonStyle(
            shape=shape,
            side=launch_side,
            bgcolor={
                ft.ControlState.DEFAULT: launch_bg,
                ft.ControlState.HOVERED: launch_hover,
            },
            color=COLORS["text_main"],
            text_style=label_style,
            padding=ft.Padding.symmetric(horizontal=10),
            elevation=0,
        ),
        on_click=lambda _, n=name: on_launch(n),
    )
