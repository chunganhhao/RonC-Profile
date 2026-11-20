from __future__ import annotations

import contextlib
import time
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
    from ...interfaces.protocols import IBrowserLauncher, IProfileManager
    from ...services.warming.scheduler import WarmingSchedulerService, WarmingTask

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)


def open_warming_dialog(
    page: ft.Page,
    warming_service: WarmingSchedulerService,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    target_profile_names: list[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
    refresh_fn: Callable[[], None] | None = None,
) -> None:
    """Open the Account Warming Scheduler & Automated Interaction dialog."""
    all_names = [p.name for p in pm.list_profiles()]
    targets = list(target_profile_names) if target_profile_names else all_names
    log = log_fn or (lambda _: None)

    task_name_field = ft.TextField(
        value=f"Warm {len(targets)} profile(s)",
        **DLG_FIELD_KWARGS,
    )
    preset_dropdown = ft.Dropdown(
        value="youtube_trust",
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["accent"],
        border_radius=8,
        text_size=13,
        options=[
            ft.dropdown.Option("youtube_trust", get_string("warming_preset_youtube")),
            ft.dropdown.Option("social_news", get_string("warming_preset_social")),
            ft.dropdown.Option("ecommerce_cookie", get_string("warming_preset_ecommerce")),
            ft.dropdown.Option("custom", get_string("warming_preset_custom")),
        ],
    )
    custom_urls_field = ft.TextField(
        multiline=True,
        min_lines=2,
        max_lines=3,
        hint_text="https://www.youtube.com\nhttps://docs.google.com",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    duration_field = ft.TextField(
        value="3",
        width=130,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    schedule_field = ft.TextField(
        value="",
        hint_text=get_string("warming_schedule_hint"),
        width=180,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    repeat_checkbox = ft.Checkbox(
        label=get_string("warming_repeat_daily"),
        value=True,
    )
    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    tasks_col = ft.Column(spacing=5, scroll=ft.ScrollMode.AUTO, expand=True)

    def _status_badge(task: WarmingTask) -> ft.Container:
        if task.status == "running":
            fg = COLORS["live"]
            label = f"● {get_string('stamp_live')}"
        elif task.status == "completed":
            fg = COLORS["success"]
            label = f"✓ {task.last_result or 'OK'}"
        elif task.status == "failed":
            fg = COLORS["error"]
            label = f"✕ {task.last_result or get_string('stamp_failed')}"
        else:
            fg = COLORS["text_dim"]
            label = f"○ {task.last_result or get_string('stamp_idle')}"
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=6,
            bgcolor=COLORS["bg"],
            border=ft.Border.all(1, COLORS["card_border"]),
            content=ft.Text(label, size=11.5, font_family=FONT_DATA, color=fg),
        )

    def _run_task(tid: str) -> None:
        warming_service.run_task_async(
            tid,
            pm,
            bl,
            log_fn=log,
            on_update=lambda: _rebuild_tasks(notify=True),
        )
        _rebuild_tasks(notify=True)

    def _stop_task(tid: str) -> None:
        warming_service.stop_task(tid)
        _rebuild_tasks(notify=True)

    def _delete_task(tid: str) -> None:
        warming_service.delete_task(tid)
        _rebuild_tasks(notify=True)
        if refresh_fn:
            refresh_fn()

    def _rebuild_tasks(notify: bool = False) -> None:
        tasks = warming_service.list_tasks()
        if not tasks:
            tasks_col.controls = [
                ft.Container(
                    padding=20,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        get_string("warming_no_tasks"),
                        size=13,
                        color=COLORS["text_dim"],
                    ),
                )
            ]
        else:
            rows: list[ft.Control] = []
            for t in reversed(tasks):
                sched_tag = f"⏰ {t.schedule_time}" if t.schedule_time else "⚡ Manual"
                last_str = (
                    time.strftime("%H:%M:%S", time.localtime(t.last_run)) if t.last_run else "-"
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
                                    content=ft.Column(
                                        spacing=2,
                                        tight=True,
                                        controls=[
                                            ft.Text(
                                                t.name,
                                                size=13,
                                                font_family=FONT_SEMIBOLD,
                                                color=COLORS["text_main"],
                                                no_wrap=True,
                                                overflow=ft.TextOverflow.ELLIPSIS,
                                            ),
                                            ft.Text(
                                                f"{t.preset} · {len(t.profile_names)} profile(s) · {t.duration_minutes}m · Last: {last_str}",
                                                size=11.5,
                                                font_family=FONT_DATA,
                                                color=COLORS["text_dim"],
                                            ),
                                        ],
                                    ),
                                ),
                                ft.Container(
                                    width=95,
                                    content=ft.Text(
                                        sched_tag,
                                        size=12,
                                        font_family=FONT_DATA,
                                        color=COLORS["accent"]
                                        if t.schedule_time
                                        else COLORS["text_sub"],
                                    ),
                                ),
                                ft.Container(expand=3, content=_status_badge(t)),
                                ft.Row(
                                    spacing=2,
                                    controls=[
                                        ft.IconButton(
                                            icon=ft.Icons.STOP_ROUNDED
                                            if t.status == "running"
                                            else ft.Icons.PLAY_ARROW_ROUNDED,
                                            icon_size=18,
                                            icon_color=COLORS["error"]
                                            if t.status == "running"
                                            else COLORS["live"],
                                            tooltip=get_string(
                                                "stop" if t.status == "running" else "launch"
                                            ),
                                            style=ICON_STYLE,
                                            on_click=lambda _, tid=t.id, r=(t.status == "running"): (
                                                _stop_task(tid) if r else _run_task(tid)
                                            ),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.DELETE_OUTLINE,
                                            icon_size=17,
                                            icon_color=COLORS["text_sub"],
                                            tooltip=get_string("delete"),
                                            style=DANGER_ICON_STYLE,
                                            on_click=lambda _, tid=t.id: _delete_task(tid),
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    )
                )
            tasks_col.controls = rows
        if notify:
            with contextlib.suppress(Exception):
                page.update()

    def _create_from_inputs(run_immediately: bool) -> None:
        name = (task_name_field.value or "").strip()
        preset = preset_dropdown.value or "youtube_trust"
        raw_urls = (custom_urls_field.value or "").strip()
        dur_str = (duration_field.value or "3").strip()
        dur = int(dur_str) if dur_str.isdigit() else 3
        sched = "" if run_immediately else (schedule_field.value or "").strip()

        task = warming_service.add_task(
            name=name,
            profile_names=targets,
            preset=preset,
            custom_urls=raw_urls,
            duration_minutes=dur,
            schedule_time=sched,
            repeat_daily=bool(repeat_checkbox.value),
            enabled=True,
        )
        if task is None:
            feedback_text.value = get_string("warming_invalid_msg")
            feedback_text.color = COLORS["error"]
            feedback_text.visible = True
            with contextlib.suppress(Exception):
                page.update()
            return

        if run_immediately:
            msg = get_string("warming_started_msg", name=task.name, count=len(targets))
            feedback_text.value = msg
            feedback_text.color = COLORS["success"]
            feedback_text.visible = True
            log(msg)
            _run_task(task.id)
        else:
            msg = get_string(
                "warming_saved_msg",
                name=task.name,
                sched=task.schedule_time or "manual",
            )
            feedback_text.value = msg
            feedback_text.color = COLORS["success"]
            feedback_text.visible = True
            log(msg)
            _rebuild_tasks(notify=True)
        if refresh_fn:
            refresh_fn()

    _rebuild_tasks(notify=False)

    body_container = ft.Container(
        width=860,
        height=550,
        content=ft.Column(
            spacing=8,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=10,
                    controls=[
                        ft.Container(
                            expand=2,
                            content=ft.Column(
                                spacing=4,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                                controls=[
                                    ft.Text(
                                        get_string("warming_task_name_label"),
                                        size=12,
                                        font_family=FONT_SEMIBOLD,
                                        color=COLORS["text_sub"],
                                    ),
                                    task_name_field,
                                ],
                            ),
                        ),
                        ft.Container(
                            expand=3,
                            content=ft.Column(
                                spacing=4,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                                controls=[
                                    ft.Text(
                                        get_string("warming_preset_label"),
                                        size=12,
                                        font_family=FONT_SEMIBOLD,
                                        color=COLORS["text_sub"],
                                    ),
                                    preset_dropdown,
                                ],
                            ),
                        ),
                    ],
                ),
                ft.Text(
                    get_string("warming_custom_urls_label"),
                    size=12,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_sub"],
                ),
                custom_urls_field,
                ft.Row(
                    spacing=10,
                    run_spacing=8,
                    wrap=True,
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.END,
                    controls=[
                        ft.Row(
                            spacing=10,
                            run_spacing=8,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.END,
                            controls=[
                                ft.Column(
                                    spacing=4,
                                    tight=True,
                                    controls=[
                                        ft.Text(
                                            get_string("warming_duration_label"),
                                            size=12,
                                            font_family=FONT_SEMIBOLD,
                                            color=COLORS["text_sub"],
                                        ),
                                        duration_field,
                                    ],
                                ),
                                ft.Column(
                                    spacing=4,
                                    tight=True,
                                    controls=[
                                        ft.Text(
                                            get_string("warming_schedule_label"),
                                            size=12,
                                            font_family=FONT_SEMIBOLD,
                                            color=COLORS["text_sub"],
                                        ),
                                        schedule_field,
                                    ],
                                ),
                                repeat_checkbox,
                            ],
                        ),
                        ft.Row(
                            spacing=8,
                            run_spacing=8,
                            wrap=True,
                            controls=[
                                ft.OutlinedButton(
                                    get_string("warming_save_schedule_btn"),
                                    icon=ft.Icons.SCHEDULE,
                                    height=38,
                                    style=OUTLINE_STYLE,
                                    on_click=lambda _: _create_from_inputs(run_immediately=False),
                                ),
                                ft.Button(
                                    get_string("warming_run_now_btn"),
                                    icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                    height=38,
                                    style=ACCENT_STYLE,
                                    on_click=lambda _: _create_from_inputs(run_immediately=True),
                                ),
                            ],
                        ),
                    ],
                ),
                feedback_text,
                ft.Divider(height=8, color=COLORS["card_border"]),
                ft.Text(
                    get_string("warming_saved_tasks_title"),
                    size=13,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                tasks_col,
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
                    get_string("warming_title"),
                    size=19,
                    font_family=FONT_BOLD,
                    color=COLORS["text_main"],
                ),
                ft.Text(
                    get_string("warming_subtitle", count=len(targets)),
                    size=13,
                    color=COLORS["text_sub"],
                ),
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
        base_height=550,
        width_ratio=0.86,
        height_ratio=0.82,
        min_width=480,
        max_width=1060,
        min_height=380,
        max_height=800,
    )
    page.show_dialog(dlg)
