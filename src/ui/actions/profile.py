from collections.abc import Callable
from functools import wraps
from typing import Any

import flet as ft

from ...core.strings import get_string
from ...interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ...services.browser import chrome_prefetch
from ...services.profile.manager import ProfileStoreChangedError
from ..dialogs import open_confirm_dialog, open_profile_dialog


def _store_conflict(on_save):
    @wraps(on_save)
    def guarded(*args, **kwargs):
        try:
            return on_save(*args, **kwargs)
        except (ProfileStoreChangedError, OSError) as exc:
            return str(exc) or "Profile/store is busy; refresh and retry"

    return guarded


def delete_profile(
    page: ft.Page,
    name: str,
    pm: IProfileManager,
    log: Callable[[str], None],
    refresh: Callable[[], None],
) -> None:
    def do_delete() -> None:
        try:
            pm.delete_profile(name)
        except (ProfileStoreChangedError, OSError) as exc:
            log(str(exc) or "Profile/store is busy; refresh and retry")
            refresh()
            return
        log(get_string("deleted_profile", name=name))
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
        if pm.update_profile(
            original,
            new_name,
            new_proxy,
            new_os,
            timezone,
            locale,
            **extra,
        ):
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
