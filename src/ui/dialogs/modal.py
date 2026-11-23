from collections.abc import Callable
from threading import Lock

import flet as ft

_editor_lock = Lock()


def show_modal_dialog(
    page: ft.Page, dialog: ft.AlertDialog
) -> Callable[[ft.ControlEvent | None], None]:
    previous_keyboard = getattr(page, "on_keyboard_event", None)
    previous_dismiss = dialog.on_dismiss
    dismissed = False

    def dismiss(event: ft.ControlEvent | None = None) -> None:
        nonlocal dismissed
        if dismissed:
            return
        dismissed = True
        try:
            if previous_dismiss is not None:
                previous_dismiss(event)
        finally:
            if page.on_keyboard_event is on_key:
                page.on_keyboard_event = previous_keyboard
                page.update()

    def close(event: ft.ControlEvent | None = None) -> None:
        if not dialog.open:
            return
        parent = dialog.parent
        if parent is not None:
            top = next((item for item in reversed(parent.controls) if item.open), None)
            if top is not dialog:
                return
        try:
            page.pop_dialog()
        finally:
            dismiss(event)

    def on_key(event: ft.KeyboardEvent) -> None:
        if event.key == "Escape":
            close()

    dialog.on_dismiss = dismiss
    page.on_keyboard_event = on_key
    try:
        page.show_dialog(dialog)
        page.update()
    except Exception:
        dismiss()
        raise
    return close


def show_profile_editor(
    page: ft.Page,
    dialog: ft.AlertDialog,
    prepare: Callable[[], None],
) -> bool:
    with _editor_lock:
        active = getattr(page, "_ronc_profile_editor", None)
        if isinstance(active, ft.AlertDialog) and active.open:
            return False
        prepare()
        previous_dismiss = dialog.on_dismiss

        def dismiss(event: ft.ControlEvent | None) -> None:
            try:
                if previous_dismiss is not None:
                    previous_dismiss(event)
            finally:
                if getattr(page, "_ronc_profile_editor", None) is dialog:
                    page._ronc_profile_editor = None

        dialog.on_dismiss = dismiss
        page._ronc_profile_editor = dialog
        show_modal_dialog(page, dialog)
        return True
