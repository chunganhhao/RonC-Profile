from collections.abc import Callable

import flet as ft

from ...core.strings import get_string, localize_launch_error
from ...models.profile import Profile, extract_backup_code_tokens
from ...utils.proxy_parser import mask_proxy
from ..theme.colors import COLORS, FONT_DATA, FONT_SEMIBOLD
from ..theme.styles import DANGER_ICON_STYLE, ICON_STYLE
from .launch_button import build_launch_button

_OS_ICONS = {
    "macos": ft.Icons.APPLE,
    "windows": ft.Icons.WINDOW_ROUNDED,
    "linux": ft.Icons.TERMINAL_ROUNDED,
}

# Tag pill color pairs: (bg_color, text_color)
_TAG_PALETTES = (
    ("#86EFAC", "#064E3B"),  # Mint green
    ("#F9A8D4", "#831843"),  # Soft pink
    ("#93C5FD", "#1E3A8A"),  # Sky blue
    ("#FDE68A", "#78350F"),  # Amber gold
    ("#C4B5FD", "#4C1D95"),  # Lavender violet
    ("#67E8F9", "#164E63"),  # Cyan
)

# Table grid: (expand, fixed_width)
COLUMNS = {
    "select": (None, 32),
    "id": (None, 64),
    "name": (9, None),
    "group": (6, None),
    "proxy": (6, None),
    "recovery": (7, None),
    "notes": (10, None),
    "status": (None, 92),
    "actions": (None, 276),
}
ROW_HEIGHT = 72
TABLE_BREAKPOINT = 1360


def _responsive_row(
    container: ft.Container, *, header: bool = False, layout_width: float | None = None
) -> ft.Container:
    """Reflow the same cells, keeping their callbacks and selection on resize."""
    ledger = container.content
    cells = dict(zip(COLUMNS, ledger.controls, strict=True))
    layout = None

    def resize(width: float, notify: bool = True) -> None:
        nonlocal layout
        narrow = width < TABLE_BREAKPOINT
        # ResponsiveRow's default breakpoints follow the page, not this sheet.
        # Account for the sidebar and workspace padding using the measured width.
        mode = (narrow, width >= 820, width >= 1080)
        if mode == layout:
            return
        layout = mode
        for key, cell in cells.items():
            cell.expand, cell.width = COLUMNS[key]
        if not narrow:
            container.content = ledger
        elif header:
            container.content = ft.Row(
                spacing=10,
                controls=[cells["select"], cells["name"]],
            )
        else:
            details = []
            for key, label in (
                ("group", "col_group"),
                ("proxy", "col_proxy"),
                ("recovery", "col_recovery"),
                ("notes", "col_notes"),
            ):
                cell = cells[key]
                cell.expand, cell.width = None, None
                details.append(
                    ft.Column(
                        col=3 if mode[2] else 6,
                        spacing=4,
                        controls=[
                            ft.Text(get_string(label), size=11, color=COLORS["text_dim"]),
                            cell,
                        ],
                    )
                )
            container.content = ft.Column(
                spacing=12,
                controls=[
                    ft.ResponsiveRow(
                        run_spacing=8,
                        controls=[
                            ft.Row(
                                col=7 if mode[1] else 12,
                                spacing=8,
                                controls=[cells[k] for k in ("select", "id", "name", "status")],
                            ),
                            ft.Row(
                                col=5 if mode[1] else 12,
                                alignment=ft.MainAxisAlignment.END,
                                controls=[cells["actions"]],
                            ),
                        ],
                    ),
                    ft.ResponsiveRow(controls=details, run_spacing=12),
                ],
            )
        if notify:
            container.update()

    container.on_size_change = lambda event: resize(event.width)
    if layout_width is not None:
        resize(layout_width, notify=False)
    return container


def _cell(key: str, content: ft.Control | None) -> ft.Container:
    expand, width = COLUMNS[key]
    return ft.Container(content=content, expand=expand, width=width)


def _tag_colors(tag: str) -> tuple[str, str]:
    low = tag.lower()
    if "google" in low or "gmail" in low or "gemini" in low:
        return ("#F9A8D4", "#831843")
    if "facebook" in low or "fb" in low or "agy" in low:
        return ("#86EFAC", "#064E3B")
    if "tiktok" in low or "x" in low or "twitter" in low:
        return ("#93C5FD", "#1E3A8A")
    if "crypto" in low or "airdrop" in low:
        return ("#FDE68A", "#78350F")
    idx = sum(ord(ch) for ch in tag) % len(_TAG_PALETTES)
    return _TAG_PALETTES[idx]


def build_header_row(
    all_selected: bool,
    any_selected: bool,
    on_select_all: Callable[[bool], None],
    layout_width: float | None = None,
) -> ft.Container:
    def label(key: str) -> ft.Text:
        return ft.Text(
            get_string(key), size=12, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"]
        )

    box = ft.Checkbox(
        value=all_selected,
        tooltip=get_string("deselect_page") if all_selected else get_string("select_page"),
        on_change=lambda e: on_select_all(not all_selected),
    )
    container = ft.Container(
        height=40,
        padding=ft.Padding.symmetric(horizontal=14),
        bgcolor=COLORS["paper_sunk"],
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                _cell("select", box),
                _cell("id", label("col_id")),
                _cell("name", label("col_profile")),
                _cell("group", label("col_group")),
                _cell("proxy", label("col_proxy")),
                _cell("recovery", label("col_recovery")),
                _cell("notes", label("col_notes")),
                _cell("status", label("col_status")),
                _cell("actions", label("col_actions")),
            ],
        ),
    )
    return _responsive_row(container, header=True, layout_width=layout_width)


def build_profile_card(
    profile: Profile,
    is_loading: bool,
    is_running: bool,
    on_launch: Callable[[str], None],
    on_edit: Callable[[str], None],
    on_delete: Callable[[str], None],
    is_selected: bool = False,
    on_select: Callable[[str], None] | None = None,
    on_cookies: Callable[[str], None] | None = None,
    on_copy_pass: Callable[[str], None] | None = None,
    on_copy_2fa: Callable[[str], None] | None = None,
    device: str = "",
    since: str = "",
    failed: str = "",
    row_index: int = 1,
    on_copy_user: Callable[[str], None] | None = None,
    on_copy_recovery: Callable[[str], None] | None = None,
    on_edit_field: Callable[[str, str, int], None] | None = None,
    on_backup_codes: Callable[[str, int], None] | None = None,
    active_feedback: set[str] | None = None,
    layout_width: float | None = None,
    is_stopping: bool = False,
) -> ft.Container:
    """One profile as a table row."""
    fb = active_feedback or set()
    is_busy = is_loading or is_stopping
    if is_running:
        bg = COLORS["card_hover"]
    elif is_selected:
        bg = COLORS["card_selected_bg"]
    else:
        bg = COLORS["card_bg"]

    def editable(field: str, content: ft.Control) -> ft.Control:
        if not on_edit_field:
            return content
        is_done = f"edit_{field}" in fb
        return ft.Row(
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(content, expand=True),
                ft.IconButton(
                    icon=ft.Icons.CHECK_ROUNDED if is_done else ft.Icons.EDIT_OUTLINED,
                    icon_size=14,
                    width=28,
                    height=36,
                    icon_color=COLORS["live"] if is_done else COLORS["text_sub"],
                    style=_feedback_icon_style() if is_done else ICON_STYLE,
                    tooltip=get_string("saved_badge")
                    if is_done
                    else get_string(
                        "edit_field",
                        field=get_string(
                            {
                                "display_order": "col_id",
                                "name": "col_profile",
                                "tags": "col_group",
                                "proxy": "col_proxy",
                                "notes": "notes_label",
                            }[field]
                        ),
                    ),
                    disabled=field == "name" and (is_running or is_busy),
                    on_click=lambda _, f=field: on_edit_field(profile.name, f, row_index),
                ),
            ],
        )

    backup_handler = on_backup_codes or (
        (lambda n, idx=row_index: on_edit_field(n, "backup_codes", idx))
        if on_edit_field is not None
        else None
    )

    container = ft.Container(
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        bgcolor=bg,
        border=ft.Border.only(bottom=ft.BorderSide(1, COLORS["card_border"])),
        content=ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                _cell(
                    "select",
                    ft.Checkbox(
                        value=is_selected,
                        tooltip=get_string("tooltip_select"),
                        on_change=lambda _, n=profile.name: on_select(n) if on_select else None,
                    ),
                ),
                _cell(
                    "id",
                    editable(
                        "display_order",
                        ft.Text(
                            str(profile.display_order or row_index),
                            size=12.5,
                            font_family=FONT_DATA,
                            color=COLORS["text_sub"],
                        ),
                    ),
                ),
                _cell(
                    "name",
                    editable(
                        "name",
                        _profile_identity(
                            profile,
                            on_copy_user,
                            copied_user="copy_user" in fb,
                        ),
                    ),
                ),
                _cell("group", editable("tags", _group_and_engine_badges(profile))),
                _cell("proxy", editable("proxy", _proxy_badge(profile))),
                _cell(
                    "recovery",
                    _recovery_cell(
                        profile,
                        row_index,
                        on_copy_recovery=on_copy_recovery,
                        on_edit_field=on_edit_field,
                        active_feedback=fb,
                    ),
                ),
                _cell("notes", editable("notes", _notes_and_device(profile, device))),
                _cell(
                    "status",
                    ft.Row(
                        controls=[
                            _stamp(is_loading, is_running, since, failed, is_stopping=is_stopping)
                        ]
                    ),
                ),
                _cell(
                    "actions",
                    ft.Row(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.END,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            build_launch_button(profile.name, is_busy, is_running, on_launch),
                            ft.Container(width=4),
                            *_action_buttons(
                                profile,
                                is_running,
                                on_edit,
                                on_delete,
                                on_cookies,
                                on_copy_pass,
                                on_copy_2fa,
                                on_backup_codes=backup_handler,
                                row_index=row_index,
                                active_feedback=fb,
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )
    return _responsive_row(container, layout_width=layout_width)


def _feedback_icon_style() -> ft.ButtonStyle:
    return ft.ButtonStyle(
        shape=ft.RoundedRectangleBorder(radius=6),
        bgcolor={
            ft.ControlState.DEFAULT: COLORS["live_tint"],
            ft.ControlState.HOVERED: COLORS["live_tint"],
        },
        padding=0,
    )


def _profile_identity(
    profile: Profile,
    on_copy_user: Callable[[str], None] | None = None,
    *,
    copied_user: bool = False,
) -> ft.Control:
    os_icon = _OS_ICONS.get(profile.os_type, ft.Icons.COMPUTER)
    top_line = ft.Row(
        spacing=7,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Icon(
                os_icon,
                size=16,
                color=COLORS["text_main"],
                tooltip=profile.os_type.upper(),
            ),
            ft.Container(
                expand=True,
                content=ft.Text(
                    profile.name,
                    size=13.5,
                    font_family=FONT_SEMIBOLD,
                    color=COLORS["text_main"],
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=profile.name,
                ),
            ),
        ],
    )
    if not profile.account_uid:
        return top_line
    uid_text = _minor(profile.account_uid, data=True)
    if copied_user:
        uid_text.color = COLORS["live"]
    return _stack(
        top_line,
        ft.Row(
            spacing=2,
            controls=[
                ft.Container(uid_text, expand=True),
                *(
                    [
                        ft.IconButton(
                            icon=ft.Icons.CHECK_ROUNDED
                            if copied_user
                            else ft.Icons.CONTENT_COPY_OUTLINED,
                            icon_size=14,
                            width=28,
                            height=28,
                            tooltip=get_string("copied_badge")
                            if copied_user
                            else get_string("copy_user"),
                            style=_feedback_icon_style() if copied_user else ICON_STYLE,
                            icon_color=COLORS["live"] if copied_user else COLORS["text_sub"],
                            on_click=lambda _: on_copy_user(profile.name),
                        )
                    ]
                    if on_copy_user
                    else []
                ),
            ],
        ),
    )


def _group_and_engine_badges(profile: Profile) -> ft.Control:
    tag_pills: list[ft.Control] = []
    for tag in profile.effective_tags:
        bg_col, fg_col = _tag_colors(tag)
        tag_pills.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                border_radius=5,
                bgcolor=bg_col,
                tooltip=tag,
                content=ft.Text(
                    tag,
                    size=11,
                    font_family=FONT_SEMIBOLD,
                    color=fg_col,
                    no_wrap=True,
                ),
            )
        )
    engine_label = "Chrome" if profile.engine == "chrome" else "Camoufox"
    engine_pill = ft.Container(
        padding=ft.Padding.symmetric(horizontal=6, vertical=1.5),
        border_radius=4,
        bgcolor=COLORS["paper_sunk"],
        border=ft.Border.all(1, COLORS["card_border"]),
        content=ft.Text(
            engine_label,
            size=10.5,
            font_family=FONT_DATA,
            color=COLORS["text_sub"],
            no_wrap=True,
        ),
    )
    return ft.Column(
        spacing=4,
        alignment=ft.MainAxisAlignment.CENTER,
        horizontal_alignment=ft.CrossAxisAlignment.START,
        controls=[
            ft.Row(
                wrap=True,
                spacing=4,
                run_spacing=4,
                controls=tag_pills,
            )
            if tag_pills
            else ft.Text("—", color=COLORS["text_dim"]),
            engine_pill,
        ],
    )


def _proxy_badge(profile: Profile) -> ft.Control:
    server = mask_proxy(profile.proxy)
    if not server:
        return ft.Text(get_string("direct_short"), size=12.5, color=COLORS["text_dim"])
    scheme, _, rest = server.partition("://")
    return ft.Row(
        spacing=6,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                expand=True,
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                border_radius=6,
                bgcolor=COLORS["proxy_pill_bg"],
                border=ft.Border.all(1, COLORS["proxy_pill_border"]),
                tooltip=f"{scheme.upper()}://{rest}",
                content=ft.Row(
                    spacing=5,
                    tight=False,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            rest,
                            expand=True,
                            size=11.5,
                            font_family=FONT_DATA,
                            color=COLORS["proxy_pill_fg"],
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Icon(
                            ft.Icons.LOCK_OUTLINE,
                            size=12,
                            color=COLORS["proxy_pill_fg"],
                        ),
                    ],
                ),
            )
        ],
    )


def _recovery_cell(
    profile: Profile,
    row_index: int,
    on_copy_recovery: Callable[[str], None] | None = None,
    on_edit_field: Callable[[str, str, int], None] | None = None,
    active_feedback: set[str] | None = None,
) -> ft.Control:
    fb = active_feedback or set()
    rec_mail = profile.effective_recovery_email
    copied_rec = "copy_recovery" in fb
    edited_rec = "edit_recovery_email" in fb

    if rec_mail:
        badge: ft.Control = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=6,
            bgcolor=COLORS["paper_sunk"],
            border=ft.Border.all(1, COLORS["card_border"]),
            tooltip=rec_mail,
            content=ft.Row(
                spacing=5,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        ft.Icons.MARK_EMAIL_READ_OUTLINED,
                        size=13,
                        color=COLORS["live"] if copied_rec else COLORS["accent"],
                    ),
                    ft.Text(
                        rec_mail,
                        expand=True,
                        size=11.5,
                        font_family=FONT_DATA,
                        color=COLORS["live"] if copied_rec else COLORS["text_main"],
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
            ),
        )
    else:
        badge = ft.Text("—", size=12, color=COLORS["text_dim"], expand=True)

    actions: list[ft.Control] = []
    if rec_mail and on_copy_recovery:
        actions.append(
            ft.IconButton(
                icon=ft.Icons.CHECK_ROUNDED if copied_rec else ft.Icons.CONTENT_COPY_OUTLINED,
                icon_size=13,
                width=24,
                height=24,
                tooltip=get_string("copied_badge") if copied_rec else get_string("copy_recovery"),
                style=_feedback_icon_style() if copied_rec else ICON_STYLE,
                icon_color=COLORS["live"] if copied_rec else COLORS["text_sub"],
                on_click=lambda _: on_copy_recovery(profile.name),
            )
        )
    if on_edit_field:
        actions.append(
            ft.IconButton(
                icon=ft.Icons.CHECK_ROUNDED if edited_rec else ft.Icons.EDIT_OUTLINED,
                icon_size=13,
                width=24,
                height=24,
                tooltip=get_string("saved_badge")
                if edited_rec
                else get_string("edit_field", field=get_string("recovery_email_label")),
                style=_feedback_icon_style() if edited_rec else ICON_STYLE,
                icon_color=COLORS["live"] if edited_rec else COLORS["text_sub"],
                on_click=lambda _: on_edit_field(profile.name, "recovery_email", row_index),
            )
        )
    return ft.Row(
        spacing=2,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[badge, *actions],
    )


def _notes_and_device(profile: Profile, device: str) -> ft.Control:
    geo_part = profile.timezone or get_string("follows_ip")
    hw_line = f"{device} / {geo_part}" if device else geo_part
    clean_notes = profile.effective_notes
    if clean_notes:
        one_line_notes = " · ".join(
            part.strip() for part in clean_notes.splitlines() if part.strip()
        )
        return _stack(
            ft.Row(
                key=f"notes_scroll_{profile.name}",
                scroll=ft.ScrollMode.AUTO,
                spacing=0,
                controls=[
                    ft.Text(
                        one_line_notes,
                        size=12.5,
                        color=COLORS["text_main"],
                        no_wrap=True,
                        selectable=True,
                        tooltip=clean_notes,
                    )
                ],
            ),
            _minor(hw_line, data=True),
        )
    return _stack(
        _minor(device or get_string("no_fingerprint_short"), data=bool(device)),
        _minor(geo_part, data=bool(profile.timezone)),
    )


def _minor(text: str, data: bool = False) -> ft.Text:
    return ft.Text(
        text,
        size=11,
        font_family=FONT_DATA if data else None,
        color=COLORS["text_dim"],
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )


def _stack(*lines: ft.Control) -> ft.Column:
    return ft.Column(spacing=1, alignment=ft.MainAxisAlignment.CENTER, controls=list(lines))


def _stamp(
    is_loading: bool, is_running: bool, since: str, failed: str = "", *, is_stopping: bool = False
) -> ft.Control:
    if failed and not (is_loading or is_stopping or is_running):
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=5,
            border=ft.Border.all(1, COLORS["error"]),
            tooltip=localize_launch_error(failed),
            content=ft.Text(
                get_string("stamp_failed"),
                size=11,
                font_family=FONT_SEMIBOLD,
                color=COLORS["error"],
            ),
        )
    if is_loading or is_stopping:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            border_radius=5,
            border=ft.Border.all(1, COLORS["text_sub"]),
            content=ft.Text(
                get_string("stamp_stopping" if is_stopping else "stamp_starting"),
                size=11,
                font_family=FONT_DATA,
                color=COLORS["text_sub"],
            ),
        )
    if not is_running:
        return ft.Text(get_string("stamp_idle"), size=12, color=COLORS["text_dim"])

    label = get_string("stamp_live")
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
        border_radius=5,
        bgcolor=COLORS["live_tint"],
        content=ft.Column(
            spacing=0,
            tight=True,
            controls=[
                ft.Text(label, size=11, font_family=FONT_SEMIBOLD, color=COLORS["live_ink"]),
                *([ft.Text(since, size=10, color=COLORS["text_sub"])] if since else []),
            ],
        ),
    )


def _action_buttons(
    profile: Profile,
    is_running: bool,
    on_edit: Callable[[str], None],
    on_delete: Callable[[str], None],
    on_cookies: Callable[[str], None] | None,
    on_copy_pass: Callable[[str], None] | None,
    on_copy_2fa: Callable[[str], None] | None,
    on_backup_codes: Callable[[str, int], None] | None = None,
    row_index: int = 1,
    active_feedback: set[str] | None = None,
) -> list[ft.IconButton]:
    fb = active_feedback or set()
    name = profile.name
    buttons: list[ft.IconButton] = []
    if profile.account_password and on_copy_pass:
        pass_done = "copy_pass" in fb
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.CHECK_ROUNDED if pass_done else ft.Icons.KEY_OUTLINED,
                width=30,
                height=36,
                icon_size=17,
                icon_color=COLORS["live"] if pass_done else COLORS["text_sub"],
                tooltip=get_string("copied_badge")
                if pass_done
                else get_string("tooltip_copy_pass", uid=profile.account_uid or name),
                style=_feedback_icon_style() if pass_done else ICON_STYLE,
                on_click=lambda _, n=name: on_copy_pass(n),
            )
        )
    if profile.two_fa_secret and on_copy_2fa:
        tfa_done = "copy_2fa" in fb
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.CHECK_ROUNDED if tfa_done else ft.Icons.SECURITY_OUTLINED,
                width=30,
                height=36,
                icon_size=17,
                icon_color=COLORS["live"] if tfa_done else COLORS["accent"],
                tooltip=get_string("copied_badge") if tfa_done else get_string("tooltip_copy_2fa"),
                style=_feedback_icon_style() if tfa_done else ICON_STYLE,
                on_click=lambda _, n=name: on_copy_2fa(n),
            )
        )
    if on_backup_codes is not None:
        bkp_done = "edit_backup_codes" in fb or "copy_backup_codes" in fb
        code_count = len(extract_backup_code_tokens(profile.backup_codes))
        has_bkp = bool((profile.backup_codes or "").strip())
        bkp_tooltip = (
            get_string("saved_badge")
            if bkp_done
            else (
                get_string("tooltip_backup_codes", count=code_count or 1)
                if has_bkp
                else get_string("tooltip_add_backup_codes")
            )
        )
        buttons.append(
            ft.IconButton(
                key=f"backup_codes_btn_{name}",
                icon=ft.Icons.CHECK_ROUNDED if bkp_done else ft.Icons.PIN_OUTLINED,
                width=30,
                height=36,
                icon_size=17,
                icon_color=COLORS["live"]
                if bkp_done
                else (COLORS["accent"] if has_bkp else COLORS["text_sub"]),
                tooltip=bkp_tooltip,
                style=_feedback_icon_style() if bkp_done else ICON_STYLE,
                on_click=lambda _, n=name, idx=row_index: on_backup_codes(n, idx),
            )
        )
    if on_cookies:
        buttons.append(
            ft.IconButton(
                icon=ft.Icons.COOKIE_OUTLINED,
                width=30,
                height=36,
                icon_size=18,
                icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
                tooltip=get_string("tooltip_cookies"),
                disabled=is_running,
                style=ICON_STYLE,
                on_click=lambda _, n=name: on_cookies(n),
            ),
        )
    edit_done = "edit_profile" in fb
    return [
        *buttons,
        ft.IconButton(
            icon=ft.Icons.CHECK_ROUNDED if edit_done else ft.Icons.EDIT_OUTLINED,
            icon_size=18,
            width=30,
            height=36,
            icon_color=COLORS["live"] if edit_done else COLORS["text_sub"],
            tooltip=get_string("saved_badge") if edit_done else get_string("tooltip_edit"),
            style=_feedback_icon_style() if edit_done else ICON_STYLE,
            on_click=lambda _, n=name: on_edit(n),
        ),
        ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=18,
            width=30,
            height=36,
            icon_color=COLORS["rule_strong"] if is_running else COLORS["text_sub"],
            tooltip=get_string("tooltip_delete"),
            disabled=is_running,
            style=DANGER_ICON_STYLE,
            on_click=lambda _, n=name: on_delete(n),
        ),
    ]
