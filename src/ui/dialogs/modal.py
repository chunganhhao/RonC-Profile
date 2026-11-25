from collections.abc import Callable
from threading import Lock

import flet as ft

_editor_lock = Lock()


def show_modal_dialog(
    page: ft.Page,
    dialog: ft.AlertDialog,
    *,
    local_keyboard: bool = False,
    escape_on_release: bool = False,
    can_close: Callable[[], bool] | None = None,
) -> Callable[[ft.ControlEvent | None], bool]:
    page_state = vars(page)
    keyboard_links = page_state.setdefault("_ronc_modal_keyboard_links", {})
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
            if keyboard_handler is not None:
                previous_keyboard = keyboard_links.pop(keyboard_handler)
                for handler, previous in keyboard_links.items():
                    if previous is keyboard_handler:
                        keyboard_links[handler] = previous_keyboard
                if page.on_keyboard_event is keyboard_handler:
                    page.on_keyboard_event = previous_keyboard
                    page.update()

    def is_topmost() -> bool:
        parent = dialog.parent
        if not dialog.open or parent is None:
            return False
        top = next(
            (
                item
                for item in reversed(parent.controls)
                if isinstance(item, ft.DialogControl) and item.open
            ),
            None,
        )
        return top is dialog

    def close(event: ft.ControlEvent | None = None) -> bool:
        detached = dialog.parent is None
        if (not detached and not is_topmost()) or (can_close is not None and not can_close()):
            return False
        if detached:
            dialog.open = False
            dismiss(event)
            return True
        try:
            page.pop_dialog()
        finally:
            dismiss(event)
        return True

    def on_key(event: ft.KeyboardEvent | ft.KeyDownEvent) -> None:
        if event.key == "Escape" and not dismissed:
            close()

    keyboard_handler = on_key
    if escape_on_release:
        keyboard_handler = None
        escape_pressed = False

        def on_key_down(event: ft.KeyDownEvent) -> None:
            nonlocal escape_pressed
            if event.key == "Escape":
                escape_pressed = not dismissed and is_topmost()

        def on_key_up(event: ft.KeyUpEvent) -> None:
            nonlocal escape_pressed
            if event.key == "Escape" and escape_pressed:
                escape_pressed = False
                if not dismissed:
                    close()

        def listen(content: ft.Control, *, autofocus: bool = False) -> ft.KeyboardListener:
            return ft.KeyboardListener(
                content=content, autofocus=autofocus, on_key_down=on_key_down, on_key_up=on_key_up
            )

        dialog.content = listen(dialog.content, autofocus=True)
        if dialog.title is not None:
            dialog.title = listen(dialog.title)
        dialog.actions = [listen(action) for action in dialog.actions]
    elif local_keyboard:
        body = ft.KeyboardListener(content=dialog.content, autofocus=True, on_key_down=on_key)
        dialog.content = body
        dialog.actions = [
            ft.KeyboardListener(content=action, on_key_down=on_key) for action in dialog.actions
        ]

        async def restore_focus(event: ft.KeyboardEvent) -> None:
            # Flutter may move focus off the dialog's controls when closing a menu.
            if event.key == "Escape":
                if dialog.parent is None:
                    close()
                elif is_topmost():
                    await body.focus()
            ft.context.disable_auto_update()

        keyboard_handler = restore_focus

    dialog.on_dismiss = dismiss
    if keyboard_handler is not None:
        keyboard_links[keyboard_handler] = getattr(page, "on_keyboard_event", None)
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
) -> Callable[[ft.ControlEvent | None], bool] | None:
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
