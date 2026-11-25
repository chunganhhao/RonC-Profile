from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from ...core.strings import get_string
from ...services.warming.scheduler import (
    SAMPLE_WARMING_SCRIPT,
    WARMING_PRESETS,
    WARMING_SCENARIO_META,
    WARMING_SCENARIO_PLAYBOOKS,
    _normalize_step,
    describe_warming_step,
    normalize_warming_steps,
    normalize_warming_urls,
)
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import (
    ACCENT_STYLE,
    DANGER_ICON_STYLE,
    DLG_FIELD_KWARGS,
    ICON_STYLE,
    OUTLINE_STYLE,
)
from .modal import show_modal_dialog
from .responsive import bind_responsive_dialog

if TYPE_CHECKING:
    from ...interfaces.protocols import IBrowserLauncher, IProfileManager
    from ...services.warming.scheduler import WarmingSchedulerService, WarmingTask

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)

_SCENARIO_CARDS: tuple[tuple[str, Any, str, str], ...] = (
    (
        "youtube_trust",
        ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED,
        "warming_card_youtube_title",
        "warming_card_youtube_desc",
    ),
    (
        "social_news",
        ft.Icons.FORUM_OUTLINED,
        "warming_card_social_title",
        "warming_card_social_desc",
    ),
    (
        "ecommerce_cookie",
        ft.Icons.SHOPPING_BAG_OUTLINED,
        "warming_card_ecommerce_title",
        "warming_card_ecommerce_desc",
    ),
    (
        "custom",
        ft.Icons.LINK_ROUNDED,
        "warming_card_custom_title",
        "warming_card_custom_desc",
    ),
    (
        "script",
        ft.Icons.TUNE_ROUNDED,
        "warming_card_script_title",
        "warming_card_script_desc",
    ),
)

_BUILDER_ACTIONS: tuple[str, ...] = (
    "navigate",
    "wait",
    "scroll",
    "type",
    "click",
    "press",
    "wait_for",
    "back",
)


def open_warming_dialog(
    page: ft.Page,
    warming_service: WarmingSchedulerService,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    target_profile_names: list[str] | None = None,
    log_fn: Callable[[str], None] | None = None,
    refresh_fn: Callable[[], None] | None = None,
    on_dismiss: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Open the Account Warming Scheduler & Visual Playbook Studio dialog."""
    all_names = [p.name for p in pm.list_profiles()]
    targets = list(target_profile_names) if target_profile_names else all_names
    log = log_fn or (lambda _: None)

    # Hidden synced dropdown preserves existing automation/test selectors cleanly.
    preset_dropdown = ft.Dropdown(
        value="youtube_trust",
        visible=False,
        options=[
            ft.dropdown.Option("youtube_trust", get_string("warming_preset_youtube")),
            ft.dropdown.Option("social_news", get_string("warming_preset_social")),
            ft.dropdown.Option("ecommerce_cookie", get_string("warming_preset_ecommerce")),
            ft.dropdown.Option("custom", get_string("warming_preset_custom")),
            ft.dropdown.Option("script", get_string("warming_preset_script")),
        ],
    )

    # Keep task_name_field as TextField[0] and custom_urls_field as TextField[1].
    task_name_field = ft.TextField(
        value=get_string("warming_default_task_name", count=len(targets)),
        **DLG_FIELD_KWARGS,
    )
    payload_label = ft.Text(
        get_string("warming_custom_urls_label"),
        size=12,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_sub"],
    )
    custom_urls_field = ft.TextField(
        multiline=True,
        min_lines=2,
        max_lines=4,
        hint_text="https://www.youtube.com\nhttps://docs.google.com",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    drafts = {"url": "", "script": ""}
    active_mode = ["url"]
    card_containers: dict[str, ft.Container] = {}
    card_icons: dict[str, ft.Icon] = {}

    steps_header_title = ft.Text(
        get_string("warming_steps_url_title"),
        size=12.5,
        font_family=FONT_SEMIBOLD,
        color=COLORS["text_main"],
    )
    steps_mode_note = ft.Text(
        "",
        size=11.5,
        color=COLORS["text_dim"],
        visible=False,
    )
    steps_count_text = ft.Text(
        "",
        size=11.5,
        font_family=FONT_DATA,
        color=COLORS["text_dim"],
    )
    steps_preview_col = ft.Column(spacing=4, tight=True)
    steps_preview_box = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=8,
        bgcolor=COLORS["bg"],
        border=ft.Border.all(1, COLORS["card_border"]),
        content=steps_preview_col,
    )

    duration_field = ft.TextField(
        value="3",
        width=110,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    schedule_field = ft.TextField(
        value="",
        hint_text=get_string("warming_schedule_hint"),
        width=260,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    repeat_checkbox = ft.Checkbox(
        label=get_string("warming_repeat_daily"),
        value=True,
    )
    # Keep feedback_text as the only hidden size=12 ft.Text in this dialog.
    feedback_text = ft.Text("", size=12, color=COLORS["success"], visible=False)
    tasks_col = ft.Column(spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def _safe_update() -> None:
        with contextlib.suppress(Exception):
            page.update()

    def _parse_draft_steps() -> tuple[list[dict[str, Any]] | None, str | None]:
        raw = (custom_urls_field.value or "").strip()
        if not raw:
            return [], None
        try:
            loaded = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            return None, str(exc)
        if not isinstance(loaded, list):
            return None, "Script must be a JSON array"
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(loaded, 1):
            try:
                out.append(_normalize_step(idx, item))
            except ValueError as exc:
                return None, str(exc)
        return out, None

    def _write_script_steps(steps: list[dict[str, Any]], *, notify: bool = True) -> None:
        encoded = json.dumps(steps, indent=2, ensure_ascii=False)
        custom_urls_field.value = encoded
        drafts["script"] = encoded
        _refresh_steps_preview(notify=False)
        if notify:
            _safe_update()

    def _move_step(idx: int, delta: int) -> None:
        steps, err = _parse_draft_steps()
        if err is not None or steps is None:
            _show_error(get_string("warming_script_sync_error"))
            return
        new_idx = idx + delta
        if 0 <= idx < len(steps) and 0 <= new_idx < len(steps):
            steps[idx], steps[new_idx] = steps[new_idx], steps[idx]
            _write_script_steps(steps, notify=True)

    def _remove_step(idx: int) -> None:
        steps, err = _parse_draft_steps()
        if err is not None or steps is None:
            _show_error(get_string("warming_script_sync_error"))
            return
        if 0 <= idx < len(steps):
            steps.pop(idx)
            _write_script_steps(steps, notify=True)

    def _refresh_steps_preview(*, notify: bool = False) -> None:
        preset = preset_dropdown.value or "youtube_trust"
        rows: list[ft.Control] = []
        if preset == "script":
            editable = True
            steps, parse_err = _parse_draft_steps()
            steps = steps or []
            steps_header_title.value = get_string("warming_steps_script_title")
            steps_mode_note.visible = False
        else:
            editable = False
            parse_err = None
            raw_tokens = [u.strip() for u in (custom_urls_field.value or "").split() if u.strip()]
            valid_urls = normalize_warming_urls(raw_tokens)
            dropped_count = len(raw_tokens) - len(valid_urls)
            using_override = bool(raw_tokens)
            if using_override and not valid_urls:
                steps = []
                parse_err = get_string("warming_override_invalid_msg")
                steps_header_title.value = get_string(
                    "warming_steps_url_override_title"
                    if preset in WARMING_SCENARIO_PLAYBOOKS
                    else "warming_steps_url_title"
                )
                steps_mode_note.visible = False
            else:
                effective_urls = valid_urls if valid_urls else list(WARMING_PRESETS.get(preset, []))
                steps = []
                for url in effective_urls:
                    steps.append({"action": "navigate", "url": url})
                    steps.append({"action": "scroll", "delta_y": 480, "humanize": True})
                if preset in WARMING_SCENARIO_PLAYBOOKS:
                    steps_header_title.value = get_string(
                        "warming_steps_url_override_title"
                        if valid_urls
                        else "warming_steps_url_title"
                    )
                else:
                    steps_header_title.value = get_string("warming_steps_url_title")
                if dropped_count > 0:
                    steps_mode_note.value = get_string(
                        "warming_urls_ignored_warn", count=dropped_count
                    )
                    steps_mode_note.color = COLORS["warn_text"]
                    steps_mode_note.visible = True
                elif preset in WARMING_SCENARIO_PLAYBOOKS:
                    steps_mode_note.value = get_string(
                        "warming_url_mode_template_note",
                        count=len(WARMING_SCENARIO_PLAYBOOKS[preset]),
                    )
                    steps_mode_note.color = COLORS["text_dim"]
                    steps_mode_note.visible = True
                else:
                    steps_mode_note.visible = False

        steps_count_text.value = get_string("warming_steps_count_badge", count=len(steps))
        if parse_err:
            rows.append(
                ft.Text(
                    get_string("warming_script_invalid_msg", error=parse_err)
                    if preset == "script"
                    else parse_err,
                    size=11.5,
                    color=COLORS["error"],
                )
            )
        elif not steps:
            rows.append(
                ft.Text(
                    get_string("warming_card_custom_desc")
                    if preset == "custom"
                    else get_string("warming_card_script_desc"),
                    size=11.5,
                    color=COLORS["text_dim"],
                )
            )
        else:
            for idx, step in enumerate(steps):
                act_lbl, detail = describe_warming_step(step)
                is_opt = bool(step.get("optional"))
                row_controls: list[ft.Control] = [
                    ft.Text(
                        f"{idx + 1:02d}",
                        size=11,
                        font_family=FONT_DATA,
                        color=COLORS["text_dim"],
                        width=22,
                    ),
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                        border_radius=5,
                        bgcolor=COLORS["accent_soft"],
                        content=ft.Text(
                            f"{act_lbl}?" if is_opt else act_lbl,
                            size=11,
                            font_family=FONT_DATA,
                            color=COLORS["accent"],
                        ),
                    ),
                    ft.Container(
                        expand=True,
                        content=ft.Text(
                            detail,
                            size=11.5,
                            font_family=FONT_DATA,
                            color=COLORS["text_main"],
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ),
                ]
                if editable:
                    row_controls.extend(
                        [
                            ft.IconButton(
                                icon=ft.Icons.ARROW_UPWARD_ROUNDED,
                                icon_size=14,
                                icon_color=COLORS["text_sub"],
                                tooltip=get_string("warming_move_up"),
                                disabled=(idx == 0),
                                style=ICON_STYLE,
                                on_click=lambda _, i=idx: _move_step(i, -1),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.ARROW_DOWNWARD_ROUNDED,
                                icon_size=14,
                                icon_color=COLORS["text_sub"],
                                tooltip=get_string("warming_move_down"),
                                disabled=(idx == len(steps) - 1),
                                style=ICON_STYLE,
                                on_click=lambda _, i=idx: _move_step(i, 1),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE_ROUNDED,
                                icon_size=14,
                                icon_color=COLORS["text_sub"],
                                tooltip=get_string("warming_remove_step"),
                                style=DANGER_ICON_STYLE,
                                on_click=lambda _, i=idx: _remove_step(i),
                            ),
                        ]
                    )
                rows.append(
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=4, vertical=3),
                        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
                        content=ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=row_controls,
                        ),
                    )
                )
        steps_preview_col.controls = rows
        if notify:
            _safe_update()

    pending_replace: list[tuple[list[dict[str, Any]], int | None] | None] = [None]

    def _apply_loaded_script(
        steps: list[dict[str, Any]], *, recommended_minutes: int | None = None
    ) -> None:
        pending_replace[0] = None
        replace_confirm_bar.visible = False
        preset_dropdown.value = "script"
        _sync_preset_mode(None)
        _write_script_steps([dict(s) for s in steps], notify=False)
        if recommended_minutes is not None:
            duration_field.value = str(recommended_minutes)
        _safe_update()

    def _request_load_script(
        steps: list[dict[str, Any]], *, recommended_minutes: int | None = None
    ) -> None:
        existing = (
            (custom_urls_field.value or "") if active_mode[0] == "script" else drafts["script"]
        ).strip()
        target_json = json.dumps([dict(s) for s in steps], indent=2, ensure_ascii=False)
        if existing and existing != target_json:
            pending_replace[0] = ([dict(s) for s in steps], recommended_minutes)
            replace_confirm_bar.visible = True
            _safe_update()
            return
        _apply_loaded_script(steps, recommended_minutes=recommended_minutes)

    def _cancel_replace(_: ft.ControlEvent | None = None) -> None:
        pending_replace[0] = None
        replace_confirm_bar.visible = False
        _safe_update()

    def _confirm_replace(_: ft.ControlEvent | None = None) -> None:
        req = pending_replace[0]
        if req is None:
            replace_confirm_bar.visible = False
            _safe_update()
            return
        steps, rec_mins = req
        _apply_loaded_script(steps, recommended_minutes=rec_mins)

    replace_confirm_bar = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        border_radius=8,
        bgcolor=COLORS["warn_tint"],
        border=ft.Border.all(1, COLORS["warning"]),
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=15, color=COLORS["warn_text"]),
                ft.Container(
                    expand=True,
                    content=ft.Text(
                        get_string("warming_replace_draft_confirm"),
                        size=11.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["warn_text"],
                    ),
                ),
                ft.TextButton(get_string("cancel"), on_click=_cancel_replace),
                ft.OutlinedButton(
                    get_string("warming_replace_draft_btn"),
                    height=28,
                    style=OUTLINE_STYLE,
                    on_click=_confirm_replace,
                ),
            ],
        ),
    )

    def _insert_sample_script(_: ft.ControlEvent | None = None) -> None:
        _request_load_script(list(SAMPLE_WARMING_SCRIPT))

    sample_script_btn = ft.TextButton(
        get_string("warming_script_sample_btn"),
        icon=ft.Icons.CODE_ROUNDED,
        visible=False,
        on_click=_insert_sample_script,
    )

    def _load_template(template_id: str) -> None:
        steps = WARMING_SCENARIO_PLAYBOOKS.get(template_id)
        if not steps:
            return
        meta_mins = WARMING_SCENARIO_META.get(template_id, {}).get("recommended_minutes")
        _request_load_script(
            steps,
            recommended_minutes=int(meta_mins) if meta_mins else None,
        )

    customize_btn = ft.OutlinedButton(
        get_string("warming_customize_script_btn"),
        icon=ft.Icons.EDIT_NOTE_ROUNDED,
        height=32,
        style=OUTLINE_STYLE,
        on_click=lambda _: _load_template(preset_dropdown.value or "youtube_trust"),
    )
    template_pills_row = ft.Row(
        spacing=6,
        visible=False,
        wrap=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(
                get_string("warming_load_template_label"),
                size=11.5,
                font_family=FONT_SEMIBOLD,
                color=COLORS["text_sub"],
            ),
            ft.OutlinedButton(
                "YouTube",
                height=28,
                style=OUTLINE_STYLE,
                on_click=lambda _: _load_template("youtube_trust"),
            ),
            ft.OutlinedButton(
                "Social",
                height=28,
                style=OUTLINE_STYLE,
                on_click=lambda _: _load_template("social_news"),
            ),
            ft.OutlinedButton(
                "E-Commerce",
                height=28,
                style=OUTLINE_STYLE,
                on_click=lambda _: _load_template("ecommerce_cookie"),
            ),
        ],
    )

    step_action_dropdown = ft.Dropdown(
        value="navigate",
        width=130,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["accent"],
        border_radius=8,
        text_size=12.5,
        options=[ft.dropdown.Option(a, a) for a in _BUILDER_ACTIONS],
    )
    step_value_field = ft.TextField(
        expand=2,
        hint_text="https://example.com",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    step_extra_field = ft.TextField(
        expand=1,
        hint_text="Search text",
        visible=False,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    def _sync_builder_fields(_: ft.ControlEvent | None = None) -> None:
        act = step_action_dropdown.value or "navigate"
        step_value_field.visible = act != "back"
        step_extra_field.visible = act in ("type", "wait")
        if act == "navigate":
            step_value_field.hint_text = "https://example.com"
        elif act == "wait":
            step_value_field.hint_text = "Min seconds (e.g. 4)"
            step_extra_field.hint_text = "Max seconds (e.g. 9)"
        elif act == "scroll":
            step_value_field.hint_text = "Delta Y (e.g. 520 or -220)"
        elif act == "type":
            step_value_field.hint_text = "CSS selector (e.g. #searchInput)"
            step_extra_field.hint_text = "Text to type + Enter"
        elif act in ("click", "wait_for"):
            step_value_field.hint_text = "CSS selector (e.g. a.result)"
        elif act == "press":
            step_value_field.hint_text = "Key (e.g. PageDown, Enter)"
        _safe_update()

    step_action_dropdown.on_select = _sync_builder_fields

    def _add_builder_step(_: ft.ControlEvent | None = None) -> None:
        steps, err = _parse_draft_steps()
        if err is not None or steps is None:
            _show_error(get_string("warming_script_sync_error"))
            return
        act = step_action_dropdown.value or "navigate"
        val = (step_value_field.value or "").strip()
        extra = (step_extra_field.value or "").strip()
        raw_step: dict[str, Any]
        try:
            if act == "navigate":
                raw_step = {"action": "navigate", "url": val or "https://example.com"}
            elif act == "wait":
                min_s = float(val or "3")
                max_s = float(extra) if extra else None
                raw_step = (
                    {"action": "wait", "min_s": min_s, "max_s": max_s}
                    if max_s is not None
                    else {"action": "wait", "seconds": min_s}
                )
            elif act == "scroll":
                raw_step = {"action": "scroll", "delta_y": float(val or "480"), "humanize": True}
            elif act == "type":
                raw_step = {
                    "action": "type",
                    "selector": val or "input",
                    "text": extra or "news",
                    "submit": True,
                }
            elif act in ("click", "wait_for"):
                raw_step = {"action": act, "selector": val or "a"}
            elif act == "press":
                raw_step = {"action": "press", "key": val or "PageDown"}
            else:
                raw_step = {"action": "back"}
            normalized_step = _normalize_step(len(steps) + 1, raw_step)
        except (ValueError, TypeError) as exc:
            _show_error(get_string("warming_script_invalid_msg", error=str(exc)))
            return
        steps.append(normalized_step)
        step_value_field.value = ""
        step_extra_field.value = ""
        feedback_text.visible = False
        _write_script_steps(steps, notify=True)

    builder_bar = ft.Row(
        spacing=8,
        visible=False,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            step_action_dropdown,
            step_value_field,
            step_extra_field,
            ft.OutlinedButton(
                get_string("warming_add_step_btn"),
                icon=ft.Icons.ADD_ROUNDED,
                height=38,
                style=OUTLINE_STYLE,
                on_click=_add_builder_step,
            ),
        ],
    )

    def _update_scenario_cards_visual(selected_id: str) -> None:
        for sid, card in card_containers.items():
            is_sel = sid == selected_id
            card.bgcolor = COLORS["card_selected_bg"] if is_sel else COLORS["bg"]
            card.border = ft.Border.all(
                1.5 if is_sel else 1,
                COLORS["card_selected_border"] if is_sel else COLORS["card_border"],
            )
            icon_ctrl = card_icons.get(sid)
            if icon_ctrl is not None:
                icon_ctrl.color = COLORS["accent"] if is_sel else COLORS["text_sub"]

    def _sync_preset_mode(
        _: ft.ControlEvent | None = None, *, update_duration: bool = False
    ) -> None:
        pending_replace[0] = None
        replace_confirm_bar.visible = False
        preset = preset_dropdown.value or "youtube_trust"
        is_script = preset == "script"
        is_builtin = preset in WARMING_SCENARIO_PLAYBOOKS
        new_mode = "script" if is_script else "url"
        if new_mode != active_mode[0]:
            drafts[active_mode[0]] = custom_urls_field.value or ""
            active_mode[0] = new_mode
            custom_urls_field.value = drafts[new_mode]
        payload_label.value = get_string(
            "warming_script_steps_label" if is_script else "warming_custom_urls_label"
        )
        custom_urls_field.hint_text = (
            '[{"action": "navigate", "url": "https://news.google.com"}, '
            '{"action": "wait", "min_s": 3, "max_s": 7}]'
            if is_script
            else "https://www.youtube.com\nhttps://docs.google.com"
        )
        custom_urls_field.min_lines = 4 if is_script else 2
        custom_urls_field.max_lines = 6 if is_script else 3
        sample_script_btn.visible = is_script
        customize_btn.visible = is_builtin
        template_pills_row.visible = is_script
        builder_bar.visible = is_script
        if update_duration and preset in WARMING_SCENARIO_META:
            duration_field.value = str(WARMING_SCENARIO_META[preset]["recommended_minutes"])
        _update_scenario_cards_visual(preset)
        _refresh_steps_preview(notify=False)
        _safe_update()

    def _select_scenario(scenario_id: str) -> None:
        preset_dropdown.value = scenario_id
        _sync_preset_mode(None, update_duration=True)

    preset_dropdown.on_select = lambda ev: _sync_preset_mode(ev, update_duration=False)
    custom_urls_field.on_change = lambda _: _refresh_steps_preview(notify=True)

    scenario_strip_controls: list[ft.Control] = []
    for sid, ico, title_key, desc_key in _SCENARIO_CARDS:
        icon_ctrl = ft.Icon(ico, size=18, color=COLORS["text_sub"])
        card_icons[sid] = icon_ctrl
        badge_str = (
            get_string("warming_sites_count_badge", count=len(WARMING_PRESETS[sid]))
            if sid in WARMING_SCENARIO_PLAYBOOKS
            else ("URL" if sid == "custom" else "JSON")
        )
        card = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=9,
            ink=True,
            on_click=lambda _, key=sid: _select_scenario(key),
            content=ft.Column(
                spacing=3,
                tight=True,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            icon_ctrl,
                            ft.Text(
                                badge_str,
                                size=10.5,
                                font_family=FONT_DATA,
                                color=COLORS["text_dim"],
                            ),
                        ],
                    ),
                    ft.Text(
                        get_string(title_key),
                        size=12.5,
                        font_family=FONT_SEMIBOLD,
                        color=COLORS["text_main"],
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        get_string(desc_key),
                        size=11,
                        color=COLORS["text_dim"],
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
            ),
        )
        card_containers[sid] = card
        scenario_strip_controls.append(card)

    def _status_badge(task: WarmingTask) -> ft.Container:
        if task.status == "running":
            fg = COLORS["live"]
            ico = ft.Icons.BOLT_ROUNDED
            label = get_string("stamp_live")
        elif task.status == "completed":
            fg = COLORS["success"]
            ico = ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED
            label = task.last_result or "OK"
        elif task.status == "failed":
            fg = COLORS["error"]
            ico = ft.Icons.ERROR_OUTLINE_ROUNDED
            label = task.last_result or get_string("stamp_failed")
        else:
            fg = COLORS["text_dim"]
            ico = ft.Icons.SCHEDULE_ROUNDED
            label = task.last_result or get_string("stamp_idle")
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            bgcolor=COLORS["bg"],
            border=ft.Border.all(1, COLORS["card_border"]),
            content=ft.Row(
                spacing=5,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ico, size=13, color=fg),
                    ft.Text(label, size=11.5, font_family=FONT_DATA, color=fg),
                ],
            ),
        )

    def _run_task(tid: str) -> bool:
        try:
            started = warming_service.run_task_async(
                tid,
                pm,
                bl,
                log_fn=log,
                on_update=lambda: _rebuild_tasks(notify=True),
            )
        except OSError:
            _rebuild_tasks()
            _show_error(get_string("warming_start_save_error"))
            return False
        if not started:
            _rebuild_tasks()
            _show_error(get_string("warming_still_running"))
            return False
        _rebuild_tasks(notify=True)
        return True

    def _stop_task(tid: str) -> None:
        try:
            warming_service.stop_task(tid)
        except OSError as exc:
            _show_save_error(exc)
            return
        _rebuild_tasks(notify=True)

    def _delete_task(tid: str) -> None:
        try:
            warming_service.delete_task(tid)
        except OSError as exc:
            _show_save_error(exc)
            return
        _rebuild_tasks(notify=True)
        if refresh_fn:
            refresh_fn()

    def _rebuild_tasks(notify: bool = False) -> None:
        tasks = warming_service.list_tasks()
        if not tasks:
            tasks_col.controls = [
                ft.Container(
                    padding=18,
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
                last_str = (
                    time.strftime("%H:%M:%S", time.localtime(t.last_run)) if t.last_run else "-"
                )
                preset_tag = (
                    f"script ({len(t.script_steps)} steps)" if t.preset == "script" else t.preset
                )
                sched_widget = ft.Row(
                    spacing=4,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            ft.Icons.SCHEDULE_ROUNDED
                            if t.schedule_time
                            else ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED,
                            size=14,
                            color=COLORS["accent"] if t.schedule_time else COLORS["text_dim"],
                        ),
                        ft.Text(
                            t.schedule_time or get_string("warming_manual_label"),
                            size=11.5,
                            font_family=FONT_DATA,
                            color=COLORS["accent"] if t.schedule_time else COLORS["text_sub"],
                        ),
                    ],
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
                                        spacing=3,
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
                                            ft.Row(
                                                spacing=10,
                                                wrap=True,
                                                controls=[
                                                    ft.Text(
                                                        preset_tag,
                                                        size=11,
                                                        font_family=FONT_DATA,
                                                        color=COLORS["text_sub"],
                                                    ),
                                                    ft.Text(
                                                        get_string(
                                                            "warming_profiles_short",
                                                            count=len(t.profile_names),
                                                        ),
                                                        size=11,
                                                        font_family=FONT_DATA,
                                                        color=COLORS["text_dim"],
                                                    ),
                                                    ft.Text(
                                                        f"{t.duration_minutes}m",
                                                        size=11,
                                                        font_family=FONT_DATA,
                                                        color=COLORS["text_dim"],
                                                    ),
                                                    ft.Text(
                                                        get_string(
                                                            "warming_last_run_short", time=last_str
                                                        ),
                                                        size=11,
                                                        font_family=FONT_DATA,
                                                        color=COLORS["text_dim"],
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                ),
                                ft.Container(width=92, content=sched_widget),
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
            _safe_update()

    def _show_save_error(exc: OSError) -> None:
        _show_error(get_string("warming_save_error_msg", error=str(exc)))

    def _show_error(err: str) -> None:
        feedback_text.value = err
        feedback_text.color = COLORS["error"]
        feedback_text.visible = True
        log(err)
        _safe_update()

    def _create_from_inputs(run_immediately: bool) -> None:
        name = (task_name_field.value or "").strip()
        preset = preset_dropdown.value or "youtube_trust"
        raw_payload = (custom_urls_field.value or "").strip()
        dur_str = (duration_field.value or "3").strip()
        dur = int(dur_str) if dur_str.isdigit() else 3
        sched = "" if run_immediately else (schedule_field.value or "").strip()

        parsed_steps = None
        raw_urls = raw_payload
        if preset == "script":
            # Check core fields first so empty name/targets still show warming_invalid_msg.
            if name and targets:
                try:
                    parsed_steps = normalize_warming_steps(raw_payload)
                except ValueError as exc:
                    feedback_text.value = get_string("warming_script_invalid_msg", error=str(exc))
                    feedback_text.color = COLORS["error"]
                    feedback_text.visible = True
                    _safe_update()
                    return
            raw_urls = ""
        elif raw_payload and not normalize_warming_urls(raw_payload):
            feedback_text.value = get_string("warming_override_invalid_msg")
            feedback_text.color = COLORS["error"]
            feedback_text.visible = True
            _safe_update()
            return

        try:
            task = warming_service.add_task(
                name=name,
                profile_names=targets,
                preset=preset,
                custom_urls=raw_urls,
                duration_minutes=dur,
                schedule_time=sched,
                repeat_daily=bool(repeat_checkbox.value),
                enabled=True,
                **(
                    {"script_steps": parsed_steps}
                    if preset == "script" and parsed_steps is not None
                    else {}
                ),
            )
        except OSError as exc:
            _show_save_error(exc)
            return
        if task is None:
            feedback_text.value = get_string("warming_invalid_msg")
            feedback_text.color = COLORS["error"]
            feedback_text.visible = True
            _safe_update()
            return

        if run_immediately:
            if not _run_task(task.id):
                return
            msg = get_string("warming_started_msg", name=task.name, count=len(targets))
            feedback_text.value = msg
            feedback_text.color = COLORS["success"]
            feedback_text.visible = True
            log(msg)
            _rebuild_tasks(notify=True)
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

    _sync_preset_mode(None, update_duration=False)
    _rebuild_tasks(notify=False)

    many_targets = len(targets) > 5
    target_pill = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=5),
        border_radius=8,
        bgcolor=COLORS["warn_tint"] if many_targets else COLORS["paper_sunk"],
        border=ft.Border.all(1, COLORS["warning"] if many_targets else COLORS["card_border"]),
        content=ft.Row(
            spacing=6,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(
                    ft.Icons.WARNING_AMBER_ROUNDED if many_targets else ft.Icons.LAYERS_OUTLINED,
                    size=15,
                    color=COLORS["warn_text"] if many_targets else COLORS["text_sub"],
                ),
                ft.Text(
                    get_string("warming_targets_many_warn", count=len(targets))
                    if many_targets
                    else get_string("warming_targets_badge", count=len(targets)),
                    size=11.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["warn_text"] if many_targets else COLORS["text_sub"],
                ),
            ],
        ),
    )

    workspace_panel = ft.Container(
        padding=12,
        border_radius=10,
        bgcolor=COLORS["paper_sunk"],
        border=ft.Border.all(1, COLORS["card_border"]),
        content=ft.Column(
            spacing=8,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[steps_header_title, steps_count_text],
                        ),
                        ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[customize_btn, sample_script_btn],
                        ),
                    ],
                ),
                template_pills_row,
                steps_mode_note,
                replace_confirm_bar,
                steps_preview_box,
                payload_label,
                custom_urls_field,
                builder_bar,
            ],
        ),
    )

    body_container = ft.Container(
        width=940,
        height=640,
        content=ft.ListView(
            spacing=10,
            scroll=ft.ScrollMode.ALWAYS,
            padding=ft.Padding.only(right=12),
            build_controls_on_demand=False,
            controls=[
                preset_dropdown,
                ft.Row(spacing=8, controls=scenario_strip_controls),
                ft.Column(
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
                workspace_panel,
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
                ft.Divider(height=6, color=COLORS["card_border"]),
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
        on_dismiss=lambda _: on_dismiss() if on_dismiss is not None else None,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=18, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=10, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=14),
        title=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Column(
                    spacing=2,
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
                target_pill,
            ],
        ),
        content=body_container,
        actions=[
            ft.TextButton(
                get_string("close_btn"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=lambda event: close(event),
            ),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=940,
        base_height=640,
        width_ratio=0.88,
        height_ratio=0.86,
        min_width=520,
        max_width=1120,
        min_height=440,
        max_height=860,
    )
    close = show_modal_dialog(page, dlg, local_keyboard=True)

    def refresh_tasks() -> None:
        if dlg.open:
            _rebuild_tasks()

    return refresh_tasks
