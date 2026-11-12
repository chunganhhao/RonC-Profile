"""Small editors: draft locally; commit only the chosen field on confirmation."""

from collections.abc import Callable

import flet as ft

from ...core.strings import get_string
from ...models.profile import DEFAULT_GROUPS, Profile
from ...utils.validation import validate_profile_name, validate_proxy_format
from ..theme.colors import COLORS, FONT_SEMIBOLD
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS
from .responsive import bind_responsive_dialog

FIELD_LABELS = {
    "display_order": "col_id",
    "name": "col_profile",
    "tags": "col_group",
    "proxy": "col_proxy",
    "recovery_email": "recovery_email_label",
    "notes": "notes_label",
}


def open_profile_field_dialog(
    page: ft.Page,
    profile: Profile,
    field: str,
    on_save: Callable[[object], str | None],
    *,
    row_index: int = 1,
    known_tags: list[str] | None = None,
) -> None:
    if field not in FIELD_LABELS:
        raise ValueError("Unsupported profile field")
    label = get_string(FIELD_LABELS[field])
    tags = list(profile.tags or ([profile.group] if profile.group else []))
    if field == "display_order":
        value: object = profile.display_order or row_index
    elif field == "recovery_email":
        value = profile.effective_recovery_email
    elif field == "notes":
        value = profile.effective_notes
    else:
        value = getattr(profile, field)
    entry = ft.TextField(
        label=label,
        value="" if field == "tags" else str(value or ""),
        autofocus=True,
        multiline=field == "notes",
        min_lines=3 if field == "notes" else 1,
        max_lines=5 if field == "notes" else 1,
        **DLG_FIELD_KWARGS,
    )
    error = ft.Text("", color=COLORS["error"], size=12, visible=False)
    chips = ft.Row(wrap=True, spacing=6, run_spacing=6)

    def report(message: str) -> None:
        error.value, error.visible = message, True
        page.update()

    def remove(tag: str) -> None:
        tags.remove(tag)
        render_tags()
        page.update()

    def render_tags() -> None:
        chips.controls = [
            ft.Container(
                bgcolor=COLORS["accent_soft"],
                border_radius=6,
                padding=ft.Padding.only(left=10, right=2),
                content=ft.Row(
                    tight=True,
                    spacing=4,
                    controls=[
                        ft.Container(
                            max_width=350,
                            content=ft.Text(
                                tag,
                                color=COLORS["text_main"],
                                size=13,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                tooltip=tag,
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE,
                            icon_size=14,
                            width=28,
                            height=32,
                            tooltip=get_string("remove_tag", tag=tag),
                            on_click=lambda _, t=tag: remove(t),
                        ),
                    ],
                ),
            )
            for tag in tags
        ]

    def add(tag: str) -> bool:
        tag = tag.strip()
        if not tag:
            return True
        if len(tag) > 64 or (tag not in tags and len(tags) >= 20):
            report(get_string("tag_limit"))
            return False
        if tag not in tags:
            tags.append(tag)
        entry.value = ""
        render_tags()
        page.update()
        return True

    def submit(_: object = None) -> None:
        draft: object = (entry.value or "").strip()
        if field == "tags":
            if not add(str(draft)):
                return
            draft = list(tags)
        elif field == "display_order":
            if (
                not str(draft).isascii()
                or not str(draft).isdigit()
                or not 1 <= int(str(draft)) <= 1_000_000
            ):
                report(get_string("order_invalid"))
                return
            draft = int(str(draft))
        elif field in ("name", "proxy"):
            valid, message = (validate_profile_name if field == "name" else validate_proxy_format)(
                str(draft)
            )
            if not valid:
                report(message)
                return
        message = on_save(draft)
        if message:
            report(message)
            return
        page.pop_dialog()

    entry.on_submit = (lambda _: add(entry.value or "")) if field == "tags" else submit
    controls: list[ft.Control] = []
    if field == "tags":
        render_tags()
        controls.extend(
            [
                chips,
                ft.Row(
                    [
                        ft.Container(entry, expand=True),
                        ft.IconButton(
                            icon=ft.Icons.ADD,
                            tooltip=get_string("add_tag"),
                            on_click=lambda _: add(entry.value or ""),
                        ),
                    ]
                ),
                ft.Row(
                    wrap=True,
                    controls=[
                        ft.TextButton(
                            tag,
                            on_click=lambda _, t=tag: add(t),
                        )
                        for tag in dict.fromkeys([*(known_tags or []), *DEFAULT_GROUPS])
                    ],
                ),
            ]
        )
    else:
        controls.append(entry)
    hint = {
        "name": "field_name_hint",
        "proxy": "field_proxy_hint",
        "display_order": "field_order_hint",
        "tags": "field_tags_hint",
        "recovery_email": "recovery_email_hint",
        "notes": "field_notes_hint",
    }[field]
    controls.extend([ft.Text(get_string(hint), size=12, color=COLORS["text_sub"]), error])
    base_h = 300 if field == "tags" else 230 if field == "notes" else 180
    body_container = ft.Container(
        width=480,
        height=base_h,
        content=ft.Column(
            controls,
            tight=True,
            spacing=14,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title=ft.Text(get_string("edit_field", field=label), size=19, font_family=FONT_SEMIBOLD),
        content=body_container,
        actions=[
            ft.TextButton(get_string("cancel"), on_click=lambda _: page.pop_dialog()),
            ft.Button(get_string("save_changes"), style=ACCENT_STYLE, on_click=submit),
        ],
    )
    bind_responsive_dialog(
        page,
        dlg,
        body_container,
        base_width=480,
        base_height=base_h,
        width_ratio=0.58,
        height_ratio=0.50,
        min_width=360,
        max_width=640,
        min_height=base_h,
        max_height=420,
    )
    page.show_dialog(dlg)
