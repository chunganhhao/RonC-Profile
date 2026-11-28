from collections.abc import Callable
from functools import wraps
from typing import Any

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ...services.browser import chrome_prefetch
from ...services.profile.manager import ProfileStoreChangedError
from ...services.warming.scheduler import WarmingTargetSaveError
from ..dialogs import open_confirm_dialog, open_profile_dialog


def _store_conflict(on_save):
    @wraps(on_save)
    def guarded(*args, **kwargs):
        try:
            return on_save(*args, **kwargs)
        except WarmingTargetSaveError:
            return get_string("warming_rename_save_error")
        except (ProfileStoreChangedError, OSError, ValueError) as exc:
            return str(exc) or "Profile/store is busy; refresh and retry"

    return guarded


def delete_profile_logged(
    name: str,
    pm: IProfileManager,
    log: Callable[[str], None],
) -> None:
    try:
        pm.delete_profile(name)
    except (ProfileStoreChangedError, OSError) as exc:
        log(str(exc) or "Profile/store is busy; refresh and retry")
        return
    log(get_string("deleted_profile", name=name))


def delete_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def do_delete() -> None:
        delete_profile_logged(name, pm, log)
        refresh()

    open_confirm_dialog(page, name, do_delete)


def edit_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    profile = pm.profiles.get(name)
    if not profile:
        return
    original = profile.name
    initial = {
        "name": original,
        "proxy": (profile.proxy or "").strip(),
        "os_type": profile.os_type,
        "timezone": (profile.timezone or "").strip(),
        "locale": (profile.locale or "").strip(),
        "group": (profile.group or "").strip(),
        "account_uid": (profile.account_uid or "").strip(),
        "account_password": profile.account_password or "",
        "two_fa_secret": profile.two_fa_secret or "",
        "recovery_email": profile.effective_recovery_email.strip(),
        "notes": profile.effective_notes.strip(),
        "start_url": profile.start_url or "",
        "canvas_mode": profile.canvas_mode,
        "extension_ids": sorted(set(profile.extension_ids)),
        "initial_fingerprint": None,
    }

    @_store_conflict
    def on_save(
        new_name: str,
        new_proxy: str,
        new_os: str,
        timezone: str,
        locale: str,
        _engine: str,
        **extra: Any,
    ) -> str | None:
        if new_name != original and bl.is_running(original):
            return get_string("stop_before_rename")
        values = {
            "name": new_name,
            "proxy": new_proxy,
            "os_type": new_os,
            "timezone": timezone,
            "locale": locale,
            **extra,
        }
        changes = {key: value for key, value in values.items() if value != initial.get(key)}
        if extra.get("initial_fingerprint") is not None:
            changes["os_type"] = new_os
        if pm.patch_profile(original, **changes):
            log(get_string("updated_profile", old=original, new=new_name))
            refresh()
            return None
        return get_string("update_failed")

    open_profile_dialog(page, ps, on_save, profile, profile_manager=pm)


def add_profile(
    page: ft.Page,
    pm: IProfileManager,
    ps: IProxyService,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    @_store_conflict
    def on_save(
        name: str,
        proxy: str,
        os_type: str,
        timezone: str,
        locale: str,
        engine: str,
        **extra: Any,
    ) -> str | None:
        if engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
            return get_string("chrome_engine_unsupported", reason=reason)
        if pm.add_profile(name, proxy, os_type, timezone, locale, engine, **extra):
            log(get_string("created_profile", name=name))
            if engine == "chrome":
                chrome_prefetch.start(log)
            refresh()
            return None
        return get_string("profile_exists")

    open_profile_dialog(page, ps, on_save, profile_manager=pm)
