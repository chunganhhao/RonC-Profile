import os
import socket
from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ..theme.colors import (
    COLORS,
    FONT_BOLD,
    FONT_DATA,
    FONT_MEDIUM,
    FONT_SEMIBOLD,
    current_theme_mode,
)
from ..theme.styles import ICON_STYLE, OUTLINE_STYLE


def _host_ram_gb() -> int:
    try:
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return max(1, round((pages * page_size) / (1024**3)))
    except Exception:
        pass
    return 16


def _host_summary() -> tuple[int, int, str]:
    cores = os.cpu_count() or 8
    ram = _host_ram_gb()
    try:
        host = socket.gethostname() or "localhost"
    except Exception:
        host = "localhost"
    return ram, cores, host


_RAM_GB, _CPU_CORES, _HOSTNAME = _host_summary()


def _toolbar_btn(
    label: str,
    icon: str,
    on_click: Callable | None,
) -> ft.OutlinedButton:
    return ft.OutlinedButton(
        label,
        icon=icon,
        height=36,
        style=OUTLINE_STYLE,
        on_click=on_click,
    )


def build_content_area(
    subtitle: ft.Text,
    profile_list: ft.Column,
    prev_btn: ft.IconButton,
    next_btn: ft.IconButton,
    page_label: ft.Text,
    bulk_bar: ft.Control | None = None,
    search_field: ft.Control | None = None,
    header_slot: ft.Container | None = None,
    on_open_warming: Callable | None = None,
    on_open_proxy_pool: Callable | None = None,
    on_import: Callable | None = None,
    on_export: Callable | None = None,
    on_refresh: Callable | None = None,
    on_toggle_theme: Callable[[str], None] | None = None,
    total_count_text: ft.Text | None = None,
    footer_proxy_text: ft.Text | None = None,
    footer_warming_text: ft.Text | None = None,
) -> ft.Container:
    """Main PionLogin-style workspace: Top header, Action toolbar, Data table, Pagination & Status bar."""
    is_dark = current_theme_mode() == "dark"

    sheet = ft.Container(
        expand=True,
        bgcolor=COLORS["card_bg"],
        border=ft.Border.all(1, COLORS["card_border"]),
        border_radius=10,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                *([] if bulk_bar is None else [bulk_bar]),
                *([] if header_slot is None else [header_slot]),
                profile_list,
            ],
        ),
    )

    toolbar_left: list[ft.Control] = []
    if on_open_warming is not None:
        toolbar_left.append(
            _toolbar_btn(
                get_string("toolbar_warming"),
                ft.Icons.AUTO_MODE_OUTLINED,
                on_open_warming,
            )
        )
    if on_open_proxy_pool is not None:
        toolbar_left.append(
            _toolbar_btn(
                get_string("toolbar_proxy_pool"),
                ft.Icons.WIFI_FIND_OUTLINED,
                on_open_proxy_pool,
            )
        )
    if on_export is not None:
        toolbar_left.append(
            _toolbar_btn(
                get_string("export"),
                ft.Icons.FILE_UPLOAD_OUTLINED,
                on_export,
            )
        )
    if on_import is not None:
        toolbar_left.append(
            _toolbar_btn(
                get_string("import"),
                ft.Icons.FILE_DOWNLOAD_OUTLINED,
                on_import,
            )
        )

    toolbar_right: list[ft.Control] = []
    if search_field is not None:
        toolbar_right.append(search_field)
    if on_refresh is not None:
        toolbar_right.append(
            ft.IconButton(
                icon=ft.Icons.REFRESH_ROUNDED,
                icon_size=18,
                icon_color=COLORS["text_sub"],
                tooltip=get_string("toolbar_sync"),
                style=ICON_STYLE,
                on_click=on_refresh,
            )
        )

    top_right_controls: list[ft.Control] = []
    if on_toggle_theme is not None:
        next_mode = "light" if is_dark else "dark"
        top_right_controls.append(
            ft.OutlinedButton(
                get_string("theme_light" if is_dark else "theme_dark"),
                icon=ft.Icons.LIGHT_MODE_OUTLINED if is_dark else ft.Icons.DARK_MODE_OUTLINED,
                height=34,
                style=OUTLINE_STYLE,
                on_click=lambda _: on_toggle_theme(next_mode),
            )
        )

    def _status_sep() -> ft.Container:
        return ft.Container(width=1, height=13, bgcolor=COLORS["card_border"])

    def _status_item(icon: str, text_ctrl: ft.Control, icon_color: str | None = None) -> ft.Row:
        return ft.Row(
            spacing=5,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(icon, size=13, color=icon_color or COLORS["text_dim"]),
                text_ctrl,
            ],
        )

    status_bar = ft.Container(
        height=30,
        padding=ft.Padding.symmetric(horizontal=18),
        bgcolor=COLORS["status_bar_bg"],
        border=ft.Border.only(top=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                _status_item(
                    ft.Icons.SIGNAL_CELLULAR_ALT_ROUNDED,
                    ft.Text(
                        get_string("footer_connection_ok"),
                        size=11.5,
                        font_family=FONT_MEDIUM,
                        color=COLORS["live"],
                    ),
                    icon_color=COLORS["live"],
                ),
                _status_sep(),
                ft.Text(
                    get_string("footer_ram", gb=_RAM_GB),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=COLORS["text_sub"],
                ),
                _status_sep(),
                ft.Text(
                    get_string("footer_cpu", cores=_CPU_CORES),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=COLORS["text_sub"],
                ),
                _status_sep(),
                ft.Text(
                    get_string("footer_device", host=_HOSTNAME),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=COLORS["text_sub"],
                ),
                *(
                    [_status_sep(), footer_proxy_text]
                    if footer_proxy_text is not None
                    else []
                ),
                *(
                    [_status_sep(), footer_warming_text]
                    if footer_warming_text is not None
                    else []
                ),
                ft.Container(expand=True),
                ft.Text(
                    get_string("footer_version"),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=COLORS["text_dim"],
                ),
            ],
        ),
    )

    main_body = ft.Container(
        expand=True,
        padding=ft.Padding.only(left=24, right=24, top=18, bottom=10),
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.BASELINE,
                            controls=[
                                ft.Text(
                                    get_string("your_profiles"),
                                    size=22,
                                    font_family=FONT_BOLD,
                                    color=COLORS["text_main"],
                                ),
                                subtitle,
                            ],
                        ),
                        ft.Row(spacing=8, controls=top_right_controls),
                    ],
                ),
                ft.Container(height=12),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(spacing=8, controls=toolbar_left),
                        ft.Row(spacing=6, controls=toolbar_right),
                    ],
                ),
                ft.Container(height=10),
                sheet,
                ft.Container(height=8),
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                            border_radius=6,
                            bgcolor=COLORS["paper_sunk"],
                            border=ft.Border.all(1, COLORS["card_border"]),
                            content=total_count_text
                            or ft.Text(
                                get_string("footer_total", count=0),
                                size=12,
                                font_family=FONT_SEMIBOLD,
                                color=COLORS["text_main"],
                            ),
                        ),
                        ft.Row(
                            spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[prev_btn, page_label, next_btn],
                        ),
                    ],
                ),
            ],
        ),
    )

    return ft.Container(
        expand=True,
        bgcolor=COLORS["bg"],
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                main_body,
                status_bar,
            ],
        ),
    )
