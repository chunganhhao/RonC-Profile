import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS
from .responsive import bind_responsive_dialog


def open_log_dialog(page: ft.Page, log_lines: list[str]) -> None:
    content = "\n".join(log_lines) if log_lines else get_string("log_no_activity")

    body_container = ft.Container(
        width=820,
        height=440,
        border_radius=10,
        bgcolor=COLORS["log_bg"],
        padding=14,
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(
                    get_string("log_entries", count=len(log_lines)),
                    size=12,
                    color=COLORS["text_dim"],
                ),
                ft.Container(height=8),
                ft.Text(
                    content,
                    size=12,
                    color=COLORS["text_sub"],
                    selectable=True,
                ),
            ],
        ),
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Text(get_string("activity_log"), size=22, weight=ft.FontWeight.BOLD),
        content=body_container,
        actions=[
            ft.TextButton(get_string("close_btn"), on_click=lambda _: page.pop_dialog()),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=820,
        base_height=440,
        width_ratio=0.84,
        height_ratio=0.72,
        min_width=440,
        max_width=1040,
        min_height=260,
        max_height=720,
    )
    page.show_dialog(dlg)
