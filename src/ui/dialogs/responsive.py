"""Responsive dialog sizing and window-resize scaling for Flet AlertDialogs."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import flet as ft


def _read_page_dims(page: Any) -> tuple[int, int]:
    """Return (width, height) from `page` or `page.window` when positive, else (0, 0)."""
    w = getattr(page, "width", None) or getattr(getattr(page, "window", None), "width", None) or 0
    h = getattr(page, "height", None) or getattr(getattr(page, "window", None), "height", None) or 0
    try:
        return int(float(w)), int(float(h))
    except (TypeError, ValueError):
        return 0, 0


def bind_responsive_dialog(
    page: ft.Page,
    dlg: ft.AlertDialog,
    container: ft.Container,
    *,
    base_width: int,
    base_height: int | None = None,
    width_ratio: float = 0.88,
    height_ratio: float = 0.80,
    min_width: int = 340,
    max_width: int = 1080,
    min_height: int = 200,
    max_height: int = 820,
    on_resize_extra: Callable[[int, int], None] | None = None,
) -> Callable[[], None]:
    """Scale `container.width` / `container.height` to the window and live-update on resize."""
    state = {"active": True}
    prev_on_resize = getattr(page, "on_resize", None)
    prev_on_dismiss = getattr(dlg, "on_dismiss", None)

    def _apply_dims(notify: bool = False) -> None:
        if not state["active"]:
            return
        pw, ph = _read_page_dims(page)
        if pw >= 420:
            avail_w = max(min_width, pw - 72)
            target_w = max(min_width, min(max_width, int(pw * width_ratio), avail_w))
        else:
            target_w = base_width
        container.width = target_w

        target_h = base_height
        if base_height is not None:
            if ph >= 360:
                avail_h = max(min_height, ph - 148)
                target_h = max(min_height, min(max_height, int(ph * height_ratio), avail_h))
            else:
                target_h = base_height
            container.height = target_h

        if on_resize_extra is not None:
            with contextlib.suppress(Exception):
                on_resize_extra(target_w, target_h or 0)

        if notify:
            with contextlib.suppress(Exception):
                page.update()

    def _on_page_resize(e: Any = None) -> None:
        if callable(prev_on_resize):
            with contextlib.suppress(Exception):
                prev_on_resize(e)
        _apply_dims(notify=True)

    def _cleanup(e: Any = None) -> None:
        if not state["active"]:
            return
        state["active"] = False
        with contextlib.suppress(Exception):
            if getattr(page, "on_resize", None) is _on_page_resize:
                page.on_resize = prev_on_resize
        if callable(prev_on_dismiss):
            with contextlib.suppress(Exception):
                prev_on_dismiss(e)

    _apply_dims(notify=False)
    dlg.clip_behavior = ft.ClipBehavior.HARD_EDGE
    container.clip_behavior = ft.ClipBehavior.HARD_EDGE
    dlg.on_dismiss = _cleanup
    with contextlib.suppress(Exception):
        page.on_resize = _on_page_resize

    return _cleanup
