from collections.abc import Callable
from threading import Lock

import flet as ft

_editor_lock = Lock()


def show_modal_dialog(
    page: ft.Page,
    dialog: ft.AlertDialog,
    *,
    local_keyboard: bool = False,
    can_close: Callable[[], bool] | None = None,
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
            if page.on_keyboard_event is keyboard_handler:
                page.on_keyboard_event = previous_keyboard
                page.update()

    def is_topmost() -> bool:
        if not dialog.open:
            return False
        parent = dialog.parent
        if parent is None:
            return True
        top = next(
            (item for item in reversed(parent.controls) if getattr(item, "open", False)), None
        )
        return top is dialog

    def close(event: ft.ControlEvent | None = None) -> None:
        if not is_topmost() or (can_close is not None and not can_close()):
            return
        try:
            page.pop_dialog()
        finally:
            dismiss(event)

    def on_key(event: ft.KeyboardEvent | ft.KeyDownEvent) -> None:
        if event.key == "Escape":
            close()

    keyboard_handler = on_key
    if local_keyboard:
        body = ft.KeyboardListener(content=dialog.content, autofocus=True, on_key_down=on_key)
        dialog.content = body
        dialog.actions = [
            ft.KeyboardListener(content=action, on_key_down=on_key) for action in dialog.actions
        ]

        async def restore_focus(event: ft.KeyboardEvent) -> None:
            # Flutter may move focus off the dialog's controls when closing a menu.
            if event.key == "Escape" and is_topmost():
                await body.focus()
            ft.context.disable_auto_update()

        keyboard_handler = restore_focus

    dialog.on_dismiss = dismiss
    page.on_keyboard_event = keyboard_handler
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
    *,
    local_keyboard: bool = False,
) -> Callable[[ft.ControlEvent | None], None] | None:
    with _editor_lock:
        active = getattr(page, "_ronc_profile_editor", None)
        if isinstance(active, ft.AlertDialog) and active.open:
            return None
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
        close = show_modal_dialog(page, dialog, local_keyboard=local_keyboard)
        page._ronc_profile_editor = dialog
        return close
