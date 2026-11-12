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

# (filter id, string key, icon) in display order; counts come from the app.
FILTERS = (
    ("all", "filter_all", ft.Icons.FOLDER_SPECIAL_OUTLINED),
    ("running", "filter_running", ft.Icons.PLAY_CIRCLE_OUTLINE),
    ("proxy", "filter_proxy", ft.Icons.WIFI_TETHERING),
    ("direct", "filter_direct", ft.Icons.PUBLIC_OFF_OUTLINED),
    ("chrome", "filter_chrome", ft.Icons.LANGUAGE),
)


def _filter_row(
    fid: str,
    label: str,
    count: int,
    on: bool,
    on_filter: Callable[[str], None],
    icon: str | None = None,
) -> ft.Container:
    is_dark = current_theme_mode() == "dark"
    active_bg = COLORS["cover_deep"]
    active_fg = COLORS["cover_ink"]
    idle_fg = COLORS["cover_muted"] if is_dark else COLORS["text_sub"]

    left_controls: list[ft.Control] = []
    if icon:
        left_controls.append(
            ft.Icon(
                icon,
                size=15,
                color=(COLORS["accent"] if not is_dark else active_fg)
                if on
                else COLORS["cover_muted"],
            )
        )
    left_controls.append(
        ft.Text(
            label,
            size=13,
            font_family=FONT_SEMIBOLD if on else FONT_MEDIUM,
            color=active_fg if on else idle_fg,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
    )

    return ft.Container(
        height=33,
        border_radius=7,
        padding=ft.Padding.symmetric(horizontal=10),
        bgcolor=active_bg if on else None,
        ink=True,
        on_click=lambda _, f=fid: on_filter(f),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=left_controls,
                ),
                ft.Text(
                    str(count),
                    size=11.5,
                    font_family=FONT_DATA,
                    color=active_fg if on else COLORS["cover_muted"],
                ),
            ],
        ),
    )


def _nav_action_row(
    icon: str,
    label: str,
    badge: str | None,
    on_click: Callable,
) -> ft.Container:
    controls: list[ft.Control] = [
        ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(icon, size=16, color=COLORS["cover_muted"]),
                ft.Text(
                    label,
                    size=13,
                    font_family=FONT_MEDIUM,
                    color=COLORS["cover_ink"],
                ),
            ],
        ),
    ]
    if badge is not None:
        controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                border_radius=4,
                bgcolor=COLORS["cover_deep"],
                content=ft.Text(
                    badge,
                    size=11,
                    font_family=FONT_DATA,
                    color=COLORS["cover_muted"],
                ),
            )
        )
    return ft.Container(
        height=34,
        border_radius=7,
        padding=ft.Padding.symmetric(horizontal=10),
        ink=True,
        on_click=on_click,
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=controls,
        ),
    )


def _section_header(title: str) -> ft.Container:
    return ft.Container(
        padding=ft.Padding.only(left=10, top=10, bottom=3),
        content=ft.Text(
            title.upper(),
            size=10.5,
            font_family=FONT_SEMIBOLD,
            color=COLORS["cover_muted"],
        ),
    )


def build_filter_buttons(
    active: str,
    counts: dict[str, int],
    on_filter: Callable[[str], None],
    group_counts: dict[str, int] | None = None,
) -> list[ft.Control]:
    """The sidebar's profile & folder filter rows."""
    controls: list[ft.Control] = []
    for fid, key, icon in FILTERS:
        controls.append(
            _filter_row(
                fid,
                get_string(key),
                counts.get(fid, 0),
                fid == active,
                on_filter,
                icon=icon,
            )
        )

    if group_counts:
        controls.append(_section_header(get_string("group_filter_title")))
        for group_name, cnt in group_counts.items():
            fid = f"group:{group_name}"
            controls.append(
                _filter_row(
                    fid,
                    group_name,
                    cnt,
                    fid == active,
                    on_filter,
                    icon=ft.Icons.FOLDER_OUTLINED,
                )
            )
    return controls


def build_sidebar(
    stats_text: ft.Text,
    running_text: ft.Text,
    _log_text: ft.Text,
    log_column: ft.Container,
    log_toggle_btn: ft.TextButton,
    on_new_profile: Callable,
    on_import: Callable,
    on_export: Callable,
    on_toggle_log: Callable,
    on_mcp: Callable,
    on_fullscreen_log: Callable,
    filter_column: ft.Column | None = None,
    language: str = "en",
    on_language: Callable[[str], None] | None = None,
    on_open_proxy_pool: Callable | None = None,
    on_open_extensions: Callable | None = None,
    on_open_warming: Callable | None = None,
    on_toggle_theme: Callable[[str], None] | None = None,
    proxy_pool_count: int = 0,
    extension_count: int = 0,
    warming_task_count: int = 0,
    on_open_cores: Callable | None = None,
) -> ft.Container:
    log_toggle_btn.on_click = on_toggle_log
    is_dark = current_theme_mode() == "dark"

    cta_style = ft.ButtonStyle(
        shape=ft.RoundedRectangleBorder(radius=8),
        bgcolor={
            ft.ControlState.DEFAULT: COLORS["primary_cta_bg"],
            ft.ControlState.HOVERED: COLORS.get("primary_cta_hover", COLORS["accent_hover"]),
        },
        color=COLORS["primary_cta_fg"],
        text_style=ft.TextStyle(font_family=FONT_SEMIBOLD, size=13.5),
        elevation=0,
    )

    nav_scroll = ft.Column(
        spacing=2,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        controls=[
            *(
                [
                    _nav_action_row(
                        ft.Icons.DOWNLOAD_OUTLINED, get_string("core_manager"), None, on_open_cores
                    )
                ]
                if on_open_cores
                else []
            ),
            _section_header(get_string("nav_profiles_section")),
            filter_column or ft.Column(spacing=2),
            _section_header(get_string("nav_network_section")),
            *(
                [
                    _nav_action_row(
                        ft.Icons.WIFI_FIND_OUTLINED,
                        get_string("nav_proxy_pool"),
                        str(proxy_pool_count),
                        on_open_proxy_pool,
                    )
                ]
                if on_open_proxy_pool
                else []
            ),
            *(
                [
                    _nav_action_row(
                        ft.Icons.EXTENSION_OUTLINED,
                        get_string("nav_extensions"),
                        str(extension_count),
                        on_open_extensions,
                    )
                ]
                if on_open_extensions
                else []
            ),
            _section_header(get_string("nav_automation_section")),
            *(
                [
                    _nav_action_row(
                        ft.Icons.AUTO_MODE_OUTLINED,
                        get_string("nav_warming"),
                        str(warming_task_count),
                        on_open_warming,
                    )
                ]
                if on_open_warming
                else []
            ),
            _nav_action_row(
                ft.Icons.SMART_TOY_OUTLINED,
                get_string("mcp_button"),
                "MCP",
                on_mcp,
            ),
        ],
    )

    return ft.Container(
        width=236,
        bgcolor=COLORS["sidebar"],
        border=ft.Border.only(right=ft.BorderSide(1, COLORS["card_border"])),
        padding=ft.Padding.only(left=14, right=14, top=18, bottom=14),
        content=ft.Column(
            spacing=0,
            expand=True,
            controls=[
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Image(
                            src="logo.png" if is_dark else "logo-light.png",
                            width=46,
                            height=46,
                        ),
                        ft.Column(
                            spacing=0,
                            expand=True,
                            controls=[
                                ft.Row(
                                    spacing=6,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    controls=[
                                        ft.Text(
                                            get_string("app_name"),
                                            size=17,
                                            font_family=FONT_BOLD,
                                            color=COLORS["cover_ink"],
                                        ),
                                        ft.Container(
                                            padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                                            border_radius=4,
                                            bgcolor=COLORS["cover_deep"],
                                            content=ft.Text(
                                                "v2.5",
                                                size=10,
                                                font_family=FONT_DATA,
                                                color=COLORS["cover_muted"],
                                            ),
                                        ),
                                    ],
                                ),
                                ft.Text(
                                    get_string("app_subtitle"),
                                    size=11.5,
                                    color=COLORS["cover_muted"],
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Container(height=8),
                stats_text,
                ft.Container(height=2),
                running_text,
                ft.Container(height=10),
                ft.Button(
                    f"+ {get_string('new_profile')}",
                    width=208,
                    height=40,
                    style=cta_style,
                    on_click=on_new_profile,
                ),
                ft.Container(height=8),
                nav_scroll,
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        log_toggle_btn,
                        ft.IconButton(
                            icon=ft.Icons.OPEN_IN_FULL,
                            icon_size=15,
                            icon_color=COLORS["cover_muted"],
                            tooltip=get_string("activity_log"),
                            on_click=on_fullscreen_log,
                        ),
                    ],
                ),
                log_column,
            ],
        ),
    )
