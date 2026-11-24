from typing import Any

import flet as ft

from .colors import COLORS, FONT_MEDIUM, FONT_SEMIBOLD

_SHAPE = ft.RoundedRectangleBorder(radius=8)
_LABEL = ft.TextStyle(font_family=FONT_SEMIBOLD, size=13.5)
_PAD = ft.Padding.symmetric(horizontal=12)

ACCENT_STYLE = ft.ButtonStyle(
    shape=_SHAPE,
    bgcolor={
        ft.ControlState.DISABLED: COLORS["rule_strong"],
        ft.ControlState.DEFAULT: COLORS["accent"],
        ft.ControlState.HOVERED: COLORS["accent_hover"],
    },
    color={ft.ControlState.DISABLED: COLORS["text_dim"], ft.ControlState.DEFAULT: "#FFFFFF"},
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
    color={
        ft.ControlState.DISABLED: COLORS["text_dim"],
        ft.ControlState.DEFAULT: COLORS["text_main"],
    },
    bgcolor={
        ft.ControlState.DISABLED: COLORS["paper_sunk"],
        ft.ControlState.DEFAULT: COLORS["card_bg"],
    },
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
        ft.ControlState.DISABLED: COLORS["rule_strong"],
        ft.ControlState.DEFAULT: COLORS["accent"],
        ft.ControlState.HOVERED: COLORS["accent_hover"],
    }
    ACCENT_STYLE.color = {
        ft.ControlState.DISABLED: COLORS["text_dim"],
        ft.ControlState.DEFAULT: "#FFFFFF",
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
    OUTLINE_STYLE.color = {
        ft.ControlState.DISABLED: COLORS["text_dim"],
        ft.ControlState.DEFAULT: COLORS["text_main"],
    }
    OUTLINE_STYLE.bgcolor = {
        ft.ControlState.DISABLED: COLORS["paper_sunk"],
        ft.ControlState.DEFAULT: COLORS["card_bg"],
    }

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


# shortcut: Flet 1.1.2 IconButton/Button before_update() always allocates a new ButtonStyle via .copy(), causing ObjectPatch to emit 3,000 phantom style ops per update; remove once upstream Flet compares style inputs before copying
# Keep class names as IconButton / Button so existing UI introspection tests (`__class__.__name__`) keep matching.
class IconButton(ft.IconButton):
    """IconButton that preserves `_internals['style']` identity unless style inputs change."""

    def before_update(self) -> None:
        super(ft.IconButton, self).before_update()
        if (
            self.style is not None
            or self.bgcolor is not None
            or self.visual_density is not None
            or self.mouse_cursor is not None
        ):
            key = (
                self.style.copy() if self.style is not None else None,
                self.bgcolor,
                self.visual_density,
                self.mouse_cursor,
            )
            if getattr(self, "_cached_style_key", None) != key:
                self._cached_style_key = key
                self._internals["style"] = (self.style or ft.ButtonStyle()).copy(
                    bgcolor=self.bgcolor,
                    visual_density=self.visual_density,
                    mouse_cursor=self.mouse_cursor,
                )
        else:
            self._cached_style_key = None
            self._internals.pop("style", None)


class Button(ft.Button):
    """Button that preserves `_internals['style']` identity unless style inputs change."""

    def before_update(self) -> None:
        super(ft.Button, self).before_update()
        if (
            self.style is not None
            or self.color is not None
            or self.bgcolor is not None
            or self.elevation != 1
        ):
            key = (
                self.style.copy() if self.style is not None else None,
                self.color,
                self.bgcolor,
                self.elevation,
            )
            if getattr(self, "_cached_style_key", None) != key:
                self._cached_style_key = key
                self._internals["style"] = (self.style or ft.ButtonStyle()).copy(
                    color=self.color,
                    bgcolor=self.bgcolor,
                    elevation=self.elevation,
                )
        else:
            self._cached_style_key = None
            self._internals.pop("style", None)


StableIconButton = IconButton
StableButton = Button
