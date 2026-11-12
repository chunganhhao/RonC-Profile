from typing import Any

import flet as ft

from .colors import COLORS, FONT_MEDIUM, FONT_SEMIBOLD

_SHAPE = ft.RoundedRectangleBorder(radius=8)
_LABEL = ft.TextStyle(font_family=FONT_SEMIBOLD, size=13.5)
_PAD = ft.Padding.symmetric(horizontal=12)

ACCENT_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    bgcolor={
        ft.ControlState.DEFAULT: COLORS["accent"],
        ft.ControlState.HOVERED: COLORS["accent_hover"],
    },
    color="#FFFFFF",
    text_style=_LABEL,
    padding=_PAD,
    elevation=0,
)

# Stop: ink, not red; stopping is routine, not destructive.
INK_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    bgcolor={
        ft.ControlState.DEFAULT: COLORS["ink_button"],
        ft.ControlState.HOVERED: COLORS["rule_strong"],
    },
    color="#FFFFFF",
    text_style=_LABEL,
    padding=_PAD,
    elevation=0,
)

ERROR_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    bgcolor={
        ft.ControlState.DEFAULT: COLORS["error"],
        ft.ControlState.HOVERED: COLORS["delete_hover"],
    },
    color="#FFFFFF",
    text_style=_LABEL,
    elevation=0,
)

OUTLINE_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    side=ft.BorderSide(1, COLORS["rule_strong"]),
    color=COLORS["text_main"],
    bgcolor=COLORS["card_bg"],
    text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=13),
)

# Outline buttons sitting on the sidebar rail.
RAIL_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    side=ft.BorderSide(1, COLORS["cover_line"]),
    color=COLORS["cover_ink"],
    overlay_color=COLORS["cover_deep"],
    text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=13),
    padding=ft.Padding.symmetric(horizontal=10),
)

ICON_STYLE = ft.ButtonStyle(shape=_SHAPE, overlay_color=COLORS["paper_sunk"])
DANGER_ICON_STYLE = ft.ButtonStyle(shape=_SHAPE, overlay_color=COLORS["warn_tint"])

DLG_FIELD_KWARGS: dict[str, Any] = dict(
    border_radius=8,
    bgcolor=COLORS["input_bg"],
    color=COLORS["text_main"],
    border_color=COLORS["rule_strong"],
    focused_border_color=COLORS["accent"],
    label_style=ft.TextStyle(color=COLORS["text_sub"]),
    hint_style=ft.TextStyle(color=COLORS["text_dim"]),
    cursor_color=COLORS["accent"],
)


def refresh_styles() -> None:
    """Synchronize mutable style objects and DLG_FIELD_KWARGS after a theme change."""
    ACCENT_STYLE.bgcolor = {
        ft.ControlState.DEFAULT: COLORS["accent"],
        ft.ControlState.HOVERED: COLORS["accent_hover"],
    }
    INK_STYLE.bgcolor = {
        ft.ControlState.DEFAULT: COLORS["ink_button"],
        ft.ControlState.HOVERED: COLORS["rule_strong"],
    }
    ERROR_STYLE.bgcolor = {
        ft.ControlState.DEFAULT: COLORS["error"],
        ft.ControlState.HOVERED: COLORS["delete_hover"],
    }
    OUTLINE_STYLE.side = ft.BorderSide(1, COLORS["rule_strong"])
    OUTLINE_STYLE.color = COLORS["text_main"]
    OUTLINE_STYLE.bgcolor = COLORS["card_bg"]

    RAIL_STYLE.side = ft.BorderSide(1, COLORS["cover_line"])
    RAIL_STYLE.color = COLORS["cover_ink"]
    RAIL_STYLE.overlay_color = COLORS["cover_deep"]

    ICON_STYLE.overlay_color = COLORS["paper_sunk"]
    DANGER_ICON_STYLE.overlay_color = COLORS["warn_tint"]

    DLG_FIELD_KWARGS.update(
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["accent"],
        label_style=ft.TextStyle(color=COLORS["text_sub"]),
        hint_style=ft.TextStyle(color=COLORS["text_dim"]),
        cursor_color=COLORS["accent"],
    )
