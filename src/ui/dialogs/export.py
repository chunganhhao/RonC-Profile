import inspect
from collections.abc import Awaitable, Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import Profile
from ..theme.colors import COLORS
from ..theme.styles import ACCENT_STYLE
from .modal import show_modal_dialog
from .responsive import bind_responsive_dialog


def open_export_dialog(
    page: ft.Page,
    file_picker: ft.FilePicker,
    profiles: list[Profile],
    on_complete: Callable[[list[str], str, bool], Awaitable[None] | None],
) -> None:
    if not profiles:
        return

    names = [p.name for p in profiles]
    checkboxes = [ft.Checkbox(label=n, value=False, shape=ft.CircleBorder()) for n in names]
    include_data_cb = ft.Checkbox(label=get_string("include_browser_data"), value=True)
    dismissed = False

    def _on_dismiss(_: ft.ControlEvent | None = None) -> None:
        nonlocal dismissed
        dismissed = True

    def _on_cancel(_: ft.ControlEvent | None = None) -> None:
        close()

    async def on_export(e: ft.ControlEvent | None = None) -> None:
        btn = e.control if e is not None else export_btn
        if btn.disabled:
            return
        selected = [cb.label for cb in checkboxes if cb.value]
        if not selected:
            return
        include_data = include_data_cb.value
        btn.disabled = True
        cancel_btn.disabled = True
        page.update()
        dir_path: str | None = None
        try:
            dir_path = await file_picker.get_directory_path(
                dialog_title=get_string("select_export_dir"),
            )
            if dir_path:
                res = on_complete(selected, dir_path, include_data)
                if inspect.isawaitable(res):
                    await res
        finally:
            btn.disabled = False
            cancel_btn.disabled = False
            page.update()
        if dir_path and not dismissed:
            close()

    def sync_select_all() -> None:
        select_all_cb.value = all(cb.value for cb in checkboxes)
        select_all_cb.tristate = any(cb.value for cb in checkboxes) and not all(
            cb.value for cb in checkboxes
        )

    def on_profile_change(_: ft.ControlEvent) -> None:
        sync_select_all()
        page.update()

    for cb in checkboxes:
        cb.on_change = on_profile_change

    def toggle_all(e: ft.ControlEvent) -> None:
        target = e.control.value
        if target is None:
            target = True
        for cb in checkboxes:
            cb.value = target
        select_all_cb.value = target
        select_all_cb.tristate = False
        page.update()

    select_all_cb = ft.Checkbox(
        label=get_string("select_all"),
        value=False,
        tristate=False,
        shape=ft.CircleBorder(),
        on_change=toggle_all,
    )

    body_container = ft.Container(
        width=420,
        content=ft.Column(
            tight=True,
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(
                    get_string("choose_profiles_to_export"),
                    size=12,
                    color=COLORS["text_dim"],
                ),
                ft.Container(height=4),
                select_all_cb,
                ft.Divider(height=1),
                *checkboxes,
                ft.Container(height=8),
                include_data_cb,
                ft.Container(
                    key="export_credentials_warning",
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                    border_radius=8,
                    bgcolor=COLORS["warn_tint"],
                    content=ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Icon(
                                ft.Icons.WARNING_AMBER_ROUNDED,
                                size=15,
                                color=COLORS["warning"],
                            ),
                            ft.Text(
                                get_string("export_credentials_warning"),
                                size=11.5,
                                color=COLORS["warn_text"],
                                expand=True,
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )

    cancel_btn = ft.TextButton(get_string("cancel"), on_click=_on_cancel)
    export_btn = ft.Button(
        get_string("export"),
        icon=ft.Icons.UPLOAD,
        style=ACCENT_STYLE,
        on_click=on_export,
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Text(get_string("export_profiles"), size=20, weight=ft.FontWeight.BOLD),
        content=body_container,
        on_dismiss=_on_dismiss,
        actions=[cancel_btn, export_btn],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=420,
        width_ratio=0.52,
        min_width=340,
        max_width=560,
    )
    close = show_modal_dialog(page, dlg, can_close=lambda: not cancel_btn.disabled)
