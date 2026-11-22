from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IProfileManager
from ..refs import UIRefs
from ..state import ITEMS_PER_PAGE, PAGE_SIZE_OPTIONS
from ..theme.colors import COLORS, FONT_DATA, FONT_MEDIUM, FONT_SEMIBOLD
from ..theme.styles import OUTLINE_STYLE


def build_ui_refs(
    pm: IProfileManager,
    on_change_page: Callable[[int], None],
    file_picker: ft.FilePicker,
    on_search: Callable[[str], None],
    on_go_page: Callable[[str], None] | None = None,
    on_change_page_size: Callable[[int], None] | None = None,
    on_toggle_running_filter: Callable[[], None] | None = None,
    initial_page_size: int = ITEMS_PER_PAGE,
) -> UIRefs:
    log_text = ft.Text(
        "",
        size=11,
        font_family=FONT_DATA,
        color=COLORS["cover_muted"],
        selectable=True,
        no_wrap=False,
    )
    log_toggle_btn = ft.TextButton(
        get_string("activity_log"),
        icon=ft.Icons.KEYBOARD_ARROW_DOWN,
        style=ft.ButtonStyle(
            color=COLORS["cover_ink"],
            overlay_color=COLORS["cover_deep"],
            text_style=ft.TextStyle(font_family=FONT_MEDIUM, size=13),
        ),
    )
    return UIRefs(
        stats_text=ft.Text(
            get_string("total_profiles", count=len(pm.profiles)),
            size=12,
            color=COLORS["cover_muted"],
        ),
        running_text=ft.Text("", size=12, color=COLORS["cover_muted"]),
        log_text=log_text,
        log_column=ft.Container(
            content=ft.Column(
                controls=[log_text],
                spacing=0,
            ),
            visible=False,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            margin=ft.Margin.symmetric(horizontal=0, vertical=4),
            border_radius=8,
            bgcolor=COLORS["log_bg"],
        ),
        log_toggle_btn=log_toggle_btn,
        content_subtitle=ft.Text("", size=13.5, color=COLORS["text_sub"]),
        profile_list_area=ft.Column(
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        ),
        prev_btn=ft.IconButton(
            key="page_prev_btn",
            icon=ft.Icons.CHEVRON_LEFT,
            icon_size=18,
            width=30,
            height=30,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("previous"),
            disabled=True,
            on_click=lambda _: on_change_page(-1),
        ),
        next_btn=ft.IconButton(
            key="page_next_btn",
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_size=18,
            width=30,
            height=30,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("next"),
            disabled=True,
            on_click=lambda _: on_change_page(1),
        ),
        first_btn=ft.IconButton(
            key="page_first_btn",
            icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT_ROUNDED,
            icon_size=18,
            width=30,
            height=30,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("first_page"),
            disabled=True,
            on_click=lambda _: on_go_page("first") if on_go_page else on_change_page(-999999),
        ),
        last_btn=ft.IconButton(
            key="page_last_btn",
            icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_RIGHT_ROUNDED,
            icon_size=18,
            width=30,
            height=30,
            icon_color=COLORS["text_sub"],
            tooltip=get_string("last_page"),
            disabled=True,
            on_click=lambda _: on_go_page("last") if on_go_page else on_change_page(999999),
        ),
        page_size_dropdown=ft.Dropdown(
            key="page_size_dropdown",
            value=str(initial_page_size),
            width=100,
            dense=True,
            bgcolor=COLORS["paper_sunk"],
            color=COLORS["text_main"],
            border_color=COLORS["card_border"],
            focused_border_color=COLORS["accent"],
            border_radius=6,
            text_size=12,
            options=[ft.dropdown.Option(str(n), str(n)) for n in PAGE_SIZE_OPTIONS],
            on_select=lambda e: (
                on_change_page_size(int(e.control.value))
                if on_change_page_size and (e.control.value or "").isdigit()
                else None
            ),
        ),
        running_filter_btn=ft.OutlinedButton(
            get_string("toolbar_running_filter", count=0),
            key="toolbar_running_filter_btn",
            icon=ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED,
            height=36,
            tooltip=get_string("toolbar_running_tooltip"),
            style=OUTLINE_STYLE,
            on_click=lambda _: on_toggle_running_filter() if on_toggle_running_filter else None,
        ),
        page_label=ft.Text(
            get_string("page_of", current=1, total=1),
            size=12.5,
            font_family=FONT_SEMIBOLD,
            color=COLORS["text_sub"],
        ),
        bulk_bar=ft.Row(visible=False, spacing=0, controls=[]),
        file_picker=file_picker,
        search_field=ft.TextField(
            hint_text=get_string("search_hint"),
            prefix_icon=ft.Icons.SEARCH,
            width=320,
            height=40,
            dense=True,
            border_radius=8,
            bgcolor=COLORS["input_bg"],
            color=COLORS["text_main"],
            text_size=14,
            border_color=COLORS["rule_strong"],
            focused_border_color=COLORS["accent"],
            cursor_color=COLORS["accent"],
            hint_style=ft.TextStyle(color=COLORS["text_dim"]),
            on_change=lambda e: on_search(e.control.value or ""),
        ),
    )
