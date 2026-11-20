from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import flet as ft

from ...core.strings import get_string
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import (
    ACCENT_STYLE,
    DANGER_ICON_STYLE,
    DLG_FIELD_KWARGS,
    ICON_STYLE,
    OUTLINE_STYLE,
)
from .responsive import bind_responsive_dialog

if TYPE_CHECKING:
    from ...interfaces.protocols import IProfileManager, IProxyService
    from ...services.proxy.pool import ProxyPoolEntry, ProxyPoolService

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)


def open_proxy_pool_dialog(
    page: ft.Page,
    pool_service: ProxyPoolService,
    proxy_service: IProxyService,
    pm: IProfileManager,
    target_profile_names: list[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
    refresh_fn: Callable[[], None] | None = None,
    on_pick_proxy: Callable[[str], None] | None = None,
) -> None:
    """Open the Centralized Proxy Pool management & bulk auto-assign dialog."""
    targets = list(target_profile_names or [p.name for p in pm.list_profiles()])
    log = log_fn or (lambda _: None)

    bulk_input = ft.TextField(
        multiline=True,
        min_lines=3,
        max_lines=4,
        hint_text="198.51.100.10:8080:user:pass|US-Resi\nsocks5://user:pass@203.0.113.5:1080|VN-4G",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    tag_input = ft.TextField(
        hint_text=get_string("proxy_pool_tag_hint"),
        width=220,
        **DLG_FIELD_KWARGS,
    )
    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    subtitle_text = ft.Text("", size=13, color=COLORS["text_sub"])
    list_col = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)

    def _status_badge(entry: ProxyPoolEntry) -> ft.Container:
        if entry.status == "working":
            fg = COLORS["success"]
            label = get_string("proxy_pool_status_working")
            extra = f" · {entry.exit_ip}" if entry.exit_ip else ""
            ms = f" ({entry.latency_ms}ms)" if entry.latency_ms else ""
            text = f"● {label}{extra}{ms}"
        elif entry.status == "dead":
            fg = COLORS["error"]
            text = f"✕ {get_string('proxy_pool_status_dead')}"
        else:
            fg = COLORS["text_dim"]
            text = f"○ {get_string('proxy_pool_status_untested')}"
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=6,
            bgcolor=COLORS["bg"],
            border=ft.Border.all(1, COLORS["card_border"]),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Text(
                text,
                size=11.5,
                font_family=FONT_DATA,
                color=fg,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=text,
            ),
        )

    def _check_single(pid: str) -> None:
        def _worker() -> None:
            pool_service.check_entry(pid, proxy_service)
            _rebuild_rows(notify=True)

        threading.Thread(target=_worker, daemon=True).start()

    def _delete_single(pid: str) -> None:
        pool_service.remove_proxy(pid)
        _rebuild_rows(notify=True)
        if refresh_fn:
            refresh_fn()

    def _use_single(proxy_str: str) -> None:
        if on_pick_proxy:
            on_pick_proxy(proxy_str)
            page.pop_dialog()

    def _rebuild_rows(notify: bool = False) -> None:
        entries = pool_service.list_entries()
        usage = pool_service.usage_counts(pm)
        subtitle_text.value = get_string("proxy_pool_subtitle", count=len(entries))
        if not entries:
            list_col.controls = [
                ft.Container(
                    padding=24,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("proxy_pool_empty_hint"),
                        size=13,
                        color=COLORS["text_dim"],
                    ),
                )
            ]
        else:
            rows: list[ft.Control] = []
            for e in entries:
                used_cnt = usage.get(e.id, 0)
                row_actions: list[ft.Control] = []
                if on_pick_proxy is not None:
                    row_actions.append(
                        ft.OutlinedButton(
                            get_string("proxy_pool_use_btn"),
                            height=30,
                            style=OUTLINE_STYLE,
                            on_click=lambda _, p=e.proxy: _use_single(p),
                        )
                    )
                row_actions.extend(
                    [
                        ft.IconButton(
                            icon=ft.Icons.WIFI_FIND,
                            icon_size=17,
                            icon_color=COLORS["text_sub"],
                            tooltip=get_string("check_proxy"),
                            style=ICON_STYLE,
                            on_click=lambda _, pid=e.id: _check_single(pid),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=17,
                            icon_color=COLORS["text_sub"],
                            tooltip=get_string("delete"),
                            style=DANGER_ICON_STYLE,
                            on_click=lambda _, pid=e.id: _delete_single(pid),
                        ),
                    ]
                )
                rows.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        border_radius=8,
                        bgcolor=COLORS["bg"],
                        border=ft.Border.all(1, COLORS["card_border"]),
                        content=ft.Row(
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Container(
                                    expand=4,
                                    content=ft.Row(
                                        spacing=8,
                                        controls=[
                                            ft.Icon(
                                                ft.Icons.SHIELD_OUTLINED,
                                                size=16,
                                                color=COLORS["accent"],
                                            ),
                                            ft.Text(
                                                e.masked,
                                                size=12.5,
                                                font_family=FONT_DATA,
                                                color=COLORS["text_main"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                            ),
                                            *(
                                                [
                                                    ft.Container(
                                                        padding=ft.Padding.symmetric(
                                                            horizontal=6, vertical=1
                                                        ),
                                                        border_radius=4,
                                                        bgcolor=COLORS["card_bg"],
                                                        border=ft.Border.all(
                                                            1, COLORS["rule_strong"]
                                                        ),
                                                        content=ft.Text(
                                                            e.label,
                                                            size=10.5,
                                                            font_family=FONT_SEMIBOLD,
                                                            color=COLORS["text_sub"],
                                                        ),
                                                    )
                                                ]
                                                if e.label
                                                else []
                                            ),
                                        ],
                                    ),
                                ),
                                ft.Container(expand=3, content=_status_badge(e)),
                                ft.Container(
                                    width=95,
                                    content=ft.Text(
                                        get_string("proxy_pool_used_by", count=used_cnt),
                                        size=11.5,
                                        font_family=FONT_DATA,
                                        color=COLORS["text_sub"],
                                    ),
                                ),
                                ft.Row(spacing=2, controls=row_actions),
                            ],
                        ),
                    )
                )
            list_col.controls = rows
        if notify:
            with contextlib.suppress(Exception):
                page.update()

    def _on_import(_: ft.ControlEvent) -> None:
        raw = (bulk_input.value or "").strip()
        if not raw:
            return
        added, invalid = pool_service.bulk_import(
            raw,
            default_label=(tag_input.value or "").strip(),
        )
        bulk_input.value = ""
        msg = get_string("proxy_pool_imported_msg", added=added, invalid=invalid)
        feedback_text.value = msg
        feedback_text.color = COLORS["success"] if added > 0 else COLORS["warning"]
        feedback_text.visible = True
        log(msg)
        _rebuild_rows(notify=True)
        if refresh_fn:
            refresh_fn()

    check_all_btn = ft.OutlinedButton(
        get_string("proxy_pool_check_all_btn"),
        icon=ft.Icons.BOLT,
        height=36,
        style=OUTLINE_STYLE,
    )

    def _on_check_all(_: ft.ControlEvent) -> None:
        check_all_btn.disabled = True
        with contextlib.suppress(Exception):
            page.update()

        def _worker() -> None:
            pool_service.check_all(proxy_service)
            check_all_btn.disabled = False
            _rebuild_rows(notify=True)

        threading.Thread(target=_worker, daemon=True).start()

    check_all_btn.on_click = _on_check_all

    def _on_clear_dead(_: ft.ControlEvent) -> None:
        removed = pool_service.clear_dead()
        msg = get_string("proxy_pool_cleared_dead_msg", count=removed)
        feedback_text.value = msg
        feedback_text.color = COLORS["text_sub"]
        feedback_text.visible = True
        log(msg)
        _rebuild_rows(notify=True)
        if refresh_fn:
            refresh_fn()

    def _on_auto_assign(_: ft.ControlEvent) -> None:
        updated = pool_service.assign_round_robin(targets, pm, only_working=False)
        msg = get_string("proxy_pool_assigned_msg", count=updated)
        feedback_text.value = msg
        feedback_text.color = COLORS["success"] if updated > 0 else COLORS["warning"]
        feedback_text.visible = True
        log(msg)
        _rebuild_rows(notify=True)
        if refresh_fn:
            refresh_fn()

    _rebuild_rows(notify=False)

    body_container = ft.Container(
        width=860,
        height=540,
        content=ft.Column(
            spacing=10,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(
                    get_string("proxy_pool_bulk_label"),
                    size=12,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
                bulk_input,
                ft.Row(
                    spacing=8,
                    run_spacing=8,
                    wrap=True,
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(
                            spacing=8,
                            run_spacing=8,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                tag_input,
                                ft.Button(
                                    get_string("proxy_pool_import_btn"),
                                    icon=ft.Icons.ADD,
                                    height=38,
                                    style=ACCENT_STYLE,
                                    on_click=_on_import,
                                ),
                                check_all_btn,
                                ft.OutlinedButton(
                                    get_string("proxy_pool_clear_dead_btn"),
                                    icon=ft.Icons.CLEANING_SERVICES_OUTLINED,
                                    height=36,
                                    style=OUTLINE_STYLE,
                                    on_click=_on_clear_dead,
                                ),
                            ],
                        ),
                        ft.OutlinedButton(
                            get_string("proxy_pool_assign_btn", count=len(targets)),
                            icon=ft.Icons.AUTO_AWESOME_OUTLINED,
                            height=36,
                            style=OUTLINE_STYLE,
                            on_click=_on_auto_assign,
                        ),
                    ],
                ),
                feedback_text,
                ft.Divider(height=8, color=COLORS["card_border"]),
                list_col,
            ],
        ),
    )

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=16),
        title=ft.Column(
            spacing=3,
            tight=True,
            controls=[
                ft.Text(
                    get_string("proxy_pool_title"),
                    size=19,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                subtitle_text,
            ],
        ),
        content=body_container,
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda _: page.pop_dialog(),
            ),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=860,
        base_height=540,
        width_ratio=0.86,
        height_ratio=0.80,
        min_width=480,
        max_width=1060,
        min_height=360,
        max_height=780,
    )
    page.show_dialog(dlg)
