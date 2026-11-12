import asyncio

import flet as ft

from ...core.strings import get_string
from ...services.browser import cores
from ..theme.colors import COLORS, FONT_BOLD
from .responsive import bind_responsive_dialog


def _fmt_bytes(num: int | None) -> str:
    if num is None:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "—"


def open_core_manager(page: ft.Page, pm=None, bl=None) -> None:
    status = ft.Text(selectable=True, color=COLORS["text_main"])
    storage_line = ft.Text(selectable=True, size=12, color=COLORS["text_sub"])
    result = ft.Text("", selectable=True, color=COLORS["text_sub"])
    choices = ft.Dropdown(label=get_string("core_candidate"), expand=True)
    profiles = ft.Dropdown(label=get_string("core_profile"), expand=True)
    confirm = ft.Checkbox(label=get_string("core_confirm"), value=False)
    backup = ft.Checkbox(label=get_string("core_backup_confirm"), value=False)
    apply_ack = ft.Checkbox(label=get_string("core_apply_confirm"), value=False)
    prune_ack = ft.Checkbox(label=get_string("core_prune_confirm"), value=False)
    download = ft.Button(get_string("core_stage"), disabled=True)
    apply = ft.OutlinedButton(get_string("core_apply"), disabled=True)
    prune_btn = ft.OutlinedButton(get_string("core_prune_btn"), disabled=True)
    refresh = ft.OutlinedButton(get_string("core_refresh"))
    candidates = {}
    profile_engines = {}
    working = False
    activation_allowed = False

    def changed(_=None):
        download.disabled = working or not (confirm.value and choices.value)
        item = candidates.get(choices.value, {})
        apply.disabled = working or not (
            activation_allowed
            and pm
            and bl
            and item.get("staged")
            and item.get("can_stage")
            and profile_engines.get(profiles.value) == item.get("engine")
            and backup.value
            and apply_ack.value
        )
        prune_btn.disabled = working or not (activation_allowed and pm and bl and prune_ack.value)
        page.update()

    def selection(_):
        backup.value = False
        apply_ack.value = False
        confirm.value = False
        prune_ack.value = False
        changed()

    def render():
        nonlocal activation_allowed
        data = cores.inventory()
        activation_allowed = data["activation_allowed"]
        status.value = "\n".join(
            f"{c['engine']}: {c.get('selected_version') or '—'} · "
            f"{get_string('core_installed') if c['installed'] else get_string('core_missing')}"
            for c in data["cores"]
        )
        storage_line.value = get_string(
            "core_storage_info",
            path=data.get("storage_home", "—"),
            free=_fmt_bytes(data.get("free_bytes")),
        )
        candidates.clear()
        for c in data["candidates"]:
            candidates[f"{c['engine']}:{c['version']}"] = c
        choices.options = [
            ft.dropdown.Option(
                key,
                f"{c['engine']} {c['version']} · {get_string('core_staged') if c['staged'] else get_string('core_not_staged')}",
                disabled=not c["can_stage"],
            )
            for key, c in candidates.items()
        ]
        profile_engines.clear()
        if pm and bl:
            for p in pm.list_profiles():
                if not bl.is_running(p.name):
                    profile_engines[p.name] = p.engine
        profiles.options = [ft.dropdown.Option(name, name) for name in profile_engines]
        result.value = "\n".join(
            c["blocked_reason"] for c in data["candidates"] if c["blocked_reason"]
        )
        if data.get("activation_blocked_reason"):
            result.value += "\n" + data["activation_blocked_reason"]

    async def operation(mode: str):
        nonlocal working
        working = True
        for control in (refresh, choices, confirm, profiles, backup, apply_ack, prune_ack):
            control.disabled = True
        changed()
        result.value = get_string(
            "core_pruning"
            if mode == "prune"
            else ("core_applying" if mode == "apply" else "core_downloading")
        )
        page.update()
        try:
            if mode == "prune":
                response = await asyncio.to_thread(
                    cores.prune_unused,
                    pm,
                    bl,
                    confirm_prune=bool(prune_ack.value),
                )
                message = get_string(
                    "core_pruned",
                    cores=len(response.get("removed_cores", [])),
                    backups=response.get("removed_backups", 0),
                    freed=_fmt_bytes(response.get("freed_bytes", 0)),
                )
            else:
                engine, version = choices.value.split(":", 1)
                if mode == "apply":
                    response = await asyncio.to_thread(
                        cores.backup_and_apply,
                        pm,
                        bl,
                        profiles.value,
                        engine,
                        version,
                        confirm_backup=bool(backup.value),
                        confirm_apply=bool(apply_ack.value),
                    )
                    message = get_string(
                        "core_applied", path=response.get("backup_path", response.get("reason", ""))
                    )
                else:
                    await asyncio.to_thread(
                        cores.stage, engine, version, confirm_download=bool(confirm.value)
                    )
                    message = get_string("core_stage_done")
            render()
            result.value = message
        except Exception as exc:
            result.value = str(exc)
        finally:
            working = False
            for control in (refresh, choices, confirm, profiles, backup, apply_ack, prune_ack):
                control.disabled = False
            backup.value = apply_ack.value = confirm.value = prune_ack.value = False
            changed()

    async def stage(_):
        await operation("stage")

    async def activate(_):
        await operation("apply")

    async def prune_action(_):
        await operation("prune")

    def reload(_):
        try:
            render()
            selection(None)
        except Exception as exc:
            result.value = str(exc)
        page.update()

    for control in (confirm, backup, apply_ack, prune_ack):
        control.on_change = changed
    choices.on_select = profiles.on_select = selection
    download.on_click = stage
    apply.on_click = activate
    prune_btn.on_click = prune_action
    refresh.on_click = reload
    body_container = ft.Container(
        width=660,
        content=ft.Column(
            tight=True,
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(get_string("core_safety")),
                status,
                storage_line,
                choices,
                confirm,
                ft.Row([refresh, download], spacing=8, run_spacing=8, wrap=True),
                profiles,
                backup,
                apply_ack,
                apply,
                prune_ack,
                prune_btn,
                ft.Text(get_string("core_apply_blocked")),
                result,
            ],
        ),
    )
    dialog = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Text(
            get_string("core_manager"), size=19, font_family=FONT_BOLD, color=COLORS["text_main"]
        ),
        content=body_container,
        actions=[ft.TextButton(get_string("close_btn"), on_click=lambda _: page.pop_dialog())],
    )
    bind_responsive_dialog(
        page,
        dialog,
        body_container,
        base_width=660,
        width_ratio=0.78,
        min_width=420,
        max_width=860,
    )
    render()
    page.show_dialog(dialog)
