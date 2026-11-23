from collections.abc import Callable
from inspect import isawaitable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from ..theme.styles import ERROR_STYLE


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
    previous_keyboard_handler = page.on_keyboard_event

    def _restore_keyboard_handler(_: ft.ControlEvent | None = None) -> None:
        if page.on_keyboard_event is _on_key_down:
            page.on_keyboard_event = previous_keyboard_handler
            page.update()

    def _on_cancel(_: ft.ControlEvent) -> None:
        if dlg.open:
            page.pop_dialog()
            _restore_keyboard_handler()

    async def _on_key_down(event: ft.KeyboardEvent) -> None:
        if dlg.open and event.key == "Escape":
            _on_cancel(event)
        elif previous_keyboard_handler is not None:
            result = previous_keyboard_handler(event)
            if isawaitable(result):
                await result

    def _on_confirm(event: ft.ControlEvent) -> None:
        if dlg.open:
            on_confirm()
            _on_cancel(event)

    dlg = ft.AlertDialog(
        modal=True,
        on_dismiss=_restore_keyboard_handler,
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
    page.show_dialog(dlg)
    page.on_keyboard_event = _on_key_down
    page.update()
