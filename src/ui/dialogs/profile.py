import contextlib
import inspect
import os
import threading
from collections.abc import Callable
from typing import Any

import flet as ft

from chrome_engine.persona import macos_cpu_counts_for_seed
from chrome_engine.release import host_os

from ...core.config import DATA_DIR
from ...core.strings import get_string
from ...interfaces.protocols import IProxyService
from ...models.profile import (
    DEFAULT_GROUPS,
    Profile,
    normalize_backup_codes,
    split_recovery_and_notes,
)
from ...services.browser import fingerprint as fp_service
from ...services.browser.extensions import ExtensionService
from ...services.proxy.geo_check import GeoCheckResult, check_geo, compare
from ...utils.totp import clean_totp_secret, generate_totp, parse_account_line
from ...utils.validation import (
    normalize_start_url,
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_start_url,
    validate_timezone,
)
from ..theme.colors import COLORS, FONT_BOLD, FONT_DATA, FONT_SEMIBOLD
from ..theme.page import build_engine_dropdown, build_os_dropdown
from ..theme.styles import ACCENT_STYLE, DLG_FIELD_KWARGS, OUTLINE_STYLE
from .modal import show_profile_editor
from .responsive import bind_responsive_dialog

_DATA = ft.TextStyle(font_family=FONT_DATA, size=12.5)
_active_totp_lock = threading.Lock()
_active_totp_stop: Callable[[], None] | None = None


class _LiveTotpText(ft.Text):
    """ft.Text that refreshes its TOTP countdown on clock ticks and when read while open."""

    _refresh_hook: Callable[[], None] | None = None
    _in_hook: bool = False

    def __getattribute__(self, name: str) -> Any:
        if name == "value" and not object.__getattribute__(self, "_in_hook"):
            hook = object.__getattribute__(self, "_refresh_hook")
            if hook is not None:
                object.__setattr__(self, "_in_hook", True)
                try:
                    hook()
                finally:
                    object.__setattr__(self, "_in_hook", False)
        return super().__getattribute__(name)


def _gpu_key(vendor: str, renderer: str) -> str:
    return f"{vendor}|||{renderer}"


def _parse_gpu_key(val: str | None) -> tuple[str, str] | None:
    if not val or val == "auto" or "|||" not in val:
        return None
    v, r = val.split("|||", 1)
    return (v, r)


def _short_gpu_label(vendor: str, renderer: str) -> str:
    short_r = renderer.replace(", or similar", "").replace("Direct3D11 vs_5_0 ps_5_0", "D3D11")
    if len(short_r) > 46:
        short_r = short_r[:43] + "..."
    return f"{short_r}"


def _build_dropdown(
    options: list[ft.dropdown.Option],
    value: str,
    on_select: Callable[[ft.ControlEvent], None] | None = None,
) -> ft.Dropdown:
    return ft.Dropdown(
        value=value,
        bgcolor=COLORS["input_bg"],
        color=COLORS["text_main"],
        border_color=COLORS["rule_strong"],
        focused_border_color=COLORS["sidebar"],
        border_radius=8,
        text_size=13,
        options=options,
        on_select=on_select,
    )


def open_profile_dialog(
    page: ft.Page,
    proxy_service: IProxyService,
    on_save: Callable[..., str | None],
    profile: Profile | None = None,
    profile_manager: Any = None,
) -> None:
    is_edit = profile is not None
    title = get_string("edit_profile") if is_edit else get_string("create_new_profile")
    subtitle = (
        get_string("editing_profile", name=profile.name)
        if profile is not None
        else get_string("configure_identity")
    )
    save_label = get_string("save_changes") if is_edit else get_string("create_profile_btn")
    save_icon = ft.Icons.SAVE if is_edit else ft.Icons.ADD

    initial_os = profile.os_type if profile is not None else "macos"
    bound_core = getattr(profile, "core_version", None)

    # Load existing saved fingerprint if editing, otherwise generate a fresh coherent one immediately
    current_fp: dict[str, Any] = {}
    current_chrome_persona: dict[str, Any] = {}
    has_saved_fp = False
    if profile is not None:
        prof_dir = os.path.join(DATA_DIR, profile.name)
        saved = fp_service.load_saved_fingerprint(prof_dir)
        if saved and saved[0] == initial_os:
            current_fp = saved[1]
            has_saved_fp = True
        saved_chrome = fp_service.load_saved_chrome_persona(prof_dir)
        if saved_chrome and saved_chrome.get("platform") == initial_os:
            current_chrome_persona = saved_chrome
    if not current_fp:
        with contextlib.suppress(Exception):
            current_fp = fp_service.generate_coherent_fingerprint(initial_os)
    if not current_chrome_persona:
        current_chrome_persona = fp_service.generate_chrome_persona(
            initial_os, core_version=bound_core
        )

    fp_state: dict[str, Any] = {"fp": current_fp}
    chrome_state: dict[str, Any] = {"persona": current_chrome_persona}

    # --- Left Column: Identity, Proxy & Account/2FA ---
    name_field = ft.TextField(
        value=profile.name if profile is not None else "",
        hint_text="" if profile is not None else get_string("enter_profile_name"),
        **DLG_FIELD_KWARGS,
    )

    group_options = [ft.dropdown.Option("", get_string("group_none"))] + [
        ft.dropdown.Option(g, g) for g in DEFAULT_GROUPS
    ]
    if profile and profile.group and profile.group not in DEFAULT_GROUPS:
        group_options.append(ft.dropdown.Option(profile.group, profile.group))
    group_dropdown = _build_dropdown(
        group_options,
        (profile.group or "") if profile is not None else "",
    )

    proxy_field = ft.TextField(
        value=(profile.proxy or "") if profile is not None else "",
        hint_text="socks5://user:pass@host:port",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    os_dropdown = build_os_dropdown(initial_os)
    os_dropdown.expand = 2
    engine_dropdown = build_engine_dropdown(
        profile.engine if profile is not None else "camoufox",
        disabled=is_edit,
    )
    engine_dropdown.expand = 3
    engine_dropdown.label = None
    os_dropdown.label = None
    for dropdown in (engine_dropdown, os_dropdown):
        dropdown.text_size = 13.5

    engine_note = ft.Container(
        visible=engine_dropdown.value == "chrome" and not is_edit,
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        border_radius=8,
        bgcolor=COLORS["warn_tint"],
        content=ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color=COLORS["warning"]),
                ft.Text(
                    get_string("engine_chrome_note"),
                    size=12,
                    color=COLORS["warn_text"],
                    expand=True,
                ),
            ],
        ),
    )

    timezone_field = ft.TextField(
        value=(profile.timezone or "") if profile is not None else "",
        hint_text=get_string("timezone_hint"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    locale_field = ft.TextField(
        value=(profile.locale or "") if profile is not None else "",
        hint_text=get_string("locale_hint"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    start_url_field = ft.TextField(
        value=(profile.start_url or "") if profile is not None else "",
        hint_text="https://mail.google.com",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )

    # Account & 2FA controls
    quick_paste_field = ft.TextField(
        hint_text="uid|password|2FA_SECRET|note",
        text_style=_DATA,
        expand=True,
        **DLG_FIELD_KWARGS,
    )
    uid_field = ft.TextField(
        value=(profile.account_uid or "") if profile is not None else "",
        hint_text="user@gmail.com / 100089...",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    pass_field = ft.TextField(
        value=(profile.account_password or "") if profile is not None else "",
        password=True,
        can_reveal_password=True,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    two_fa_field = ft.TextField(
        value=(profile.two_fa_secret or "") if profile is not None else "",
        hint_text="JBSWY3DPEHPK3PXP",
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    totp_preview_text = _LiveTotpText(
        "",
        size=12,
        font_family=FONT_DATA,
        color=COLORS["sidebar"],
    )
    recovery_email_field = ft.TextField(
        value=profile.effective_recovery_email if profile is not None else "",
        hint_text=get_string("recovery_email_hint"),
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    initial_backup_codes = (
        normalize_backup_codes(profile.backup_codes) if profile is not None else ""
    )
    backup_codes_field = ft.TextField(
        key="dialog_backup_codes_field",
        value=initial_backup_codes,
        hint_text=get_string("backup_codes_placeholder"),
        multiline=True,
        min_lines=3,
        max_lines=5,
        text_style=_DATA,
        **DLG_FIELD_KWARGS,
    )
    notes_field = ft.TextField(
        value=profile.effective_notes if profile is not None else "",
        hint_text=get_string("notes_hint"),
        **DLG_FIELD_KWARGS,
    )

    def _clean_backup_text(raw: str) -> str:
        return raw.replace("\r\n", "\n").replace("\r", "\n").strip()

    initial_account_uid = (profile.account_uid or "").strip() if profile is not None else ""
    initial_recovery_email = (recovery_email_field.value or "").strip()
    cred_state: dict[str, Any] = {
        "account_uid": initial_account_uid,
        "recovery_owner": initial_account_uid if initial_recovery_email else "",
        "recovery_value": initial_recovery_email,
        "backup_owner": initial_account_uid if initial_backup_codes else "",
        "backup_value": initial_backup_codes,
        "initial_backup_restore": None,
        "used_quick_paste": False,
    }
    account_error = ft.Text(
        "",
        key="account_cred_error",
        size=12,
        color=COLORS["error"],
        visible=False,
    )

    def _on_recovery_change(_: ft.ControlEvent) -> None:
        val = (recovery_email_field.value or "").strip()
        owner = (uid_field.value or "").strip() or cred_state["account_uid"]
        cred_state["recovery_value"] = val
        cred_state["recovery_owner"] = owner if val else ""
        with contextlib.suppress(Exception):
            ft.context.disable_auto_update()

    def _on_backup_change(_: ft.ControlEvent) -> None:
        val = _clean_backup_text(backup_codes_field.value or "")
        owner = (uid_field.value or "").strip() or cred_state["account_uid"]
        cred_state["backup_value"] = val
        cred_state["backup_owner"] = owner if val else ""
        if initial_account_uid and owner.casefold() == initial_account_uid.casefold():
            cred_state["initial_backup_restore"] = None
        with contextlib.suppress(Exception):
            ft.context.disable_auto_update()

    recovery_email_field.on_change = _on_recovery_change
    backup_codes_field.on_change = _on_backup_change

    totp_timer_state: dict[str, Any] = {"active": True, "timer": None, "last": None}
    totp_timer_lock = threading.Lock()
    timer_cls = threading.Timer

    def _render_totp_state(notify: bool = True) -> None:
        raw = (two_fa_field.value or "").strip()
        if not raw:
            new_val = ""
            new_color = totp_preview_text.color
            new_visible = False
        else:
            res = generate_totp(raw)
            if res:
                code, rem = res
                new_val = f"🛡️ {get_string('two_fa_preview', code=code, rem=rem)}"
                new_color = COLORS["success"]
                new_visible = True
            else:
                new_val = get_string("two_fa_invalid")
                new_color = COLORS["warning"]
                new_visible = True
        state = (new_val, new_color, new_visible)
        if state == totp_timer_state["last"]:
            return
        totp_timer_state["last"] = state
        totp_preview_text.value = new_val
        totp_preview_text.color = new_color
        totp_preview_text.visible = new_visible
        if notify:
            with contextlib.suppress(Exception):
                if (
                    isinstance(page, ft.Page)
                    and getattr(totp_preview_text, "_parent", None) is not None
                ):
                    totp_preview_text.update()
                else:
                    page.update()

    def _update_totp_preview(_: object = None) -> None:
        _render_totp_state(notify=True)

    def _sync_totp_on_read() -> None:
        if totp_timer_state["active"]:
            _render_totp_state(notify=False)

    object.__setattr__(totp_preview_text, "_refresh_hook", _sync_totp_on_read)

    def _schedule_totp_tick() -> None:
        if not totp_timer_state["active"]:
            return
        timer = timer_cls(1.0, _on_totp_tick)
        timer.daemon = True
        with totp_timer_lock:
            if totp_timer_state["active"]:
                totp_timer_state["timer"] = timer
                timer.start()

    def _on_totp_tick() -> None:
        if not totp_timer_state["active"]:
            return
        _render_totp_state(notify=True)
        _schedule_totp_tick()

    def _stop_totp_timer(_: object = None) -> None:
        global _active_totp_stop
        with totp_timer_lock:
            totp_timer_state["active"] = False
            timer = totp_timer_state.get("timer")
            if timer is not None:
                with contextlib.suppress(Exception):
                    timer.cancel()
                totp_timer_state["timer"] = None
        with _active_totp_lock:
            if _active_totp_stop is _stop_totp_timer:
                _active_totp_stop = None
        object.__setattr__(totp_preview_text, "_refresh_hook", None)

    two_fa_field.on_change = _update_totp_preview
    _render_totp_state(notify=False)

    def _on_quick_paste(e: ft.ControlEvent | None = None) -> None:
        raw_val = quick_paste_field.value or ""
        if not raw_val and e is not None and isinstance(getattr(e, "data", None), str):
            raw_val = e.data
        raw = raw_val.strip()
        if not raw:
            with contextlib.suppress(Exception):
                ft.context.disable_auto_update()
            return
        parsed = parse_account_line(raw)
        next_uid = parsed["account_uid"]
        typed_uid = (uid_field.value or "").strip()
        # On a previously bound draft, a temporarily blank UID stays bound to the last account.
        owner_uid = typed_uid or cred_state["account_uid"]
        current_rec = (recovery_email_field.value or "").strip()
        current_backup = _clean_backup_text(backup_codes_field.value or "")
        cred_state["used_quick_paste"] = True
        # Re-bind credential ownership only when the user explicitly edited that field.
        if current_rec != cred_state["recovery_value"]:
            cred_state["recovery_owner"] = owner_uid if current_rec else ""
            cred_state["recovery_value"] = current_rec
        if current_backup != cred_state["backup_value"]:
            cred_state["backup_owner"] = owner_uid if current_backup else ""
            cred_state["backup_value"] = current_backup
            if initial_account_uid and owner_uid.casefold() == initial_account_uid.casefold():
                cred_state["initial_backup_restore"] = None

        rec_owner = cred_state["recovery_owner"]
        rec_belongs_to_other = bool(
            rec_owner and next_uid and rec_owner.casefold() != next_uid.casefold()
        )
        backup_owner = cred_state["backup_owner"]
        backup_belongs_to_other = bool(
            backup_owner and next_uid and backup_owner.casefold() != next_uid.casefold()
        )

        uid_field.value = next_uid
        if next_uid and not (name_field.value or "").strip() and not is_edit:
            name_field.value = next_uid.split("@")[0][:40]
        pass_field.value = parsed["account_password"]
        two_fa_field.value = parsed["two_fa_secret"]

        pasted_rec, clean_note = split_recovery_and_notes("", parsed["notes"])
        if pasted_rec:
            recovery_email_field.value = pasted_rec
            # Keep a blank-UID Parse bound to the active draft account rather than unowned.
            cred_state["recovery_owner"] = next_uid or owner_uid
            cred_state["recovery_value"] = pasted_rec
        elif rec_belongs_to_other:
            recovery_email_field.value = ""
            cred_state["recovery_owner"] = ""
            cred_state["recovery_value"] = ""
        elif current_rec and next_uid and not cred_state["recovery_owner"]:
            cred_state["recovery_owner"] = next_uid

        reverting_to_initial = bool(
            is_edit
            and initial_account_uid
            and next_uid
            and next_uid.casefold() == initial_account_uid.casefold()
        )
        if backup_belongs_to_other:
            if (
                is_edit
                and initial_account_uid
                and backup_owner.casefold() == initial_account_uid.casefold()
            ):
                cred_state["initial_backup_restore"] = current_backup
            if reverting_to_initial and cred_state["initial_backup_restore"] is not None:
                restored = cred_state["initial_backup_restore"]
                backup_codes_field.value = restored
                cred_state["backup_owner"] = next_uid if restored else ""
                cred_state["backup_value"] = restored
                cred_state["initial_backup_restore"] = None
            else:
                backup_codes_field.value = ""
                cred_state["backup_owner"] = ""
                cred_state["backup_value"] = ""
        elif (
            reverting_to_initial
            and cred_state["initial_backup_restore"] is not None
            and not current_backup
        ):
            # Reverting restores A's untouched snapshot or deliberate edit from before the detour.
            restored = cred_state["initial_backup_restore"]
            backup_codes_field.value = restored
            cred_state["backup_owner"] = next_uid if restored else ""
            cred_state["backup_value"] = restored
            cred_state["initial_backup_restore"] = None
        elif current_backup and next_uid and not cred_state["backup_owner"]:
            cred_state["backup_owner"] = next_uid

        if next_uid:
            cred_state["account_uid"] = next_uid
        notes_field.value = clean_note
        quick_paste_field.value = ""
        account_error.visible = False
        # Update TOTP preview state without a child-only update() call, which in Flet 1.x
        # marks update-called and suppresses auto-update for the 7 sibling fields above.
        _render_totp_state(notify=False)
        with contextlib.suppress(Exception):
            page.update()

    quick_paste_field.on_submit = _on_quick_paste
    quick_paste_btn = ft.OutlinedButton(
        get_string("quick_paste_btn"),
        icon=ft.Icons.BOLT,
        height=38,
        style=OUTLINE_STYLE,
        on_click=_on_quick_paste,
    )

    # --- Right Column: Hardware & Fingerprint Controls (Coherence C++) ---
    details = fp_service.extract_details(fp_state["fp"], initial_os)

    def _screen_options_for_os(os_name: str, current_scr: str | None) -> list[ft.dropdown.Option]:
        presets = list(fp_service.COMMON_SCREENS.get(os_name, ["auto", "1920x1080"]))
        if current_scr and current_scr not in presets:
            presets.insert(1, current_scr)
        return [
            ft.dropdown.Option("auto", get_string("screen_auto"))
            if s == "auto"
            else ft.dropdown.Option(s, s)
            for s in presets
        ]

    def _gpu_options_for_os(os_name: str) -> list[ft.dropdown.Option]:
        gpus = fp_service.available_gpus(os_name)
        return [ft.dropdown.Option(_gpu_key(v, r), _short_gpu_label(v, r)) for v, r in gpus] or [
            ft.dropdown.Option("auto", "Auto")
        ]

    def _core_options_for(os_name: str, renderer: str) -> list[ft.dropdown.Option]:
        if engine_dropdown.value == "chrome" and os_name == "macos":
            return [
                ft.dropdown.Option(str(c), f"{c} Cores")
                for c in macos_cpu_counts_for_seed(chrome_state["persona"]["seed"])
            ]
        cores = set(fp_service.available_cores(os_name, renderer))
        if engine_dropdown.value == "chrome":
            from chrome_engine.persona import _CORES

            cores.update(c for c, _ in _CORES.get(os_name, []))
        if init_cores and init_cores.isdigit():
            cores.add(int(init_cores))
        return [ft.dropdown.Option(str(c), f"{c} Cores") for c in sorted(cores)]

    init_screen = details.get("screen") or "auto"
    screen_dropdown = _build_dropdown(
        _screen_options_for_os(initial_os, init_screen),
        init_screen,
    )

    init_gpu_key = _gpu_key(details.get("gpu_vendor") or "", details.get("gpu_renderer") or "")
    gpu_opts = _gpu_options_for_os(initial_os)
    valid_gpu_keys = {opt.key for opt in gpu_opts}
    if init_gpu_key not in valid_gpu_keys and gpu_opts:
        init_gpu_key = gpu_opts[0].key or "auto"
    gpu_dropdown = _build_dropdown(gpu_opts, init_gpu_key)

    init_cores = str(
        (
            chrome_state["persona"].get("hardware_concurrency")
            if (profile is not None and profile.engine == "chrome")
            else details.get("hardware_concurrency")
        )
        or 8
    )
    core_opts = _core_options_for(initial_os, details.get("gpu_renderer") or "")
    if init_cores not in {opt.key for opt in core_opts} and core_opts:
        init_cores = core_opts[0].key or "8"
    cores_dropdown = _build_dropdown(core_opts, init_cores)

    webrtc_dropdown = _build_dropdown(
        [
            ft.dropdown.Option("proxy", get_string("webrtc_proxy")),
            ft.dropdown.Option("disable", get_string("webrtc_disable")),
        ],
        profile.webrtc_mode if profile is not None else "proxy",
    )
    canvas_dropdown = _build_dropdown(
        [
            ft.dropdown.Option("auto", get_string("canvas_auto")),
            ft.dropdown.Option("noise", get_string("canvas_noise")),
            ft.dropdown.Option("native", get_string("canvas_native")),
        ],
        profile.canvas_mode if profile is not None else "auto",
    )

    fp_summary_col = ft.Column(spacing=4, tight=True)

    def _render_fp_summary() -> None:
        os_val = os_dropdown.value or "windows"
        is_chrome_mode = engine_dropdown.value == "chrome"
        info = (
            fp_service.extract_chrome_details(chrome_state["persona"], os_val)
            if is_chrome_mode
            else fp_service.extract_details(fp_state["fp"], os_val)
        )
        # Chrome's native GPU follows its saved seed, not the Camoufox GPU catalog.
        vendor, renderer = info.get("gpu_vendor") or "", info.get("gpu_renderer") or ""
        gpu_key = _gpu_key(vendor, renderer)
        gpu_dropdown.options = (
            [ft.dropdown.Option(gpu_key, _short_gpu_label(vendor, renderer))]
            if is_chrome_mode
            else _gpu_options_for_os(os_val)
        )
        valid_keys = {option.key for option in gpu_dropdown.options}
        gpu_dropdown.value = gpu_key if gpu_key in valid_keys else gpu_dropdown.options[0].key
        ua = info.get("user_agent") or "Mozilla/5.0 (Firefox/158.0)"
        ua_short = ua[:59] + "..." if len(ua) > 62 else ua

        def _kv(k: str, v: str) -> ft.Row:
            return ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Text(
                        k, expand=4, size=11.5, color=COLORS["text_dim"], font_family=FONT_SEMIBOLD
                    ),
                    ft.Text(
                        v,
                        expand=7,
                        text_align=ft.TextAlign.RIGHT,
                        size=11.5,
                        color=COLORS["text_main"],
                        font_family=FONT_DATA,
                        selectable=True,
                    ),
                ],
            )

        status_text = (
            get_string("fp_chrome_mode_note") if is_chrome_mode else get_string("fp_coherence_ok")
        )
        status_color = COLORS["success"]

        trait_sig = f"{info.get('trait_hash') or '#--------'} · Win {info.get('window_inner') or info.get('screen') or 'Auto'}"
        dpr_text = f" · DPR {info['dpr']}" if info["dpr"] is not None else ""
        fp_summary_col.controls = [
            _kv("OS / Platform", f"{info['os'].upper()} · {info['platform']}"),
            _kv(
                "Screen / Display",
                f"{info.get('screen') or 'Auto'}{dpr_text} · {info['color_depth']}-bit",
            ),
            _kv("CPU Concurrency", f"{info['hardware_concurrency']} Cores"),
            _kv("WebGL Vendor", str(info.get("gpu_vendor") or "Auto")),
            _kv("WebGL Renderer", _short_gpu_label("", str(info.get("gpu_renderer") or "Auto"))),
            _kv(
                "Audio / Fonts",
                f"{info['audio_sample_rate']} Hz · {info['fonts_count']} OS Fonts",
            ),
            _kv(get_string("fp_trait_hash_label"), trait_sig),
            ft.Text(
                ua_short,
                size=10.5,
                font_family=FONT_DATA,
                color=COLORS["text_sub"],
                tooltip=ua,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            ft.Container(
                padding=ft.Padding.only(top=4),
                content=ft.Text(
                    status_text,
                    size=11.5,
                    font_family=FONT_SEMIBOLD,
                    color=status_color,
                ),
            ),
        ]

    hw_changed = {"value": False}
    chrome_rolled = {"value": not is_edit}

    def _rebuild_fp(roll_new: bool = False, os_changed: bool = False) -> None:
        hw_changed["value"] = True
        if roll_new or os_changed:
            chrome_rolled["value"] = True
        os_val = os_dropdown.value or "windows"
        is_chrome_mode = engine_dropdown.value == "chrome"
        if os_changed:
            new_gpu_opts = _gpu_options_for_os(os_val)
            gpu_dropdown.options = new_gpu_opts
            gpu_dropdown.value = new_gpu_opts[0].key if new_gpu_opts else "auto"
            new_scr_opts = _screen_options_for_os(os_val, None)
            screen_dropdown.options = new_scr_opts
            screen_dropdown.value = "auto"

        gpu_pair = None if (roll_new or os_changed) else _parse_gpu_key(gpu_dropdown.value)
        renderer_hint = "" if is_chrome_mode else (gpu_pair[1] if gpu_pair else "")
        new_core_opts = _core_options_for(os_val, renderer_hint)
        cores_dropdown.options = new_core_opts
        valid_core_strs = {opt.key for opt in new_core_opts}
        if cores_dropdown.value not in valid_core_strs and new_core_opts:
            cores_dropdown.value = new_core_opts[0].key

        cpu_int = (
            None
            if (roll_new or os_changed)
            else (int(cores_dropdown.value) if (cores_dropdown.value or "").isdigit() else None)
        )
        scr_spec = "auto" if (roll_new or os_changed) else (screen_dropdown.value or "auto")
        base = None if (roll_new or os_changed) else fp_state["fp"]

        with contextlib.suppress(Exception):
            fp_state["fp"] = fp_service.generate_coherent_fingerprint(
                os_val,
                screen_spec=scr_spec,
                cpu_cores=cpu_int,
                gpu_pair=gpu_pair,
                base_fp=base,
            )
            info = fp_service.extract_details(fp_state["fp"], os_val)
            # Sync dropdown values back to coherent result
            scr_now = info.get("screen")
            screen_dropdown.options = _screen_options_for_os(os_val, scr_now)
            if scr_spec == "auto":
                screen_dropdown.value = "auto"
            elif scr_now:
                screen_dropdown.value = scr_now
            g_key = _gpu_key(info.get("gpu_vendor") or "", info.get("gpu_renderer") or "")
            if g_key in {opt.key for opt in gpu_dropdown.options}:
                gpu_dropdown.value = g_key
            if not is_chrome_mode:
                new_core_opts = _core_options_for(os_val, info.get("gpu_renderer") or "")
                cores_dropdown.options = new_core_opts
                c_str = str(info.get("hardware_concurrency") or 8)
                if c_str in {opt.key for opt in new_core_opts}:
                    cores_dropdown.value = c_str

        with contextlib.suppress(Exception):
            keep_seed = None if (roll_new or os_changed) else chrome_state["persona"].get("seed")
            base_persona = None if (roll_new or os_changed) else chrome_state.get("persona")
            chrome_cpu = cpu_int if is_chrome_mode else None
            chrome_state["persona"] = fp_service.generate_chrome_persona(
                os_val,
                hardware_concurrency=chrome_cpu,
                seed=int(keep_seed) if isinstance(keep_seed, int) else None,
                base_persona=base_persona,
                roll_new=roll_new,
                core_version=bound_core,
            )
            if is_chrome_mode:
                cores_dropdown.options = _core_options_for(os_val, renderer_hint)
                c_str = str(chrome_state["persona"].get("hardware_concurrency") or 8)
                if c_str in {opt.key for opt in cores_dropdown.options}:
                    cores_dropdown.value = c_str
                elif cores_dropdown.options:
                    cores_dropdown.value = cores_dropdown.options[0].key

        _render_fp_summary()
        with contextlib.suppress(Exception):
            page.update()

    os_changed_by_user = False

    def on_os_change(_: ft.ControlEvent) -> None:
        nonlocal os_changed_by_user
        os_changed_by_user = True
        _rebuild_fp(roll_new=True, os_changed=True)

    os_dropdown.on_select = on_os_change
    screen_dropdown.on_select = lambda _: _rebuild_fp(roll_new=False)
    cores_dropdown.on_select = lambda _: _rebuild_fp(roll_new=False)
    gpu_dropdown.on_select = lambda _: _rebuild_fp(roll_new=False)

    roll_btn = ft.OutlinedButton(
        get_string("roll_fingerprint_btn"),
        icon=ft.Icons.CASINO_OUTLINED,
        height=34,
        style=OUTLINE_STYLE,
        on_click=lambda _: _rebuild_fp(roll_new=True),
    )

    def _sync_hardware_controls_for_engine() -> None:
        is_chrome = engine_dropdown.value == "chrome"
        for ctrl in (screen_dropdown, gpu_dropdown, canvas_dropdown):
            ctrl.disabled = is_chrome
        for ctrl in (cores_dropdown, webrtc_dropdown, roll_btn):
            ctrl.disabled = False
        if is_chrome:
            cores_dropdown.options = _core_options_for(os_dropdown.value or "windows", "")
            c_str = str(chrome_state["persona"].get("hardware_concurrency") or 8)
            if c_str in {opt.key for opt in cores_dropdown.options}:
                cores_dropdown.value = c_str
            elif cores_dropdown.options:
                cores_dropdown.value = cores_dropdown.options[0].key

    _sync_hardware_controls_for_engine()
    _render_fp_summary()

    def on_engine_change(_: ft.ControlEvent) -> None:
        if not is_edit:
            if not os_changed_by_user:
                os_dropdown.value = host_os() if engine_dropdown.value == "chrome" else "macos"
            _rebuild_fp(roll_new=True, os_changed=True)
        engine_note.visible = engine_dropdown.value == "chrome" and not is_edit
        _sync_hardware_controls_for_engine()
        _render_fp_summary()
        page.update()

    engine_dropdown.on_select = on_engine_change

    geo_error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    geo_result = ft.Column(spacing=5, tight=True, visible=False)
    name_error = ft.Text("", size=12, color=COLORS["error"], visible=False)
    proxy_error = ft.Text("", size=12, color=COLORS["error"], visible=False)

    check_btn = ft.OutlinedButton(
        get_string("check_proxy"),
        icon=ft.Icons.WIFI_FIND,
        height=36,
        style=OUTLINE_STYLE,
    )
    ip_btn = ft.OutlinedButton(
        get_string("check_ip"),
        icon=ft.Icons.PUBLIC,
        height=36,
        style=OUTLINE_STYLE,
    )
    pool_idx_state = {"idx": 0}

    def _on_pick_from_pool(_: ft.ControlEvent) -> None:
        from ...services.profile.manager import ProfileManager
        from ...services.proxy.pool import ProxyPoolService

        pool = ProxyPoolService()
        entries = [e for e in pool.list_entries() if e.status != "dead"] or pool.list_entries()
        if not entries:
            proxy_error.value = get_string("proxy_pool_empty_hint")
            proxy_error.color = COLORS["warning"]
            proxy_error.visible = True
            with contextlib.suppress(Exception):
                page.update()
            return
        pm_inst = profile_manager if profile_manager is not None else ProfileManager()
        counts = pool.usage_counts(pm_inst)
        entries.sort(key=lambda e: (counts.get(e.id, 0), 0 if e.status == "working" else 1))
        chosen = (
            (pool.pick_least_used(pm_inst) or entries[0])
            if pool_idx_state["idx"] == 0
            else entries[pool_idx_state["idx"] % len(entries)]
        )
        pool_idx_state["idx"] += 1
        proxy_field.value = chosen.proxy
        proxy_error.value = get_string("picked_pool_proxy", masked=chosen.masked)
        proxy_error.color = COLORS["success"]
        proxy_error.visible = True
        with contextlib.suppress(Exception):
            page.update()

    pick_pool_btn = ft.OutlinedButton(
        get_string("pick_pool_proxy_btn"),
        icon=ft.Icons.STORAGE_OUTLINED,
        height=36,
        style=OUTLINE_STYLE,
        on_click=_on_pick_from_pool,
    )
    ip_btn.on_click = lambda _: _do_ip_check(
        page, proxy_field, timezone_field, locale_field, geo_result, ip_btn
    )
    check_btn.on_click = lambda _: _do_proxy_check(
        page, proxy_field, proxy_error, check_btn, proxy_service
    )

    ext_service = ExtensionService()
    selected_ext_ids: set[str] = set(profile.extension_ids if profile is not None else [])
    ext_checks_col = ft.Column(spacing=2, tight=True)

    def _rebuild_ext_checks() -> None:
        entries = ext_service.list_entries()
        if not entries:
            ext_checks_col.controls = [
                ft.Text(
                    get_string("ext_empty_hint"),
                    size=11.5,
                    color=COLORS["text_dim"],
                )
            ]
            return
        rows: list[ft.Control] = []
        for e in entries:
            is_global = bool(e.apply_to_all_chrome)
            is_checked = is_global or (e.id in selected_ext_ids)

            def _on_chk(ev: ft.ControlEvent, eid: str = e.id) -> None:
                if ev.control.value:
                    selected_ext_ids.add(eid)
                else:
                    selected_ext_ids.discard(eid)

            pin_lbl = (
                " · 📌"
                if e.pin_mode == "force_pinned"
                else (" · 🚫" if e.pin_mode == "force_unpinned" else "")
            )
            global_lbl = f" · {get_string('ext_apply_all_chrome')}" if is_global else ""
            rows.append(
                ft.Checkbox(
                    value=is_checked,
                    disabled=is_global,
                    label=f"{e.name} (v{e.version}){pin_lbl}{global_lbl}",
                    fill_color=COLORS["accent"],
                    on_change=_on_chk,
                )
            )
        ext_checks_col.controls = rows

    _rebuild_ext_checks()

    def _open_ext_manager(_: ft.ControlEvent) -> None:
        from ...services.profile.manager import ProfileManager
        from .extensions import open_extensions_dialog

        pm_inst = profile_manager if profile_manager is not None else ProfileManager()
        open_extensions_dialog(
            page,
            ext_service,
            pm_inst,
            refresh_fn=lambda: (_rebuild_ext_checks(), page.update()),
        )

    manage_ext_btn = ft.OutlinedButton(
        get_string("ext_manage_btn"),
        icon=ft.Icons.EXTENSION_OUTLINED,
        height=30,
        style=OUTLINE_STYLE,
        on_click=_open_ext_manager,
    )

    def on_submit(_: ft.ControlEvent) -> None:
        name = (name_field.value or "").strip()
        proxy = (proxy_field.value or "").strip()
        os_type = os_dropdown.value or "windows"
        engine = engine_dropdown.value or "camoufox"
        timezone = (timezone_field.value or "").strip()
        locale = (locale_field.value or "").strip()
        name_error.visible = proxy_error.visible = geo_error.visible = account_error.visible = False

        valid_name, name_err = validate_profile_name(
            name, allow_reserved=(profile is not None and name == profile.name)
        )
        if not valid_name:
            name_error.value = name_err
            name_error.visible = True
            page.update()
            return

        valid_proxy, proxy_err = validate_proxy_format(proxy)
        if not valid_proxy:
            proxy_error.value = proxy_err
            proxy_error.visible = True
            page.update()
            return

        for validate, value in ((validate_timezone, timezone), (validate_locale, locale)):
            valid, geo_err = validate(value)
            if not valid:
                geo_error.value = geo_err
                geo_error.visible = True
                page.update()
                return

        raw_2fa = (two_fa_field.value or "").strip()
        cleaned_2fa = clean_totp_secret(raw_2fa)
        if raw_2fa and not cleaned_2fa:
            totp_preview_text.value = get_string("two_fa_invalid")
            totp_preview_text.color = COLORS["error"]
            totp_preview_text.visible = True
            page.update()
            return

        raw_start_url = (start_url_field.value or "").strip()
        valid_url, url_err = validate_start_url(raw_start_url)
        if not valid_url:
            name_error.value = url_err
            name_error.visible = True
            page.update()
            return

        final_uid = (uid_field.value or "").strip()
        final_rec = (recovery_email_field.value or "").strip()
        raw_backup = backup_codes_field.value or ""
        quick_paste_reverted_auto_clear = bool(
            is_edit
            and cred_state["used_quick_paste"]
            and initial_account_uid
            and final_uid.casefold() == initial_account_uid.casefold()
            and cred_state["initial_backup_restore"] is not None
            and not cred_state["backup_value"]
            and not _clean_backup_text(raw_backup)
        )
        if quick_paste_reverted_auto_clear:
            raw_backup = cred_state["initial_backup_restore"]

        try:
            cleaned_backup_codes = normalize_backup_codes(raw_backup)
        except ValueError as exc:
            name_error.value = str(exc)
            name_error.visible = True
            page.update()
            return

        if cred_state["used_quick_paste"]:
            if final_rec != cred_state["recovery_value"]:
                cred_state["recovery_owner"] = final_uid if final_rec else ""
                cred_state["recovery_value"] = final_rec
            if (
                not quick_paste_reverted_auto_clear
                and cleaned_backup_codes != cred_state["backup_value"]
            ):
                cred_state["backup_owner"] = final_uid if cleaned_backup_codes else ""
                cred_state["backup_value"] = cleaned_backup_codes
                if initial_account_uid and final_uid.casefold() == initial_account_uid.casefold():
                    cred_state["initial_backup_restore"] = None

            mismatched_owners: list[str] = []
            if (
                final_rec
                and cred_state["recovery_owner"]
                and cred_state["recovery_owner"].casefold() != final_uid.casefold()
            ):
                mismatched_owners.append(cred_state["recovery_owner"])
            if (
                cleaned_backup_codes
                and cred_state["backup_owner"]
                and cred_state["backup_owner"].casefold() != final_uid.casefold()
            ):
                mismatched_owners.append(cred_state["backup_owner"])
            if mismatched_owners:
                account_error.value = get_string(
                    "quick_paste_uid_mismatch",
                    owner=", ".join(dict.fromkeys(mismatched_owners)),
                    uid=final_uid or "(empty)",
                )
                account_error.visible = True
                page.update()
                return

        is_chrome = engine == "chrome"
        default_canvas = profile.canvas_mode if profile is not None else "auto"
        if is_edit and not hw_changed["value"] and (is_chrome or has_saved_fp):
            initial_fp: dict[str, Any] | None = None
        elif is_chrome and not chrome_rolled["value"]:
            raw_cores = cores_dropdown.value or ""
            selected_cpu = int(raw_cores) if raw_cores.isdigit() else None
            initial_fp = {
                "platform": os_type,
                "hardware_concurrency": selected_cpu,
            }
            if profile is not None and os_type == "macos":
                latest = fp_service.load_saved_chrome_persona(os.path.join(DATA_DIR, profile.name))
                if latest is None:
                    initial_fp["seed"] = chrome_state["persona"]["seed"]
                elif selected_cpu not in macos_cpu_counts_for_seed(latest["seed"]):
                    initial_fp["hardware_concurrency"] = None
        else:
            initial_fp = chrome_state["persona"] if is_chrome else fp_state["fp"]
        extra_kwargs = {
            "group": (group_dropdown.value or "").strip(),
            "account_uid": final_uid,
            "account_password": pass_field.value or "",
            "two_fa_secret": cleaned_2fa,
            "recovery_email": final_rec,
            "notes": (notes_field.value or "").strip(),
            "start_url": normalize_start_url(raw_start_url),
            "webrtc_mode": webrtc_dropdown.value or "proxy",
            "canvas_mode": default_canvas if is_chrome else (canvas_dropdown.value or "auto"),
            "initial_fingerprint": initial_fp,
            "extension_ids": sorted(selected_ext_ids),
        }
        quick_paste_replaced_initial = bool(
            is_edit
            and cred_state["used_quick_paste"]
            and initial_account_uid
            and final_uid.casefold() != initial_account_uid.casefold()
        )
        if (not is_edit and cleaned_backup_codes) or (
            is_edit
            and (cleaned_backup_codes != initial_backup_codes or quick_paste_replaced_initial)
        ):
            extra_kwargs["backup_codes"] = cleaned_backup_codes

        sig = inspect.signature(on_save)
        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_kwargs or "group" in sig.parameters:
            error = on_save(name, proxy, os_type, timezone, locale, engine, **extra_kwargs)
        else:
            error = on_save(name, proxy, os_type, timezone, locale, engine)

        if error:
            name_error.value = error
            name_error.visible = True
            page.update()
        else:
            _close_dialog()

    name_field.on_submit = on_submit

    left_column = ft.Column(
        spacing=4,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=3,
                        content=_field(get_string("profile_name"), name_field),
                    ),
                    ft.Container(
                        expand=2,
                        content=_field(get_string("group_label"), group_dropdown),
                    ),
                ],
            ),
            name_error,
            ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Container(
                        expand=3,
                        content=_field(
                            get_string("browser_engine"),
                            engine_dropdown,
                            get_string("engine_locked") if is_edit else None,
                        ),
                    ),
                    ft.Container(
                        expand=2,
                        content=_field(get_string("operating_system"), os_dropdown),
                    ),
                ],
            ),
            engine_note,
            _field(get_string("proxy_optional"), proxy_field, get_string("proxy_format_hint")),
            ft.Row(
                spacing=8, run_spacing=8, wrap=True, controls=[check_btn, ip_btn, pick_pool_btn]
            ),
            proxy_error,
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=1,
                        content=_field(get_string("timezone_optional"), timezone_field),
                    ),
                    ft.Container(
                        expand=1,
                        content=_field(get_string("locale_optional"), locale_field),
                    ),
                ],
            ),
            geo_error,
            geo_result,
            ft.Divider(height=14, color=COLORS["card_border"]),
            ft.Text(
                get_string("account_section_title"),
                size=13.5,
                font_family=FONT_BOLD,
                color=COLORS["text_main"],
            ),
            _field(
                get_string("quick_paste_label"),
                ft.Row(spacing=8, controls=[quick_paste_field, quick_paste_btn]),
            ),
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=1,
                        content=_field(get_string("account_uid_label"), uid_field),
                    ),
                    ft.Container(
                        expand=1,
                        content=_field(get_string("account_pass_label"), pass_field),
                    ),
                ],
            ),
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=1,
                        content=_field(get_string("two_fa_label"), two_fa_field),
                    ),
                    ft.Container(
                        expand=1,
                        content=_field(get_string("start_url_label"), start_url_field),
                    ),
                ],
            ),
            totp_preview_text,
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=1,
                        content=_field(get_string("recovery_email_label"), recovery_email_field),
                    ),
                    ft.Container(
                        expand=1,
                        content=_field(get_string("notes_label"), notes_field),
                    ),
                ],
            ),
            _field(
                get_string("backup_codes_label"),
                backup_codes_field,
                get_string("backup_codes_hint"),
            ),
            account_error,
        ],
    )

    right_column = ft.Column(
        spacing=4,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Row(
                spacing=8,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        get_string("hw_section_title"),
                        size=13.5,
                        font_family=FONT_BOLD,
                        color=COLORS["text_main"],
                        expand=True,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=get_string("hw_section_title"),
                    ),
                    roll_btn,
                ],
            ),
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=3,
                        content=_field(get_string("screen_label"), screen_dropdown),
                    ),
                    ft.Container(
                        expand=2,
                        content=_field(get_string("cpu_cores_label"), cores_dropdown),
                    ),
                ],
            ),
            _field(get_string("gpu_label"), gpu_dropdown),
            ft.Row(
                spacing=10,
                controls=[
                    ft.Container(
                        expand=1,
                        content=_field(get_string("webrtc_label"), webrtc_dropdown),
                    ),
                    ft.Container(
                        expand=1,
                        content=_field(get_string("canvas_label"), canvas_dropdown),
                    ),
                ],
            ),
            ft.Container(height=6),
            ft.Container(
                padding=14,
                border_radius=10,
                bgcolor=COLORS["bg"],
                border=ft.Border.all(1, COLORS["card_border"]),
                content=ft.Column(
                    spacing=8,
                    tight=True,
                    controls=[
                        ft.Row(
                            spacing=6,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Icon(ft.Icons.FINGERPRINT, size=17, color=COLORS["sidebar"]),
                                ft.Text(
                                    get_string("fp_preview_title"),
                                    size=12.5,
                                    font_family=FONT_BOLD,
                                    color=COLORS["text_main"],
                                    expand=True,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                        ),
                        fp_summary_col,
                    ],
                ),
            ),
            ft.Divider(height=12, color=COLORS["card_border"]),
            ft.Row(
                spacing=8,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        get_string("ext_section_title"),
                        size=13,
                        font_family=FONT_BOLD,
                        color=COLORS["text_main"],
                        expand=True,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    manage_ext_btn,
                ],
            ),
            ft.Text(
                get_string("ext_section_hint"),
                size=11.5,
                color=COLORS["text_dim"],
            ),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                border_radius=8,
                bgcolor=COLORS["bg"],
                border=ft.Border.all(1, COLORS["card_border"]),
                content=ext_checks_col,
            ),
        ],
    )

    columns_host = ft.Column(
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        controls=[
            ft.Row(
                spacing=20,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Container(expand=11, content=left_column),
                    ft.Container(width=1, bgcolor=COLORS["card_border"]),
                    ft.Container(expand=10, content=right_column),
                ],
            )
        ],
    )

    body_container = ft.Container(
        width=940,
        height=660,
        content=columns_host,
    )

    def _on_dialog_resize(target_w: int, _target_h: int) -> None:
        if target_w < 900:
            columns_host.controls = [
                left_column,
                ft.Divider(height=16, color=COLORS["card_border"]),
                right_column,
            ]
        else:
            columns_host.controls = [
                ft.Row(
                    spacing=20,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Container(expand=11, content=left_column),
                        ft.Container(width=1, bgcolor=COLORS["card_border"]),
                        ft.Container(expand=10, content=right_column),
                    ],
                )
            ]

    def _close_dialog(_: object = None) -> None:
        if close is not None:
            close(None)

    dlg = ft.AlertDialog(
        modal=True,
        bgcolor=COLORS["card_bg"],
        shape=ft.RoundedRectangleBorder(radius=12),
        title_padding=ft.Padding.only(left=24, right=24, top=20, bottom=0),
        content_padding=ft.Padding.only(left=24, right=24, top=6, bottom=8),
        actions_padding=ft.Padding.only(left=24, right=24, top=8, bottom=18),
        on_dismiss=_stop_totp_timer,
        title=ft.Column(
            spacing=3,
            tight=True,
            controls=[
                ft.Text(title, size=19, font_family=FONT_BOLD, color=COLORS["text_main"]),
                ft.Text(subtitle, size=13, color=COLORS["text_sub"]),
            ],
        ),
        content=body_container,
        actions=[
            ft.TextButton(
                get_string("cancel"),
                style=ft.ButtonStyle(color=COLORS["text_sub"]),
                on_click=_close_dialog,
            ),
            ft.Button(
                save_label,
                icon=save_icon,
                style=ACCENT_STYLE,
                on_click=on_submit,
            ),
        ],
    )

    def prepare_dialog() -> None:
        bind_responsive_dialog(
            page,
            dlg,
            body_container,
            base_width=940,
            base_height=660,
            width_ratio=0.90,
            height_ratio=0.84,
            min_width=520,
            max_width=1120,
            min_height=420,
            max_height=860,
            on_resize_extra=_on_dialog_resize,
        )

    close = show_profile_editor(page, dlg, prepare_dialog, local_keyboard=True)
    if close is not None:
        global _active_totp_stop
        with _active_totp_lock:
            prev_stop = _active_totp_stop
            _active_totp_stop = _stop_totp_timer
        if prev_stop is not None:
            prev_stop()
        _schedule_totp_tick()


def _field(label: str, control: ft.Control, hint: str | None = None) -> ft.Column:
    """A label set above its field, with an optional hint below, as in the web panel."""
    return ft.Column(
        spacing=4,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Container(
                padding=ft.Padding.only(top=6),
                content=ft.Text(
                    label, size=12, font_family=FONT_SEMIBOLD, color=COLORS["text_sub"]
                ),
            ),
            control,
            *([ft.Text(hint, size=11.5, color=COLORS["text_dim"])] if hint else []),
        ],
    )


def _do_proxy_check(
    page: ft.Page,
    proxy_field: ft.TextField,
    proxy_error: ft.Text,
    check_btn: ft.OutlinedButton,
    proxy_service: IProxyService,
) -> None:
    proxy = (proxy_field.value or "").strip()
    if not proxy:
        proxy_error.value = get_string("enter_proxy_to_check")
        proxy_error.color = COLORS["warning"]
        proxy_error.visible = True
        page.update()
        return

    check_btn.content = ft.Text(get_string("proxy_checking"))
    check_btn.disabled = True
    proxy_error.visible = False
    page.update()

    def do_check() -> None:
        success, message = proxy_service.check_proxy_sync(proxy)
        check_btn.content = ft.Text(get_string("check_proxy"))
        check_btn.disabled = False
        proxy_error.value = message
        proxy_error.color = COLORS["success"] if success else COLORS["error"]
        proxy_error.visible = True
        with contextlib.suppress(Exception):
            page.update()

    threading.Thread(target=do_check, daemon=True).start()


def _resolve_ip_timezone(result: GeoCheckResult) -> str | None:
    """Return the best validated IANA timezone detected for the exit IP."""
    for s in result.sources:
        if not s.error and s.timezone and validate_timezone(s.timezone)[0]:
            return s.timezone
    if result.auto_timezone and validate_timezone(result.auto_timezone)[0]:
        return result.auto_timezone
    if result.timezone and validate_timezone(result.timezone)[0]:
        return result.timezone
    return None


def _do_ip_check(
    page: ft.Page,
    proxy_field: ft.TextField,
    timezone_field: ft.TextField,
    locale_field: ft.TextField,
    geo_result: ft.Column,
    ip_btn: ft.OutlinedButton,
) -> None:
    proxy = (proxy_field.value or "").strip()
    timezone = (timezone_field.value or "").strip()
    locale = (locale_field.value or "").strip()
    for validate, value in (
        (validate_proxy_format, proxy),
        (validate_timezone, timezone),
        (validate_locale, locale),
    ):
        valid, err = validate(value)
        if not valid:
            geo_result.controls = [ft.Text(err, size=12, color=COLORS["error"])]
            geo_result.visible = True
            page.update()
            return

    ip_btn.content = ft.Text(get_string("ip_checking"))
    ip_btn.disabled = True
    geo_result.visible = False
    page.update()

    state_holder: dict[str, GeoCheckResult | None] = {"res": None}

    def _rerender_geo() -> None:
        res = state_holder["res"]
        if res is None:
            return
        cur_tz = (timezone_field.value or "").strip()
        cur_loc = (locale_field.value or "").strip()
        eff_tz = cur_tz or res.auto_timezone
        eff_loc = cur_loc or res.locale
        updated_warnings = compare(
            res.exit_ip,
            res.country,
            eff_tz,
            eff_loc,
            bool(cur_loc),
            res.sources,
        )
        updated = GeoCheckResult(
            exit_ip=res.exit_ip,
            country=res.country,
            auto_timezone=res.auto_timezone,
            suggested_locale=res.suggested_locale,
            timezone=eff_tz,
            timezone_pinned=bool(cur_tz),
            locale=eff_loc,
            locale_pinned=bool(cur_loc),
            sources=res.sources,
            warnings=updated_warnings,
            error=res.error,
        )
        geo_result.controls = _geo_lines(
            updated,
            use_locale,
            use_timezone=use_timezone,
            use_both=use_both,
            current_tz=cur_tz,
            current_locale=cur_loc,
        )
        with contextlib.suppress(Exception):
            page.update()

    def use_locale(value: str) -> None:
        locale_field.value = value
        _rerender_geo()

    def use_timezone(value: str) -> None:
        timezone_field.value = value
        _rerender_geo()

    def use_both(tz_val: str, loc_val: str) -> None:
        timezone_field.value = tz_val
        locale_field.value = loc_val
        _rerender_geo()

    def do_check() -> None:
        try:
            result = check_geo(proxy or None, timezone or None, locale or None)
            state_holder["res"] = result
            cur_tz = (timezone_field.value or "").strip()
            cur_loc = (locale_field.value or "").strip()
            geo_result.controls = _geo_lines(
                result,
                use_locale,
                use_timezone=use_timezone,
                use_both=use_both,
                current_tz=cur_tz,
                current_locale=cur_loc,
            )
        except Exception as e:
            geo_result.controls = [
                ft.Text(get_string("geo_check_failed", error=e), size=12, color=COLORS["error"])
            ]
        ip_btn.content = ft.Text(get_string("check_ip"))
        ip_btn.disabled = False
        geo_result.visible = True
        with contextlib.suppress(Exception):
            page.update()

    threading.Thread(target=do_check, daemon=True).start()


def _geo_lines(
    result: GeoCheckResult,
    use_locale: Callable[[str], None],
    use_timezone: Callable[[str], None] | None = None,
    use_both: Callable[[str, str], None] | None = None,
    current_tz: str = "",
    current_locale: str = "",
) -> list[ft.Control]:
    def line(text: str, color: str = COLORS["text_sub"]) -> ft.Text:
        return ft.Text(text, size=12, color=color, selectable=True)

    if result.exit_ip is None:
        return [line(get_string("geo_check_failed", error=result.error), COLORS["error"])]

    ip_tz = _resolve_ip_timezone(result)
    ip_loc = result.suggested_locale
    tz_needs_sync = bool(ip_tz and current_tz.strip() != ip_tz)
    loc_needs_sync = bool(ip_loc and current_locale.strip().lower() != ip_loc.lower())

    mode = {True: get_string("geo_pinned"), False: get_string("geo_auto")}
    lines: list[ft.Control] = [
        line(
            get_string("geo_exit", ip=result.exit_ip, country=result.country or "?"),
            COLORS["text_main"],
        ),
        line(
            get_string(
                "geo_timezone", value=result.timezone or "?", mode=mode[result.timezone_pinned]
            )
        ),
        line(
            get_string(
                "geo_locale",
                value=result.locale or f"~{result.suggested_locale or '?'}",
                mode=mode[True]
                if result.locale_pinned
                else get_string("geo_locale_kept" if result.locale else "geo_locale_first"),
            )
        ),
    ]
    for s in result.sources:
        if s.error:
            lines.append(
                line(
                    get_string("geo_source_error", source=s.source, error=s.error),
                    COLORS["text_dim"],
                )
            )
        else:
            lines.append(
                line(
                    get_string(
                        "geo_source",
                        source=s.source,
                        ip=s.ip or "?",
                        country=s.country or "?",
                        city=s.city or "",
                        timezone=s.timezone or "-",
                    ),
                    COLORS["text_dim"],
                )
            )
    if result.error:
        lines.append(line(result.error, COLORS["error"]))
    for w in result.warnings:
        lines.append(line(f"\u26a0 {w.message}", COLORS["warning"]))

    if tz_needs_sync or loc_needs_sync:
        lines.append(
            line(
                f"\u26a0 {get_string('geo_warn_setting_mismatch', country=result.country or '?', tz=ip_tz or '?', loc=ip_loc or '?')}",
                COLORS["warning"],
            )
        )
    elif not result.warnings and not result.error:
        lines.append(line(f"\u2713 {get_string('geo_all_good')}", COLORS["success"]))

    action_btns: list[ft.Control] = []
    if tz_needs_sync and ip_tz and use_timezone is not None:
        action_btns.append(
            ft.OutlinedButton(
                get_string("geo_use_timezone", value=ip_tz),
                icon=ft.Icons.SCHEDULE_OUTLINED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=lambda _, v=ip_tz: use_timezone(v),
            )
        )
    if loc_needs_sync and ip_loc:
        action_btns.append(
            ft.OutlinedButton(
                get_string("geo_use_suggested", value=ip_loc),
                icon=ft.Icons.LANGUAGE_OUTLINED,
                height=32,
                style=OUTLINE_STYLE,
                on_click=lambda _, v=ip_loc: use_locale(v),
            )
        )
    if tz_needs_sync and loc_needs_sync and ip_tz and ip_loc and use_both is not None:
        action_btns.append(
            ft.Button(
                get_string("geo_use_both", tz=ip_tz, loc=ip_loc),
                icon=ft.Icons.AUTO_FIX_HIGH_OUTLINED,
                height=32,
                style=ACCENT_STYLE,
                on_click=lambda _, t_v=ip_tz, l_v=ip_loc: use_both(t_v, l_v),
            )
        )
    if action_btns:
        lines.append(
            ft.Row(
                spacing=8,
                run_spacing=6,
                wrap=True,
                controls=action_btns,
            )
        )
    return lines
