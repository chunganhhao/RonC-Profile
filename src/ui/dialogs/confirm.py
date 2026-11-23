from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from ..theme.styles import ERROR_STYLE
from .modal import show_modal_dialog


def open_confirm_dialog(
    page: ft.Page,
    profile_name: str,
    on_confirm: Callable[[], None],
    *,
    title: str | None = None,
    body: str | None = None,
    action_label: str | None = None,
    action_icon: ft.IconData = ft.Icons.DELETE,
) -> None:
    def _on_cancel(_: ft.ControlEvent) -> None:
        close()

    def _on_confirm(event: ft.ControlEvent) -> None:
        if dlg.open:
            try:
                on_confirm()
            finally:
                _on_cancel(event)

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            title if title is not None else get_string("confirm_delete_msg", name=profile_name),
            size=18,
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Text(
            body if body is not None else get_string("cannot_undo"),
            size=13,
            color=COLORS["text_dim"],
        ),
        actions=[
            ft.TextButton(get_string("cancel"), on_click=_on_cancel),
            ft.Button(
                action_label if action_label is not None else get_string("delete"),
                icon=action_icon,
                style=ERROR_STYLE,
                on_click=_on_confirm,
            ),
        ],
    )
    close = show_modal_dialog(page, dlg)
