import asyncio
import os
import sys
import time
from collections.abc import Callable

import flet as ft

from ..core.config import DATA_DIR, PREFERENCES_FILE
from ..core.container import Container
from ..core.logging import get_logger
from ..core.strings import current_language, get_string, localize_launch_error, set_language
from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from ..services.browser import fingerprint
from ..utils.totp import generate_totp
from . import preferences
from .components import (
    build_content_area,
    build_empty_state,
    build_profile_card,
    build_sidebar,
    build_ui_refs,
    rebuild_bulk_bar,
)
from .components.bulk_bar import layout_bulk_bar
from .components.profile_card import ProfileRow, build_header_row
from .components.sidebar import build_filter_buttons
from .dialogs.appearance import open_appearance_dialog
from .dialogs.cores import open_core_manager
from .dialogs.extensions import open_extensions_dialog
from .dialogs.mcp_guide import open_mcp_guide
from .dialogs.proxy_pool import open_proxy_pool_dialog
from .dialogs.warming import open_warming_dialog
from .handlers import AppHandlers
from .refs import UIRefs
from .state import ITEMS_PER_PAGE, PAGE_SIZE_OPTIONS, AppState
from .theme import (
    COLORS,
    apply_page_theme,
    configure_page,
    current_accent_color,
    current_theme_mode,
    refresh_styles,
    set_accent_color,
    set_theme_mode,
)
from .theme.colors import FONT_DATA, FONT_SEMIBOLD
from .theme.page import ASSETS_DIR

logger = get_logger("app")


class App:
    _SEARCH_DEBOUNCE_S = 0.15
    _WINDOW_SAVE_DELAY_S = 0.5

    def __init__(self, container: Container | None = None) -> None:
        c = container or Container()
        self.pm: IProfileManager = c.profile_manager
        self.bl: IBrowserLauncher = c.browser_launcher
        self.ps: IProxyService = c.proxy_service
        self.proxy_pool = c.proxy_pool
        self.extensions = c.extension_service
        self.warming = c.warming_scheduler
        self.state = AppState()
        self.page: ft.Page | None = None
        self._reconcile_started = False
        self.refs: UIRefs | None = None
        self.filter = "all"
        self.preferences_file = PREFERENCES_FILE
        self._saved_preferences = preferences.Preferences()
        self._window_save_seq = 0
        self._window_save_pending = False
        self._live_since: dict[str, str] = {}
        self._feedback_until: dict[tuple[str, str], float] = {}
        self._toast_until: float = 0.0
        self._toast_message = ""
        self._toast_text = ft.Text("", size=12, font_family=FONT_SEMIBOLD)
        self._toast_banner = ft.Container(visible=False)
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        self._profile_table_width: float | None = None
        self._refresh_warming_dialog: Callable[[], None] | None = None
        self._row_cache: dict[str, tuple[tuple, ProfileRow]] = {}
        self._header_cache_key: tuple | None = None
        self._filter_cache_key: tuple | None = None
        self._search_seq: int = 0
        self._sidebar_extension_badge = ft.Text("0")
        self._total_count_text = ft.Text("", size=12, font_family=FONT_SEMIBOLD)
        self._footer_proxy_text = ft.Text("", size=11.5, font_family=FONT_DATA)
        self._footer_warming_text = ft.Text("", size=11.5, font_family=FONT_DATA)
        c.event_bus.subscribe(self.state.schedule_refresh)
        self.h = AppHandlers(
            pm=self.pm,
            bl=self.bl,
            ps=self.ps,
            state=self.state,
            get_page=lambda: self.page,
            get_refs=lambda: self.refs,
            log_fn=self._log,
            refresh_fn=self._refresh_profiles,
            get_page_profiles=self._get_page_profiles,
        )

    def run(self) -> None:
        if sys.platform == "darwin":
            os.environ.setdefault("FLET_NO_IMPELLER", "true")
        ft.run(self._main, assets_dir=ASSETS_DIR)

    def _main(self, page: ft.Page) -> None:
        self._refresh_warming_dialog = None
        self._row_cache = {}
        self._header_cache_key = None
        self._filter_cache_key = None
        self._search_seq = 0
        self.page = page
        saved = self._apply_preferences()
        configure_page(page, saved.window)
        page.window.on_event = self._on_window_event
        fp = ft.FilePicker()
        page.services.append(fp)
        self.clipboard = ft.Clipboard()
        page.services.append(self.clipboard)
        self._file_picker = fp
        self.warming.start_daemon(
            self.pm,
            self.bl,
            log_fn=self._log,
            on_update=self.state.schedule_refresh,
        )
        self._build_ui()
        self.state._last_running_snapshot = self.bl.running_profile_names()
        if not self._reconcile_started:
            self._reconcile_started = True
            page.run_task(self._ui_reconcile_loop)

    def _build_ui(self) -> None:
        """(Re)build every control; strings and theme colors are read at build time."""
        assert self.page is not None
        self._row_cache = {}
        self._header_cache_key = None
        self._filter_cache_key = None
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        refresh_styles()
        apply_page_theme(self.page)
        self.refs = build_ui_refs(
            pm=self.pm,
            on_change_page=self._change_page,
            file_picker=self._file_picker,
            on_search=self._on_search,
            on_go_page=self._go_to_page,
            on_change_page_size=self._on_change_page_size,
            on_toggle_running_filter=self._toggle_running_filter,
            initial_page_size=self.state.items_per_page,
        )
        if self.state.search_query:
            self.refs.search_field.value = self.state.search_query
        self._filter_column = ft.Column(spacing=2)
        self._header_slot = ft.Container()
        self._total_count_text = ft.Text(
            get_string("footer_total", count=len(self.pm.profiles)),
            size=12,
            font_family=FONT_SEMIBOLD,
            color=COLORS["text_main"],
        )
        proxy_count = len(self.proxy_pool.list_entries())
        warming_count = len(self.warming.list_tasks())
        self._sidebar_proxy_badge = ft.Text(str(proxy_count))
        self._sidebar_extension_badge = ft.Text(str(len(self.extensions.list_entries())))
        self._sidebar_warming_badge = ft.Text(str(warming_count))
        self._footer_proxy_text = ft.Text(
            get_string("footer_proxy_pool", count=proxy_count),
            size=11.5,
            font_family=FONT_DATA,
            color=COLORS["text_sub"],
        )
        self._footer_warming_text = ft.Text(
            get_string("footer_warming_tasks", count=warming_count),
            size=11.5,
            font_family=FONT_DATA,
            color=COLORS["text_sub"],
        )
        toast = self._display_toast()
        self._toast_text = ft.Text(
            toast,
            expand=True,
            size=12,
            font_family=FONT_SEMIBOLD,
            color=COLORS["live_ink"],
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
            tooltip=toast or None,
        )
        self._toast_banner = ft.Container(
            key="action_toast_banner",
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=8,
            bgcolor=COLORS["live_tint"],
            border=ft.Border.all(1, COLORS["live"]),
            content=ft.Row(
                tight=True,
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=15, color=COLORS["live"]),
                    self._toast_text,
                ],
            ),
        )
        self._set_toast_visible(
            time.monotonic() < self._toast_until and bool(self._toast_text.value)
        )
        self.page.title = get_string("window_title")
        self.page.controls.clear()
        self.page.add(self._build_root_layout(self.refs))
        self._restore_log()
        self._refresh_profiles()

    def _restore_log(self) -> None:
        """Refill a freshly built log pane (flush_log only returns new lines)."""
        r = self.refs
        assert r is not None
        lines = self.state.get_all_log_lines()[-6:]
        r.log_text.value = "\n".join(lines)
        r.log_column.height = max(72, len(lines) * 18 + 20)
        r.log_column.visible = bool(lines) and not self.state.log_collapsed
        r.log_toggle_btn.icon = (
            ft.Icons.KEYBOARD_ARROW_RIGHT
            if self.state.log_collapsed
            else ft.Icons.KEYBOARD_ARROW_DOWN
        )

    def _apply_preferences(self) -> preferences.Preferences:
        saved = preferences.load(self.preferences_file)
        if saved.language:
            set_language(saved.language)
        if saved.theme:
            set_theme_mode(saved.theme)
        if saved.accent:
            set_accent_color(saved.accent)
        if saved.page_size:
            self.state.items_per_page = saved.page_size
        self._saved_preferences = saved
        return saved

    def _window_bounds(self) -> preferences.Bounds | None:
        window = self.page.window if self.page is not None else None
        if window is None or window.full_screen or window.maximized or window.minimized:
            return self._saved_preferences.window
        bounds = (window.left, window.top, window.width, window.height)
        return preferences.valid_bounds(bounds) or self._saved_preferences.window

    def _save_preferences(self) -> None:
        current = preferences.Preferences(
            language=current_language(),
            theme=current_theme_mode(),
            accent=current_accent_color(),
            page_size=self.state.items_per_page,
            window=self._window_bounds(),
        )
        if current != self._saved_preferences and preferences.save(self.preferences_file, current):
            self._saved_preferences = current

    def _on_window_event(self, e: ft.WindowEvent) -> None:
        if self.page is None or e.type not in (
            ft.WindowEventType.MOVED,
            ft.WindowEventType.RESIZED,
        ):
            return
        # The first event of a burst is written at once, so quitting right after a move keeps
        # it; the rest are coalesced into one write once the window settles.
        if not self._window_save_pending:
            self._save_preferences()
        self._window_save_pending = True
        self._window_save_seq += 1
        self.page.run_task(self._save_window_later, self._window_save_seq)

    async def _save_window_later(self, seq: int) -> None:
        await asyncio.sleep(self._WINDOW_SAVE_DELAY_S)
        if seq == self._window_save_seq:
            self._window_save_pending = False
            self._save_preferences()

    def _set_language(self, code: str) -> None:
        if code == current_language():
            return
        set_language(code)
        self.state.search_query = ""
        self._save_preferences()
        self._build_ui()

    def _set_theme(self, mode: str) -> None:
        if mode == current_theme_mode():
            return
        set_theme_mode(mode)
        self._save_preferences()
        self._build_ui()

    def _set_accent(self, preset: str) -> None:
        if preset == current_accent_color():
            return
        set_accent_color(preset)
        self._save_preferences()
        self._build_ui()

    def _open_appearance(self, _: object = None) -> None:
        if not self.page:
            return
        open_appearance_dialog(
            self.page,
            on_language=self._set_language,
            on_theme=self._set_theme,
            on_accent=self._set_accent,
        )

    def _trigger_feedback(self, name: str, key: str, toast_msg: str) -> None:
        self._feedback_until[(name, key)] = time.monotonic() + 2.0
        self._log(toast_msg)
        self._refresh_profiles()

    def _active_feedback_for(self, name: str, now: float) -> set[str]:
        return {
            key
            for (prof_name, key), until in self._feedback_until.items()
            if prof_name == name and until > now
        }

    def _open_proxy_pool(self, _: object = None) -> None:
        if not self.page:
            return
        selected = sorted(self.state.selected_names())
        open_proxy_pool_dialog(
            self.page,
            self.proxy_pool,
            self.ps,
            self.pm,
            target_profile_names=selected or None,
            log_fn=self._log,
            refresh_fn=self._refresh_profiles,
        )

    def _open_warming(self, _: object = None) -> None:
        if not self.page:
            return
        selected = sorted(self.state.selected_names())

        def clear_dialog_refresh() -> None:
            if self._refresh_warming_dialog is refresh:
                self._refresh_warming_dialog = None

        refresh = open_warming_dialog(
            self.page,
            self.warming,
            self.pm,
            self.bl,
            target_profile_names=selected or None,
            log_fn=self._log,
            refresh_fn=self._refresh_profiles,
            on_dismiss=clear_dialog_refresh,
        )
        self._refresh_warming_dialog = refresh

    def _open_extensions(self, _: object = None) -> None:
        if not self.page:
            return
        open_extensions_dialog(
            self.page,
            self.extensions,
            self.pm,
            file_picker=getattr(self, "_file_picker", None),
            log_fn=self._log,
            refresh_fn=self._refresh_profiles_and_extensions,
        )

    def _refresh_profiles_and_extensions(self) -> None:
        self._sidebar_extension_badge.value = str(len(self.extensions.list_entries()))
        self._refresh_profiles()

    def _build_root_layout(self, r: UIRefs) -> ft.Row:
        sidebar = build_sidebar(
            r.stats_text,
            r.running_text,
            r.log_text,
            r.log_column,
            r.log_toggle_btn,
            on_new_profile=lambda _: self.h.open_add_dialog(),
            on_import=self.h.on_import,
            on_export=lambda _: self.h.on_export_open(),
            on_mcp=lambda _: open_mcp_guide(self.page, self.clipboard),
            on_open_cores=lambda _: open_core_manager(self.page, self.pm, self.bl),
            on_toggle_log=lambda _: self.h.toggle_log(),
            on_fullscreen_log=lambda _: self.h.open_log_fullscreen(),
            filter_column=self._filter_column,
            language=current_language(),
            on_language=self._set_language,
            on_open_proxy_pool=self._open_proxy_pool,
            on_open_extensions=self._open_extensions,
            on_open_warming=self._open_warming,
            on_toggle_theme=self._set_theme,
            proxy_pool_count=self._sidebar_proxy_badge,
            extension_count=self._sidebar_extension_badge,
            warming_task_count=self._sidebar_warming_badge,
        )
        initial_table_width = self._profile_table_width
        if initial_table_width is None and self.page is not None:
            win_w = getattr(self.page, "width", None) or getattr(
                getattr(self.page, "window", None), "width", None
            )
            if isinstance(win_w, (int, float)) and win_w > 320:
                initial_table_width = max(600.0, float(win_w) - 252 - 48 - 2)
                self._profile_table_width = initial_table_width
        if initial_table_width is not None:
            layout_bulk_bar(r.bulk_bar, initial_table_width)
        content = build_content_area(
            r.content_subtitle,
            r.profile_list_area,
            r.prev_btn,
            r.next_btn,
            r.page_label,
            r.search_field,
            header_slot=self._header_slot,
            on_open_warming=self._open_warming,
            on_open_proxy_pool=self._open_proxy_pool,
            on_import=self.h.on_import,
            on_export=lambda _: self.h.on_export_open(),
            on_refresh=lambda _: self._refresh_profiles_and_extensions(),
            on_toggle_theme=self._set_theme,
            on_open_appearance=self._open_appearance,
            toast_banner=self._toast_banner,
            total_count_text=self._total_count_text,
            footer_proxy_text=self._footer_proxy_text,
            footer_warming_text=self._footer_warming_text,
            first_btn=r.first_btn,
            last_btn=r.last_btn,
            page_size_dropdown=r.page_size_dropdown,
            running_filter_btn=r.running_filter_btn,
            initial_table_width=initial_table_width,
            on_table_resize=self._remember_table_width,
        )
        return ft.Row(
            expand=True,
            spacing=0,
            controls=[
                sidebar,
                content,
            ],
        )

    def _remember_table_width(self, width: float) -> None:
        self._profile_table_width = width
        if self.refs is not None:
            layout_bulk_bar(self.refs.bulk_bar, width)

    def _on_search(self, query: str) -> None:
        self.state.search_query = query.strip().lower()
        self.state.current_page = 1
        seq = getattr(self, "_search_seq", 0) + 1
        self._search_seq = seq
        if isinstance(self.page, ft.Page):
            ft.context.disable_auto_update()
            self.page.run_task(self._debounced_search, seq)
            return
        self._refresh_profiles()

    async def _debounced_search(self, seq: int) -> None:
        await asyncio.sleep(self._SEARCH_DEBOUNCE_S)
        if seq == getattr(self, "_search_seq", 0) and self.page:
            self._refresh_profiles()

    def _on_filter(self, fid: str, *, clear_search: bool = False) -> None:
        self.filter = fid
        self.state.current_page = 1
        # Primary sidebar filters clear stale search so the table matches the badge count;
        # the toolbar Running toggle keeps the active search query (parity with Web).
        if clear_search:
            if self.state.search_query:
                self.state.search_query = ""
            if self.refs and self.refs.search_field is not None and self.refs.search_field.value:
                self.refs.search_field.value = ""
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        self._refresh_profiles()

    def _toggle_running_filter(self) -> None:
        self._on_filter("all" if self.filter == "running" else "running", clear_search=False)

    def _on_change_page_size(self, size: int) -> None:
        if size not in PAGE_SIZE_OPTIONS or size == self.state.items_per_page:
            return
        self.state.items_per_page = size
        self.state.current_page = 1
        self._save_preferences()
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        self._refresh_profiles()

    def _go_to_page(self, target: str) -> None:
        _, _, total = self._get_page_profiles()
        self.state.current_page = 1 if target == "first" else total
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        self._refresh_profiles()

    def _filter_tests(self) -> dict:
        return {
            "all": lambda p: True,
            "running": lambda p: self.bl.is_running(p.name),
            "proxy": lambda p: bool(p.proxy),
            "direct": lambda p: not p.proxy,
            "chrome": lambda p: p.engine == "chrome",
        }

    def _get_page_profiles(self, all_profiles: list | None = None) -> tuple[list, list, int]:
        all_profiles = self.pm.list_profiles() if all_profiles is None else list(all_profiles)
        if self.filter.startswith("group:"):
            target_group = self.filter.split(":", 1)[1]
            tagged_profiles = [p for p in all_profiles if target_group in p.effective_tags]
            if tagged_profiles:
                all_profiles = tagged_profiles
            else:
                self.filter = "all"
                self.state.current_page = 1
        else:
            test = self._filter_tests().get(self.filter, lambda p: True)
            all_profiles = [p for p in all_profiles if test(p)]
        if q := self.state.search_query:
            all_profiles = [
                p
                for p in all_profiles
                if q in p.name.lower()
                or q in (p.proxy or "").lower()
                or any(q in tag.lower() for tag in p.effective_tags)
                or q in (p.account_uid or "").lower()
                or q in p.effective_recovery_email.lower()
                or q in (p.notes or "").lower()
            ]
        page_size = max(1, int(getattr(self.state, "items_per_page", ITEMS_PER_PAGE)))
        total = max(1, (len(all_profiles) + page_size - 1) // page_size)
        self.state.current_page = max(1, min(self.state.current_page, total))
        start = (self.state.current_page - 1) * page_size
        return all_profiles, all_profiles[start : start + page_size], total

    def _on_edit_field(self, name: str, field: str, row_index: int = 1) -> None:
        from ..services.profile.manager import ProfileStoreChangedError
        from ..services.warming.scheduler import WarmingTargetSaveError
        from .dialogs.profile_field import open_profile_field_dialog

        profile = self.pm.profiles.get(name)
        if not profile or not self.page:
            return

        def save(value: object) -> str | None:
            if field == "name" and value != name and self.bl.is_running(name):
                return get_string("stop_before_rename")
            # Use the latest in-process values, never a stale dialog-wide snapshot.
            current = self.pm.profiles.get(name)
            if not current:
                return get_string("update_failed")
            try:
                ok = self.pm.update_profile(
                    name,
                    str(value) if field == "name" else name,
                    str(value) if field == "proxy" else current.proxy or "",
                    current.os_type,
                    current.timezone,
                    current.locale,
                    **({field: value} if field not in ("name", "proxy") else {}),
                )
            except WarmingTargetSaveError:
                return get_string("warming_rename_save_error")
            except (ProfileStoreChangedError, OSError, ValueError) as exc:
                return str(exc) or get_string("update_failed")
            if not ok:
                return get_string("update_failed")
            target_name = str(value) if field == "name" else name
            field_label_key = {
                "display_order": "col_id",
                "name": "col_profile",
                "tags": "col_group",
                "proxy": "col_proxy",
                "recovery_email": "recovery_email_label",
                "backup_codes": "backup_codes_label",
                "notes": "notes_label",
            }.get(field, "col_profile")
            self._trigger_feedback(
                target_name,
                f"edit_{field}",
                get_string(
                    "field_saved_toast",
                    field=get_string(field_label_key),
                    name=target_name,
                ),
            )
            return None

        def copy_code(text: str) -> None:
            if not text or not self.page:
                return

            async def _copy() -> None:
                await self.clipboard.set(text)

            self.page.run_task(_copy)
            # Never log or toast raw single backup codes: logger persists to daily log files.
            self._trigger_feedback(
                name,
                "copy_backup_codes",
                get_string("copied_backup_codes_toast", name=name),
            )

        open_profile_field_dialog(
            self.page,
            profile,
            field,
            save,
            row_index=row_index,
            known_tags=sorted({tag for p in self.pm.list_profiles() for tag in p.effective_tags}),
            on_copy_code=copy_code,
        )

    def _on_copy_user(self, name: str) -> None:
        profile = self.pm.profiles.get(name)
        if not profile or not profile.account_uid or not self.page:
            return
        uid = profile.account_uid

        async def copy() -> None:
            await self.clipboard.set(uid)

        self.page.run_task(copy)
        self._trigger_feedback(
            name,
            "copy_user",
            get_string("copied_user_toast", name=name, uid=uid),
        )

    def _on_copy_recovery(self, name: str) -> None:
        profile = self.pm.profiles.get(name)
        if not profile or not profile.effective_recovery_email or not self.page:
            return
        email = profile.effective_recovery_email

        async def copy() -> None:
            await self.clipboard.set(email)

        self.page.run_task(copy)
        self._trigger_feedback(
            name,
            "copy_recovery",
            get_string("copied_recovery_toast", name=name, email=email),
        )

    def _on_copy_pass(self, name: str) -> None:
        profile = self.pm.profiles.get(name)
        if not profile or not profile.account_password or not self.page:
            return
        pw = profile.account_password

        async def _copy() -> None:
            await self.clipboard.set(pw)

        self.page.run_task(_copy)
        self._trigger_feedback(
            name,
            "copy_pass",
            get_string("copied_password", name=name),
        )

    def _on_copy_2fa(self, name: str) -> None:
        profile = self.pm.profiles.get(name)
        if not profile or not profile.two_fa_secret or not self.page:
            return
        res = generate_totp(profile.two_fa_secret)
        if not res:
            return
        code, rem = res

        async def _copy() -> None:
            await self.clipboard.set(code)

        self.page.run_task(_copy)
        self._trigger_feedback(
            name,
            "copy_2fa",
            get_string("copied_2fa", name=name, code=code, rem=rem),
        )

    def _refresh_profiles(self) -> None:
        r = self.refs
        assert r is not None
        everything = self.pm.list_profiles()
        self._update_stats()
        self._flush_log()
        all_profiles, page_profiles, total_pages = self._get_page_profiles(everything)
        page_size = max(1, int(getattr(self.state, "items_per_page", ITEMS_PER_PAGE)))
        global_positions = {profile.name: index for index, profile in enumerate(everything, 1)}

        all_names = set(self.pm.profiles)
        for stale in self.state.selected_names() - all_names:
            self.state.toggle_selection(stale)

        running = self.bl.running_profile_names()
        for name in set(self.state.failed) - (all_names - running):
            self.state.failed.pop(name, None)
        for name in running - set(self._live_since):
            self._live_since[name] = time.strftime("%H:%M")
        for name in set(self._live_since) - running:
            del self._live_since[name]

        tests = self._filter_tests()
        group_counts: dict[str, int] = {}
        for p in everything:
            for tag in p.effective_tags:
                group_counts[tag] = group_counts.get(tag, 0) + 1
        if r.search_field is not None and isinstance(
            getattr(r.search_field, "suffix", None), ft.Control
        ):
            r.search_field.suffix.visible = bool(self.state.search_query)
        filter_counts = {fid: sum(1 for p in everything if test(p)) for fid, test in tests.items()}
        filter_key = (
            self.filter,
            tuple(sorted(filter_counts.items())),
            tuple(sorted(group_counts.items())),
        )
        if filter_key != getattr(self, "_filter_cache_key", None):
            self._filter_cache_key = filter_key
            self._filter_column.controls = build_filter_buttons(
                self.filter,
                filter_counts,
                lambda fid: self._on_filter(fid, clear_search=True),
                group_counts=group_counts,
            )

        dirty_rows: list[ProfileRow] = []
        now = time.monotonic()
        self._set_toast_visible(now < self._toast_until and bool(self._toast_text.value))
        prev_cache: dict[str, tuple[tuple, ProfileRow]] = getattr(self, "_row_cache", {})
        new_cache: dict[str, tuple[tuple, ProfileRow]] = {}
        if page_profiles:
            new_controls: list[ft.Control] = []
            for p in page_profiles:
                is_loading = self.state.is_loading(p.name)
                is_running = self.bl.is_running(p.name)
                is_selected = self.state.is_selected(p.name)
                is_stopping = self.state.is_stopping(p.name)
                device = self._device_label(p.name)
                since = self._live_since.get(p.name, "")
                failed = self.state.failed.get(p.name, "")
                row_index = global_positions[p.name]
                active_fb = self._active_feedback_for(p.name, now)
                row_key = (
                    p.name,
                    p.proxy,
                    p.os_type,
                    p.timezone,
                    p.engine,
                    p.group,
                    tuple(p.tags or ()),
                    p.display_order,
                    p.account_uid,
                    bool(p.account_password),
                    bool(p.two_fa_secret),
                    p.recovery_email,
                    p.backup_codes,
                    p.notes,
                    is_loading,
                    is_running,
                    is_selected,
                    device,
                    since,
                    failed,
                    row_index,
                    frozenset(active_fb),
                    is_stopping,
                )
                cached = prev_cache.get(p.name)
                if cached is not None and cached[0] == row_key:
                    row = cached[1]
                    if self._profile_table_width is not None and row.set_layout_width(
                        self._profile_table_width
                    ):
                        dirty_rows.append(row)
                else:
                    row = build_profile_card(
                        p,
                        is_loading,
                        is_running,
                        self.h.on_launch,
                        self.h.on_edit,
                        self.h.on_delete,
                        is_selected=is_selected,
                        on_select=self.h.on_toggle_select,
                        on_cookies=self.h.on_cookies,
                        on_copy_user=self._on_copy_user,
                        on_copy_recovery=self._on_copy_recovery,
                        on_edit_field=self._on_edit_field,
                        on_copy_pass=self._on_copy_pass,
                        on_copy_2fa=self._on_copy_2fa,
                        device=device,
                        since=since,
                        failed=failed,
                        row_index=row_index,
                        active_feedback=active_fb,
                        layout_width=self._profile_table_width,
                        is_stopping=is_stopping,
                    )
                new_cache[p.name] = (row_key, row)
                new_controls.append(row)
        else:
            new_controls = [self._build_empty_or_no_results()]
        self._row_cache = new_cache
        old_controls = r.profile_list_area.controls
        if len(old_controls) != len(new_controls) or any(
            a is not b for a, b in zip(old_controls, new_controls, strict=True)
        ):
            r.profile_list_area.controls = new_controls
        rebuild_bulk_bar(
            r.bulk_bar,
            self.state,
            all_profiles,
            {
                "launch": self.h.on_bulk_launch,
                "stop": self.h.on_bulk_stop,
                "delete": self.h.on_bulk_delete,
                "select_page": self.h.on_select_all_page,
                "deselect_page": self.h.on_deselect_page,
                "clear": self.h.on_clear_selection,
            },
        )
        layout_bulk_bar(r.bulk_bar, self._profile_table_width)
        names = [p.name for p in page_profiles]
        all_sel = bool(names) and all(self.state.is_selected(n) for n in names)
        any_sel = any(self.state.is_selected(n) for n in names)
        bulk_visible = bool(r.bulk_bar.visible)
        header_key = (bool(page_profiles), all_sel, any_sel, bulk_visible)
        want_header = bool(page_profiles) or bulk_visible
        header_matches = header_key == getattr(self, "_header_cache_key", None) and (
            not want_header
            or (
                self._header_slot.content is not None
                and (
                    not isinstance(self._header_slot.content, ProfileRow)
                    if bulk_visible
                    else isinstance(self._header_slot.content, ProfileRow)
                )
            )
        )
        if header_matches:
            if (
                not bulk_visible
                and page_profiles
                and self._profile_table_width is not None
                and isinstance(self._header_slot.content, ProfileRow)
                and self._header_slot.content.set_layout_width(
                    self._profile_table_width, header=True
                )
            ):
                dirty_rows.append(self._header_slot.content)
        else:
            self._header_cache_key = header_key
            self._header_slot.content = (
                build_header_row(
                    all_sel,
                    any_sel,
                    (
                        lambda select: (
                            self.h.on_select_all_page() if select else self.h.on_deselect_page()
                        )
                    )
                    if page_profiles
                    else None,
                    layout_width=self._profile_table_width,
                    bulk_actions=r.bulk_bar if bulk_visible else None,
                )
                if want_header
                else None
            )
        self._set_subtitle(r.content_subtitle)
        self._total_count_text.value = get_string("footer_total", count=len(all_profiles))
        proxy_count = len(self.proxy_pool.list_entries())
        self._sidebar_proxy_badge.value = str(proxy_count)
        self._footer_proxy_text.value = get_string("footer_proxy_pool", count=proxy_count)
        warming_count = len(self.warming.list_tasks())
        self._sidebar_warming_badge.value = str(warming_count)
        self._footer_warming_text.value = get_string("footer_warming_tasks", count=warming_count)
        r.page_label.value = get_string(
            "page_of",
            current=self.state.current_page,
            total=total_pages,
        )
        at_first = self.state.current_page <= 1
        at_last = self.state.current_page >= total_pages
        r.prev_btn.disabled = at_first
        r.next_btn.disabled = at_last
        if r.first_btn is not None:
            r.first_btn.disabled = at_first
        if r.last_btn is not None:
            r.last_btn.disabled = at_last
        if r.page_size_dropdown is not None:
            r.page_size_dropdown.value = str(page_size)
        if r.running_filter_btn is not None:
            is_run_active = self.filter == "running"
            r.running_filter_btn.content = ft.Text(
                get_string("toolbar_running_filter", count=len(running)),
                size=13,
                font_family=FONT_SEMIBOLD if is_run_active else None,
                color=COLORS["live_ink"] if is_run_active else COLORS["text_main"],
            )
            r.running_filter_btn.icon = (
                ft.Icons.PLAY_CIRCLE_FILL_ROUNDED
                if is_run_active or running
                else ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED
            )
            r.running_filter_btn.icon_color = (
                COLORS["live"] if is_run_active or running else COLORS["text_sub"]
            )
            r.running_filter_btn.style = ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=8),
                side=ft.BorderSide(1, COLORS["live"] if is_run_active else COLORS["rule_strong"]),
                bgcolor=COLORS["live_tint"] if is_run_active else COLORS["card_bg"],
                color=COLORS["live_ink"] if is_run_active else COLORS["text_main"],
            )
        try:
            self._safe_update()
        finally:
            for row in dirty_rows:
                row.mark_layout_clean()

    @staticmethod
    def _device_label(name: str) -> str:
        info = fingerprint.summary(os.path.join(DATA_DIR, name))
        if not info:
            return ""
        cpu = f"{info['hardware_concurrency']} CPU"
        return f"{info['screen']} · {cpu}" if info.get("screen") else cpu

    def _build_empty_or_no_results(self) -> ft.Control:
        if self.pm.profiles:
            return ft.Container(
                padding=ft.Padding.symmetric(vertical=56, horizontal=24),
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Text(
                            get_string("no_search_results", query=self.state.search_query)
                            if self.state.search_query
                            else get_string("no_filter_results"),
                            size=14,
                            color=COLORS["text_sub"],
                        ),
                        ft.OutlinedButton(
                            get_string("clear_filters"),
                            on_click=lambda _: self._clear_filters(),
                        ),
                    ],
                ),
            )
        return build_empty_state(lambda _: self.h.open_add_dialog())

    def _clear_filters(self) -> None:
        self.filter = "all"
        self.state.search_query = ""
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        if self.refs and self.refs.search_field is not None:
            self.refs.search_field.value = ""
        self._refresh_profiles()

    def _change_page(self, delta: int) -> None:
        self.state.current_page += delta
        self._refresh_profiles()

    def _set_subtitle(self, text: ft.Text) -> None:
        c, r = len(self.pm.profiles), self.bl.running_count()
        head = get_string("profiles_configured", count=c) + " · "
        text.value = None
        text.spans = (
            [
                ft.TextSpan(head),
                ft.TextSpan(str(r), ft.TextStyle(color=COLORS["live"], font_family=FONT_SEMIBOLD)),
                ft.TextSpan(" " + get_string("profiles_running_suffix")),
            ]
            if r
            else [ft.TextSpan(head + get_string("profiles_none_running"))]
        )

    def _update_stats(self) -> None:
        r = self.refs
        if r:
            cnt = self.bl.running_count()
            r.stats_text.value = get_string(
                "total_profiles",
                count=len(self.pm.profiles),
            )
            r.running_text.value = (
                get_string("browsers_running", count=cnt)
                if cnt
                else get_string("no_active_sessions")
            )

    def _set_toast_visible(self, visible: bool) -> None:
        self._toast_banner.visible = visible
        if self.refs is not None:
            self.refs.content_subtitle.visible = not visible

    def _display_toast(self) -> str:
        message = self._toast_message
        return localize_launch_error(message) if "LAUNCH_FAILED:" in message else message

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.refs is not None:
            self._toast_message = message
            visible_message = self._display_toast()
            self._toast_until = time.monotonic() + 2.4
            self._toast_text.value = visible_message
            self._toast_text.tooltip = visible_message
            self._toast_text.color = COLORS["live_ink"]
            self._set_toast_visible(True)
            for prefix in ("Updated:", "Đã cập nhật:", "Updated profile:", "Đã cập nhật hồ sơ:"):
                if message.startswith(prefix):
                    if "->" in message:
                        target = message.split("->")[-1].strip()
                    elif "→" in message:
                        target = message.split("→")[-1].strip()
                    else:
                        target = message[len(prefix) :].strip()
                    if target:
                        self._feedback_until[(target, "edit_profile")] = time.monotonic() + 2.0
        if self.state.add_log(message):
            self.state.schedule_refresh()

    def _flush_log(self) -> None:
        text = self.state.flush_log()
        if text is not None and self.refs:
            lines = text.split("\n")
            sidebar_lines = lines[-6:]
            self.refs.log_text.value = "\n".join(sidebar_lines)
            self.refs.log_column.height = max(72, len(sidebar_lines) * 18 + 20)
            self.refs.log_column.visible = bool(sidebar_lines) and not self.state.log_collapsed

    def _safe_update(self) -> None:
        if not self.page:
            return
        try:
            with self.state._ui_update_lock:
                self.page.update()
        except Exception as e:
            logger.error("Error updating UI: %s", e)

    def _update_toast_only(self) -> None:
        if not self.page:
            return
        if (
            isinstance(self.page, ft.Page)
            and getattr(self._toast_banner, "_parent", None) is not None
        ):
            try:
                with self.state._ui_update_lock:
                    self._toast_banner.update()
                    if (
                        self.refs is not None
                        and getattr(self.refs.content_subtitle, "_parent", None) is not None
                    ):
                        self.refs.content_subtitle.update()
                return
            except Exception:
                pass
        self._safe_update()

    async def _ui_reconcile_loop(self) -> None:
        while self.page:
            try:
                now = time.monotonic()
                expired_keys = [k for k, until in self._feedback_until.items() if until <= now]
                for k in expired_keys:
                    del self._feedback_until[k]
                toast_expired = self._toast_banner.visible and now >= self._toast_until
                if toast_expired:
                    self._set_toast_visible(False)
                running_now = self.bl.running_profile_names()
                changed = running_now != self.state._last_running_snapshot
                if changed:
                    self.state._last_running_snapshot = running_now
                refresh_requested = self.state.consume_refresh()
                if refresh_requested:
                    if self._refresh_warming_dialog is not None:
                        self._refresh_warming_dialog()
                    self._refresh_profiles_and_extensions()
                elif changed or expired_keys:
                    self._refresh_profiles()
                elif toast_expired:
                    self._update_toast_only()
            except Exception as e:
                logger.error("Error in UI reconcile loop: %s", e)
            await asyncio.sleep(0.12)
