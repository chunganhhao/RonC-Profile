from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import pathlib
import shutil
import threading
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ...core.config import (
    DATA_DIR,
    PROFILES_FILE,
    PendingDeleteEntry,
    PendingProfileDeleteError,
    PinnedStorageRoot,
    StorageIdentity,
    StorageVolumeUnmountedError,
    ensure_data_dir,
    find_pending_delete_entries,
    format_pending_delete_message,
    is_reserved_profile_name,
    is_valid_import_tx,
    require_data_dir_mounted,
    require_no_pending_delete,
    require_same_storage_root,
    storage_root_identity,
)
from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import (
    CANVAS_MODES,
    ENGINES,
    WEBRTC_MODES,
    Profile,
    normalize_backup_codes,
    normalize_tags,
    split_recovery_and_notes,
    valid_display_order,
)
from ...utils.totp import clean_totp_secret
from ...utils.validation import (
    normalize_start_url,
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_timezone,
)
from ..browser import fingerprint as fp_service
from ..browser.lease import native_profile_guard, profile_lease, store_lease
from .transfer import export_to_zip, import_from_zip, read_profile_meta

_ORIG_IMPORT_FROM_ZIP = import_from_zip

if TYPE_CHECKING:
    from ..warming.scheduler import WarmingSchedulerService

logger = get_logger("profile.manager")


class ProfileImportConflict(str):
    code = "profile_exists"

    def __new__(cls, name: str):
        result = super().__new__(cls, f"Profile '{name}' already exists")
        result.name = name
        return result


class ProfileStoreChangedError(RuntimeError):
    pass


def _engine_or_default(value: object) -> str:
    return value if value in ENGINES else "camoufox"


def _webrtc_or_default(value: object) -> str:
    return value if value in WEBRTC_MODES else "proxy"


def _canvas_or_default(value: object) -> str:
    return value if value in CANVAS_MODES else "auto"


def _replace(src: pathlib.Path, dst: pathlib.Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows it fails with a sharing violation
    while another process (antivirus, indexer, a reader) has the target open."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


def _restore_chrome_persona_file(
    profile_dir: pathlib.Path,
    prev_bytes: bytes | None,
    dir_existed: bool,
    *,
    pinned: PinnedStorageRoot | None = None,
    rel_dir: tuple[str, ...] | None = None,
) -> None:
    persona_path = profile_dir / fp_service.CHROME_PERSONA_FILE
    if pinned is not None and rel_dir is not None:
        if prev_bytes is None:
            with contextlib.suppress(OSError):
                pinned.unlink_file(rel_dir, fp_service.CHROME_PERSONA_FILE)
        else:
            with contextlib.suppress(OSError):
                if pinned.read_file_bytes(rel_dir, fp_service.CHROME_PERSONA_FILE) != prev_bytes:
                    pinned.write_file_atomic(rel_dir, fp_service.CHROME_PERSONA_FILE, prev_bytes)
        fp_service._summary_cache.pop(str(persona_path), None)
        if not dir_existed:
            pinned.rmdir_if_empty(rel_dir)
        return
    if prev_bytes is None:
        with contextlib.suppress(OSError):
            persona_path.unlink(missing_ok=True)
    else:
        with contextlib.suppress(OSError):
            persona_path.write_bytes(prev_bytes)
    fp_service._summary_cache.pop(str(persona_path), None)
    if not dir_existed:
        with contextlib.suppress(OSError):
            profile_dir.rmdir()


class ProfileManager:
    def __init__(self, warming_scheduler: WarmingSchedulerService | None = None) -> None:
        self._lock = threading.RLock()
        self._warming_scheduler = warming_scheduler
        self.profiles: dict[str, Profile] = {}
        self._import_tx: dict[str, str] = {}
        self._store_digest = None
        self._load_profiles()
        ensure_data_dir(DATA_DIR)

    @property
    def profiles_file(self) -> str:
        return PROFILES_FILE

    def _find_by_casefold(self, name: str) -> str | None:
        """Return the existing profile key that case-folds to *name*, or None.

        Used to enforce case-insensitive uniqueness across all platforms so that
        a profiles.json exported on Linux (case-sensitive FS) can be imported on
        Windows/macOS (case-insensitive FS) without data-dir collisions.
        """
        lower = name.casefold()
        return next((k for k in self.profiles if k.casefold() == lower), None)

    def _data_path(self, name: str) -> str:
        return os.path.join(DATA_DIR, name)

    def _pin_storage_root(self, expected: StorageIdentity | None = None) -> StorageIdentity:
        require_data_dir_mounted(DATA_DIR)
        if expected is None:
            return storage_root_identity(DATA_DIR)
        return require_same_storage_root(DATA_DIR, expected)

    def _load_profiles(self) -> None:
        self._import_tx.clear()
        if pathlib.Path(PROFILES_FILE).exists():
            try:
                with pathlib.Path(PROFILES_FILE).open("rb") as f:
                    raw = f.read()
                    self._store_digest = hashlib.sha256(raw).hexdigest()
                    data = json.loads(raw)
                    for name, p_data in data.items():
                        if (
                            not validate_profile_name(name, allow_reserved=True)[0]
                            or p_data.get("name") != name
                        ):
                            logger.warning("Skipping profile with unsafe name: %r", name)
                            continue
                        try:
                            tags = normalize_tags(p_data.get("tags", []))
                        except ValueError:
                            tags = []  # A malformed optional field must not hide legacy profiles.
                        ext_ids = p_data.get("extension_ids")
                        clean_ext_ids = (
                            [str(x).strip() for x in ext_ids if isinstance(x, str) and x.strip()]
                            if isinstance(ext_ids, list)
                            else []
                        )
                        try:
                            backup_codes = (
                                normalize_backup_codes(p_data.get("backup_codes")) or None
                            )
                        except ValueError:
                            backup_codes = None
                        clean_data = {
                            "name": p_data.get("name"),
                            "proxy": p_data.get("proxy"),
                            "os_type": p_data.get(
                                "os_type",
                                p_data.get("config", {}).get("os", "windows"),
                            ),
                            "timezone": valid_or_none(p_data.get("timezone"), validate_timezone),
                            "locale": valid_or_none(p_data.get("locale"), validate_locale),
                            "engine": _engine_or_default(p_data.get("engine")),
                            "core_version": p_data.get("core_version")
                            if isinstance(p_data.get("core_version"), str)
                            else None,
                            "group": tags[0]
                            if tags
                            else (p_data.get("group") or "").strip() or None,
                            "tags": tags,
                            "extension_ids": clean_ext_ids,
                            "display_order": p_data.get("display_order")
                            if valid_display_order(p_data.get("display_order"))
                            else None,
                            "account_uid": (p_data.get("account_uid") or "").strip() or None,
                            "account_password": p_data.get("account_password") or None,
                            "two_fa_secret": clean_totp_secret(p_data.get("two_fa_secret")) or None,
                            "recovery_email": split_recovery_and_notes(
                                p_data.get("recovery_email"), p_data.get("notes")
                            )[0]
                            or None,
                            "backup_codes": backup_codes,
                            "notes": split_recovery_and_notes(
                                p_data.get("recovery_email"), p_data.get("notes")
                            )[1]
                            or None,
                            "start_url": normalize_start_url(p_data.get("start_url")) or None,
                            "webrtc_mode": _webrtc_or_default(p_data.get("webrtc_mode")),
                            "canvas_mode": _canvas_or_default(p_data.get("canvas_mode")),
                        }
                        self.profiles[name] = Profile(**clean_data)
                        tx_token = p_data.get("_import_tx")
                        if is_valid_import_tx(tx_token):
                            self._import_tx[name] = tx_token
                logger.info("Loaded %d profiles", len(self.profiles))
            except Exception as e:
                logger.exception("Error loading profiles: %s", e)

    def _check_store_current(self) -> None:
        path = pathlib.Path(PROFILES_FILE)
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if current != self._store_digest:
            self.profiles.clear()
            self._import_tx.clear()
            self._store_digest = None
            self._load_profiles()
            raise ProfileStoreChangedError(
                "Profile metadata changed in another process; reload before retrying"
            )

    def _serialized_store_rows(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for name, profile in self.profiles.items():
            row = profile.to_dict()
            tx_token = self._import_tx.get(name)
            if tx_token:
                row["_import_tx"] = tx_token
            rows[name] = row
        return rows

    def save_profiles(self, *, strict: bool = False) -> None:
        """Atomic write: write to a temp file then os.replace."""
        path = pathlib.Path(PROFILES_FILE)
        tmp = path.with_suffix(".json.tmp")
        try:
            with self._lock, store_lease(path):
                self._check_store_current()
                payload = json.dumps(self._serialized_store_rows(), indent=4)
                mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
                fd = os.open(
                    tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                with os.fdopen(fd, "wb") as out:
                    out.write(payload.encode("utf-8"))
                    out.flush()
                    os.fsync(out.fileno())
                tmp.chmod(mode)
                _replace(tmp, path)
                self._store_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            logger.debug("Profiles saved")
        except Exception as e:
            logger.exception("Error saving profiles: %s", e)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            if strict or isinstance(e, ProfileStoreChangedError):
                raise

    def add_profile(
        self,
        name: str,
        proxy: str,
        os_type: str,
        timezone: str | None = None,
        locale: str | None = None,
        engine: str = "camoufox",
        *,
        group: str | None = None,
        tags: list[str] | None = None,
        extension_ids: list[str] | None = None,
        display_order: int | None = None,
        account_uid: str | None = None,
        account_password: str | None = None,
        two_fa_secret: str | None = None,
        recovery_email: str | None = None,
        backup_codes: str | None = None,
        notes: str | None = None,
        start_url: str | None = None,
        webrtc_mode: str = "proxy",
        canvas_mode: str = "auto",
        initial_fingerprint: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock, store_lease(PROFILES_FILE):
            self._check_store_current()
            if is_reserved_profile_name(name):
                raise ValueError(get_string("validation_reserved_name", name=name))
            if self._find_by_casefold(name) is not None:
                return False
            try:
                clean_tags = normalize_tags(tags) if tags is not None else None
                clean_backup_codes = (
                    normalize_backup_codes(backup_codes) if backup_codes is not None else None
                )
            except ValueError:
                return False
            if not valid_display_order(display_order):
                return False
            if two_fa_secret and two_fa_secret.strip() and not clean_totp_secret(two_fa_secret):
                return False
            if start_url and start_url.strip() and not normalize_start_url(start_url):
                return False
            # Create directory before touching profiles.json so a mkdir failure
            # does not leave an entry with no data dir on disk.
            require_data_dir_mounted(DATA_DIR)
            try:
                ensure_data_dir(DATA_DIR)
            except StorageVolumeUnmountedError:
                raise
            except OSError as e:
                logger.warning("Could not create data dir for %s: %s", name, e)
                return False
            data_dir = self._data_path(name)
            require_no_pending_delete(
                data_dir, known_profiles=self.profiles.keys(), profiles_file=PROFILES_FILE
            )
            data_path = pathlib.Path(data_dir)
            clean_ext_ids = (
                [str(x).strip() for x in extension_ids if isinstance(x, str) and x.strip()]
                if isinstance(extension_ids, list)
                else []
            )
            originals = dict(self.profiles)
            previous = {k: replace(p) for k, p in originals.items()}
            with PinnedStorageRoot(
                DATA_DIR,
                mount_check=require_data_dir_mounted,
                identity_fn=storage_root_identity,
            ) as add_pinned:
                add_pinned.clear_resolved_import_journals(
                    (name,), profiles_file=PROFILES_FILE, strict=True
                )
                dir_existed = add_pinned.exists(name)
                built_chrome: tuple[Any, dict[str, Any] | None] | None = None
                prev_persona_bytes: bytes | None = None
                if isinstance(initial_fingerprint, dict) and engine == "chrome":
                    built_chrome = fp_service._build_chrome_persona(
                        data_dir, initial_fingerprint, os_type=os_type
                    )
                    prev_persona_bytes = add_pinned.read_file_bytes(
                        (name,), fp_service.CHROME_PERSONA_FILE
                    )
                try:
                    add_pinned.ensure_subdir((name,))
                except StorageVolumeUnmountedError:
                    raise
                except OSError as e:
                    logger.warning("Could not create data dir for %s: %s", name, e)
                    return False
                try:
                    if built_chrome is not None:
                        fp_service._write_chrome_persona(
                            data_dir, *built_chrome, pinned=add_pinned, rel_dir=(name,)
                        )
                    self.profiles[name] = Profile(
                        name=name,
                        proxy=proxy or None,
                        os_type=os_type,
                        timezone=timezone or None,
                        locale=locale or None,
                        engine=_engine_or_default(engine),
                        group=(clean_tags[0] if clean_tags else None)
                        if clean_tags is not None
                        else (group or "").strip() or None,
                        tags=clean_tags or [],
                        extension_ids=clean_ext_ids,
                        account_uid=(account_uid or "").strip() or None,
                        account_password=account_password or None,
                        two_fa_secret=clean_totp_secret(two_fa_secret) or None,
                        recovery_email=(recovery_email or "").strip() or None,
                        backup_codes=clean_backup_codes or None,
                        notes=(notes or "").strip() or None,
                        start_url=normalize_start_url(start_url) or None,
                        webrtc_mode=_webrtc_or_default(webrtc_mode),
                        canvas_mode=_canvas_or_default(canvas_mode),
                    )
                    if display_order is not None:
                        self._move_profile(name, display_order)
                    add_pinned.verify()
                    self.save_profiles(strict=True)
                except Exception as exc:
                    if not isinstance(exc, ProfileStoreChangedError):
                        for prof_name, original in originals.items():
                            original.__dict__.update(vars(previous[prof_name]))
                        self.profiles.clear()
                        self.profiles.update(originals)
                    if not isinstance(exc, StorageVolumeUnmountedError):
                        can_cleanup = True
                        try:
                            add_pinned.verify()
                        except StorageVolumeUnmountedError:
                            can_cleanup = False
                        if can_cleanup:
                            if built_chrome is not None:
                                _restore_chrome_persona_file(
                                    data_path,
                                    prev_persona_bytes,
                                    dir_existed,
                                    pinned=add_pinned,
                                    rel_dir=(name,),
                                )
                            elif not dir_existed:
                                add_pinned.rmdir_if_empty((name,))
                    raise
                if isinstance(initial_fingerprint, dict) and engine == "camoufox":
                    with contextlib.suppress(Exception):
                        fp_service.save_fingerprint(
                            data_dir,
                            os_type,
                            initial_fingerprint,
                            pinned=add_pinned,
                            rel_dir=(name,),
                        )
            logger.info("Created profile: %s", name)
            return True

    def patch_profile(self, original_name: str, **changes: Any) -> bool:
        """Resolve omitted base fields and commit under the same locks."""
        with self._lock, store_lease(PROFILES_FILE):
            self._check_store_current()
            current = self.profiles.get(original_name)
            if current is None:
                return False
            return self.update_profile(
                original_name,
                changes.pop("name", original_name),
                changes.pop("proxy", current.proxy) or "",
                changes.pop("os_type", current.os_type),
                changes.pop("timezone", current.timezone),
                changes.pop("locale", current.locale),
                **changes,
            )

    def update_profile(
        self,
        original_name: str,
        new_name: str,
        new_proxy: str,
        new_os: str,
        new_timezone: str | None = None,
        new_locale: str | None = None,
        *,
        group: str | None = None,
        tags: list[str] | None = None,
        extension_ids: list[str] | None = None,
        display_order: int | None = None,
        account_uid: str | None = None,
        account_password: str | None = None,
        two_fa_secret: str | None = None,
        recovery_email: str | None = None,
        backup_codes: str | None = None,
        notes: str | None = None,
        start_url: str | None = None,
        webrtc_mode: str | None = None,
        canvas_mode: str | None = None,
        initial_fingerprint: dict[str, Any] | None = None,
    ) -> bool:
        """Replace every field; a None/empty timezone or locale means automatic."""
        with (
            self._lock,
            store_lease(PROFILES_FILE),
            (
                profile_lease(self._data_path(original_name))
                if new_name != original_name
                or initial_fingerprint is not None
                or (
                    self.profiles.get(original_name)
                    and new_os != self.profiles[original_name].os_type
                )
                else contextlib.nullcontext()
            ),
        ):
            self._check_store_current()
            if original_name not in self.profiles:
                return False
            try:
                clean_tags = normalize_tags(tags) if tags is not None else None
                clean_backup_codes = (
                    normalize_backup_codes(backup_codes) if backup_codes is not None else None
                )
                current = self.profiles[original_name]
                if tags is None and group is not None and group.strip() != (current.group or ""):
                    clean_tags = normalize_tags(
                        ([group] if group.strip() else []) + current.effective_tags[1:]
                    )
            except ValueError:
                return False
            if not valid_display_order(display_order):
                return False
            if two_fa_secret and two_fa_secret.strip() and not clean_totp_secret(two_fa_secret):
                return False
            if start_url and start_url.strip() and not normalize_start_url(start_url):
                return False

            if new_name != original_name:
                if is_reserved_profile_name(new_name):
                    raise ValueError(get_string("validation_reserved_name", name=new_name))
                # Case-insensitive uniqueness: a case-only rename of the profile
                # itself is allowed; renaming to another profile's casefold is not.
                conflicting = self._find_by_casefold(new_name)
                if conflicting is not None and conflicting != original_name:
                    return False

            touches_data = (
                new_name != original_name
                or isinstance(initial_fingerprint, dict)
                or new_os != current.os_type
            )
            with contextlib.ExitStack() as pin_stack:
                pinned: PinnedStorageRoot | None = None
                known_names = tuple(self.profiles.keys())
                if touches_data:
                    require_data_dir_mounted(DATA_DIR)
                    if pathlib.Path(DATA_DIR).is_dir():
                        pinned = pin_stack.enter_context(
                            PinnedStorageRoot(
                                DATA_DIR,
                                mount_check=require_data_dir_mounted,
                                identity_fn=storage_root_identity,
                            )
                        )
                    require_no_pending_delete(
                        self._data_path(original_name),
                        known_profiles=known_names,
                        profiles_file=PROFILES_FILE,
                    )
                    if new_name != original_name:
                        require_no_pending_delete(
                            self._data_path(new_name),
                            known_profiles=known_names,
                            profiles_file=PROFILES_FILE,
                        )
                    if pinned is not None:
                        pinned.clear_resolved_import_journals(
                            (original_name, new_name),
                            profiles_file=PROFILES_FILE,
                            strict=True,
                        )

                orig_dir = pathlib.Path(self._data_path(original_name))
                orig_dir_existed = (
                    pinned.exists(original_name) if pinned is not None else orig_dir.exists()
                )
                built_chrome: tuple[Any, dict[str, Any] | None] | None = None
                prev_persona_bytes: bytes | None = None
                if isinstance(initial_fingerprint, dict) and current.engine == "chrome":
                    built_chrome = fp_service._build_chrome_persona(
                        str(orig_dir),
                        initial_fingerprint,
                        os_type=new_os,
                        core_version=getattr(current, "core_version", None),
                    )
                    if pinned is not None:
                        prev_persona_bytes = pinned.read_file_bytes(
                            (original_name,), fp_service.CHROME_PERSONA_FILE
                        )
                    else:
                        orig_persona_file = orig_dir / fp_service.CHROME_PERSONA_FILE
                        if orig_persona_file.is_file():
                            prev_persona_bytes = orig_persona_file.read_bytes()

                originals = dict(self.profiles)
                previous = {name: replace(p) for name, p in originals.items()}
                previous_tx = dict(self._import_tx)
                renamed_dir = False
                rename_journal: str | None = None
                # Record durable rename journal before moving data so an offline
                # rollback fences both names on remount instead of treating data as empty.
                if new_name != original_name:
                    old_dir = pathlib.Path(self._data_path(original_name))
                    new_dir = pathlib.Path(self._data_path(new_name))
                    has_old = (
                        pinned.exists(original_name) if pinned is not None else old_dir.exists()
                    )
                    if has_old:
                        has_new = (
                            pinned.exists(new_name) if pinned is not None else new_dir.exists()
                        )
                        if has_new:
                            # On case-insensitive filesystems (Windows/macOS) a
                            # case-only rename sees the target as existing because
                            # both paths point to the same directory entry.  Allow
                            # the rename only in that situation.
                            if pinned is not None:
                                same = pinned.same_entry(original_name, new_name)
                            else:
                                try:
                                    same = old_dir.samefile(new_dir)
                                except OSError:
                                    same = False
                            if not same:
                                # Refuse: do NOT delete potential user data.
                                logger.warning(
                                    "Cannot rename %s -> %s: target data dir already exists",
                                    original_name,
                                    new_name,
                                )
                                return False
                        try:
                            if pinned is not None:
                                rename_journal = pinned.record_pending_rename(
                                    original_name, new_name
                                )
                                pinned.rename_child(original_name, new_name)
                            else:
                                old_dir.rename(new_dir)
                            renamed_dir = True
                        except StorageVolumeUnmountedError:
                            if pinned is not None and rename_journal is not None:
                                pinned.clear_pending_rename(rename_journal)
                            raise
                        except OSError as e:
                            if pinned is not None and rename_journal is not None:
                                pinned.clear_pending_rename(rename_journal)
                            logger.warning("Could not rename data dir for %s: %s", original_name, e)
                            return False

                profile = self.profiles[original_name]
                old_os = profile.os_type
                profile.name = new_name
                profile.proxy = new_proxy or None
                profile.os_type = new_os
                profile.timezone = new_timezone or None
                profile.locale = new_locale or None
                if clean_tags is not None:
                    profile.tags = clean_tags
                    profile.group = clean_tags[0] if clean_tags else None
                if extension_ids is not None:
                    profile.extension_ids = [
                        str(x).strip() for x in extension_ids if isinstance(x, str) and x.strip()
                    ]
                if account_uid is not None:
                    profile.account_uid = account_uid.strip() or None
                if account_password is not None:
                    profile.account_password = account_password or None
                if two_fa_secret is not None:
                    profile.two_fa_secret = clean_totp_secret(two_fa_secret) or None
                if recovery_email is not None or notes is not None:
                    # Materialize any legacy recovery mail still embedded in notes first.
                    eff_rec, eff_notes = split_recovery_and_notes(
                        profile.recovery_email, profile.notes
                    )
                    profile.recovery_email = eff_rec or None
                    profile.notes = eff_notes or None
                if recovery_email is not None:
                    profile.recovery_email = recovery_email.strip() or None
                if backup_codes is not None:
                    profile.backup_codes = clean_backup_codes or None
                if notes is not None:
                    profile.notes = notes.strip() or None
                if start_url is not None:
                    profile.start_url = normalize_start_url(start_url) or None
                if webrtc_mode is not None:
                    profile.webrtc_mode = _webrtc_or_default(webrtc_mode)
                if canvas_mode is not None:
                    profile.canvas_mode = _canvas_or_default(canvas_mode)

                if new_name != original_name:
                    # Name edits must not change the legacy insertion-based row ID.
                    renamed = {
                        new_name if key == original_name else key: value
                        for key, value in self.profiles.items()
                    }
                    self.profiles.clear()
                    self.profiles.update(renamed)
                    if original_name in self._import_tx:
                        self._import_tx[new_name] = self._import_tx.pop(original_name)

                if display_order is not None:
                    self._move_profile(new_name, display_order)
                target_dir = self._data_path(new_name)
                target_existed_before = (
                    orig_dir_existed
                    if renamed_dir
                    else (
                        pinned.exists(new_name)
                        if pinned is not None
                        else pathlib.Path(target_dir).exists()
                    )
                )
                try:
                    if built_chrome is not None:
                        fp_service._write_chrome_persona(
                            target_dir, *built_chrome, pinned=pinned, rel_dir=(new_name,)
                        )
                    with (
                        self._warming_scheduler.retarget_profile(original_name, new_name)
                        if self._warming_scheduler is not None and new_name != original_name
                        else contextlib.nullcontext()
                    ):
                        if pinned is not None:
                            pinned.verify()
                        self.save_profiles(strict=True)
                except Exception as exc:
                    if not isinstance(exc, ProfileStoreChangedError):
                        for name, original in originals.items():
                            original.__dict__.update(vars(previous[name]))
                        self.profiles.clear()
                        self.profiles.update(originals)
                        self._import_tx.clear()
                        self._import_tx.update(previous_tx)
                    if renamed_dir:
                        try:
                            if pinned is not None:
                                pinned.rename_child(new_name, original_name)
                                pinned.clear_pending_rename(rename_journal)
                            else:
                                new_dir.rename(old_dir)
                        except StorageVolumeUnmountedError as rollback:
                            raise StorageVolumeUnmountedError(
                                rollback.errno or errno.ENODEV,
                                f"Profile save failed; storage changed before restoring data "
                                f"directory {new_dir} to {old_dir}",
                                str(new_dir),
                            ) from rollback
                        except OSError as rollback:
                            raise OSError(
                                f"Profile save failed; could not restore data directory {new_dir} to {old_dir}"
                            ) from rollback
                    if built_chrome is not None and not isinstance(
                        exc, StorageVolumeUnmountedError
                    ):
                        can_restore = True
                        if pinned is not None:
                            try:
                                pinned.verify()
                            except StorageVolumeUnmountedError:
                                can_restore = False
                        if can_restore:
                            restore_name = original_name if renamed_dir else new_name
                            _restore_chrome_persona_file(
                                old_dir if renamed_dir else pathlib.Path(target_dir),
                                prev_persona_bytes,
                                target_existed_before,
                                pinned=pinned,
                                rel_dir=(restore_name,),
                            )
                    raise
                if pinned is not None and rename_journal is not None:
                    pinned.clear_pending_rename(rename_journal)

                # A failed metadata commit must not reset or replace browser identity.
                if isinstance(initial_fingerprint, dict) and profile.engine == "camoufox":
                    with contextlib.suppress(Exception):
                        fp_service.save_fingerprint(
                            target_dir,
                            new_os,
                            initial_fingerprint,
                            pinned=pinned,
                            rel_dir=(new_name,),
                        )
                elif built_chrome is None and new_os != old_os:
                    with contextlib.suppress(Exception):
                        fp_service.reset(
                            target_dir,
                            pinned=pinned,
                            rel_dir=(new_name,),
                            profiles_file=PROFILES_FILE,
                        )
            logger.info("Updated profile: %s -> %s", original_name, new_name)
            return True

    def delete_profile(self, name: str) -> bool:
        with self._lock, store_lease(PROFILES_FILE), profile_lease(self._data_path(name)):
            self._check_store_current()
            if name not in self.profiles:
                return False
            known_names = tuple(self.profiles.keys())
            with PinnedStorageRoot(
                DATA_DIR,
                mount_check=require_data_dir_mounted,
                identity_fn=storage_root_identity,
            ) as pinned:
                target_dir = pathlib.Path(self._data_path(name))
                pending = find_pending_delete_entries(
                    target_dir.parent,
                    target_dir.name,
                    known_profiles=known_names,
                    profiles_file=PROFILES_FILE,
                )
                staged_entry: PendingDeleteEntry | None = None
                staged_by_this_call = False
                if pinned.exists_or_symlink(name):
                    if pending:
                        raise PendingProfileDeleteError(
                            errno.EBUSY,
                            format_pending_delete_message(name, target_dir, pending),
                            str(pending[0].path),
                        )
                    # Stage inside the verified internal namespace via dir_fd so a
                    # volume swap never writes onto a replacement disk and a failed
                    # commit restores the directory cleanly.
                    pinned.clear_resolved_import_journals(
                        (name,), profiles_file=PROFILES_FILE, strict=True
                    )
                    staged_entry = pinned.stage_profile_for_delete(name)
                    staged_by_this_call = True
                else:
                    pinned.verify()
                    if any(not entry.verified for entry in pending) or len(pending) > 1:
                        raise PendingProfileDeleteError(
                            errno.EBUSY,
                            format_pending_delete_message(name, target_dir, pending),
                            str(pending[0].path),
                        )
                    pinned.clear_resolved_import_journals(
                        (name,), profiles_file=PROFILES_FILE, strict=True
                    )
                    if len(pending) == 1:
                        # Finish a verified interrupted pre-commit delete cleanly on retry.
                        staged_entry = pending[0]
                previous = self.profiles.copy()
                previous_tx = dict(self._import_tx)
                previous_orders: list[tuple[Profile, int | None]] = []
                del self.profiles[name]
                self._import_tx.pop(name, None)
                try:
                    if any(p.display_order is not None for p in self.profiles.values()):
                        for index, profile in enumerate(self._ordered_profiles(), 1):
                            previous_orders.append((profile, profile.display_order))
                            profile.display_order = index
                    pinned.verify()
                    self.save_profiles(strict=True)
                except Exception as exc:
                    if not isinstance(exc, ProfileStoreChangedError):
                        for profile, order in previous_orders:
                            profile.display_order = order
                        self.profiles.clear()
                        self.profiles.update(previous)
                        self._import_tx.clear()
                        self._import_tx.update(previous_tx)
                    if staged_entry is not None and staged_by_this_call:
                        try:
                            pinned.restore_staged_delete(staged_entry, name)
                        except OSError as rollback:
                            offline_exc = (
                                exc
                                if isinstance(exc, StorageVolumeUnmountedError)
                                else (
                                    rollback
                                    if isinstance(rollback, StorageVolumeUnmountedError)
                                    else None
                                )
                            )
                            if offline_exc is None:
                                try:
                                    pinned.verify()
                                except StorageVolumeUnmountedError as mount_exc:
                                    offline_exc = mount_exc
                            msg = (
                                f"Profile delete aborted; original metadata preserved and data "
                                f"staged at '{staged_entry.path}'"
                            )
                            if offline_exc is not None:
                                raise StorageVolumeUnmountedError(
                                    offline_exc.errno or errno.ENODEV,
                                    msg,
                                    str(staged_entry.path),
                                ) from rollback
                            raise OSError(
                                rollback.errno or errno.EIO, msg, str(staged_entry.path)
                            ) from rollback
                    raise
                # Inside the lock: a profile re-created under the same (or, on a
                # case-insensitive file system, a case-variant) name must not have
                # its fresh data dir removed by this delete.
                if staged_entry is not None:
                    try:
                        pinned.remove_staged_delete(staged_entry, rmtree_fn=shutil.rmtree)
                    except StorageVolumeUnmountedError as exc:
                        raise StorageVolumeUnmountedError(
                            exc.errno,
                            f"Profile '{name}' removed from store, but external storage went "
                            f"offline or changed before staged data at '{staged_entry.path}' "
                            "could be deleted",
                            str(staged_entry.path),
                        ) from exc
                    except OSError as exc:
                        try:
                            pinned.verify()
                        except StorageVolumeUnmountedError as mount_exc:
                            raise StorageVolumeUnmountedError(
                                mount_exc.errno or errno.ENODEV,
                                f"Profile '{name}' removed from store, but external storage went "
                                f"offline or changed before staged data at '{staged_entry.path}' "
                                "could be deleted",
                                str(staged_entry.path),
                            ) from exc
                        raise OSError(
                            exc.errno or errno.EIO,
                            f"Profile '{name}' removed from store, but staged data at "
                            f"'{staged_entry.path}' could not be deleted: {exc.strerror or exc}",
                            str(staged_entry.path),
                        ) from exc
        logger.info("Deleted profile: %s", name)
        return True

    def list_profiles(self) -> list[Profile]:
        with self._lock:
            with contextlib.suppress(ProfileStoreChangedError):
                self._check_store_current()
            return self._ordered_profiles()

    def set_extension_assignments(self, extension_id: str, profile_names: set[str]) -> int:
        """Apply one extension's membership without overwriting other profile edits."""
        with self._lock:
            profiles = self.list_profiles()
            if not profile_names.issubset(profile.name for profile in profiles):
                raise ProfileStoreChangedError(
                    "Selected profiles no longer exist; reopen extension settings and try again"
                )
            changed = []
            for profile in profiles:
                ids = profile.extension_ids
                should_have = profile.name in profile_names
                if should_have == (extension_id in ids):
                    continue
                changed.append((profile, ids))
                profile.extension_ids = (
                    [*ids, extension_id] if should_have else [x for x in ids if x != extension_id]
                )
            if changed:
                try:
                    self.save_profiles(strict=True)
                except Exception:
                    for profile, ids in changed:
                        profile.extension_ids = ids
                    raise
            return len(changed)

    def get_profile(self, name: str) -> Profile | None:
        """The named profile, read from the shared store if another process changed it."""
        with self._lock:
            with contextlib.suppress(ProfileStoreChangedError):
                self._check_store_current()
            return self.profiles.get(name)

    def _ordered_profiles(self) -> list[Profile]:
        profiles = list(self.profiles.values())
        return [
            p
            for _, p in sorted(
                enumerate(profiles, 1), key=lambda pair: pair[1].display_order or pair[0]
            )
        ]

    def _move_profile(self, name: str, position: int) -> None:
        profiles = [p for p in self._ordered_profiles() if p.name != name]
        profiles.insert(min(position - 1, len(profiles)), self.profiles[name])
        for index, profile in enumerate(profiles, 1):
            profile.display_order = index

    def export_profile(
        self,
        name: str,
        export_path: str,
        include_data: bool = True,
    ) -> tuple[bool, str]:
        with self._lock:
            if name not in self.profiles:
                return False, "Profile not found"
            known_names = tuple(self.profiles.keys())
            try:
                root_identity = self._pin_storage_root()
                data_path = self._data_path(name)
                require_no_pending_delete(
                    data_path, known_profiles=known_names, profiles_file=PROFILES_FILE
                )
            except (StorageVolumeUnmountedError, PendingProfileDeleteError) as exc:
                return False, str(exc)
            profile = self.profiles[name]
        return export_to_zip(
            profile,
            data_path,
            export_path,
            include_data,
            expected_root_identity=root_identity,
            known_profiles=known_names,
            profiles_file=PROFILES_FILE,
        )

    def import_profile(
        self,
        zip_path: str,
        overwrite: bool = False,
    ) -> tuple[bool, str]:
        # Decide before extracting: an archive must not write into an existing
        # profile's data unless overwriting, and an overwrite that changes the
        # engine starts from an empty directory (the engines' data do not mix).
        meta = read_profile_meta(zip_path)
        if not isinstance(meta, dict):
            return False, "Invalid profile metadata"
        try:
            normalize_tags(meta.get("tags", []))
            if meta.get("backup_codes") is not None:
                normalize_backup_codes(meta.get("backup_codes"))
        except ValueError as exc:
            return False, str(exc)
        if not valid_display_order(meta.get("display_order")):
            return False, "Invalid display order in archive"
        name = meta.get("name")
        if not isinstance(name, str) or not validate_profile_name(name)[0]:
            return False, "Invalid profile name in archive"
        with (
            self._lock,
            store_lease(PROFILES_FILE),
            profile_lease(self._data_path(self._find_by_casefold(name) or name)),
        ):
            self._check_store_current()
            # Case-insensitive collision check: find any existing profile whose
            # name casefolds to the imported name so the check is consistent on
            # all platforms (Windows/macOS case-insensitive FS, Linux sensitive).
            existing_key = self._find_by_casefold(name) if name else None
            if existing_key is not None:
                if not overwrite:
                    return False, ProfileImportConflict(name)
                if meta.get("core_version") != self.profiles[existing_key].core_version:
                    return (
                        False,
                        "Cannot change an existing core binding through import; use backed-up core management",
                    )
            # Extraction and metadata commit must both succeed before discarding sessions.
            base = ensure_data_dir(DATA_DIR)
            known_names = tuple(self.profiles.keys())
            with PinnedStorageRoot(
                base,
                mount_check=require_data_dir_mounted,
                identity_fn=storage_root_identity,
            ) as pinned:
                require_no_pending_delete(
                    self._data_path(name), known_profiles=known_names, profiles_file=PROFILES_FILE
                )
                if existing_key is not None and existing_key != name:
                    require_no_pending_delete(
                        self._data_path(existing_key),
                        known_profiles=known_names,
                        profiles_file=PROFILES_FILE,
                    )
                with contextlib.ExitStack() as guards, contextlib.ExitStack() as cleanup:
                    temp_name = f".import_{uuid.uuid4().hex[:8]}"
                    temporary = pinned.mkdir_child(temp_name, mode=0o700)
                    cleanup.callback(pinned.remove_child_tree, temp_name, ignore_errors=True)
                    stage = pinned.ensure_subdir((temp_name, "incoming"), mode=0o700)
                    old_dir = pathlib.Path(self._data_path(existing_key)) if existing_key else None
                    has_old = existing_key is not None and pinned.exists(existing_key)
                    if old_dir is not None and has_old:
                        guards.enter_context(
                            native_profile_guard(old_dir, self.profiles[existing_key].engine)
                        )
                    if (
                        old_dir is not None
                        and has_old
                        and (meta.get("engine") or "camoufox") == self.profiles[existing_key].engine
                    ):
                        # Firefox record locks are released if this PID reads/closes their inode.
                        lock_files = {
                            ".parentlock",
                            "parent.lock",
                            "lock",
                            "SingletonLock",
                            "SingletonCookie",
                            "SingletonSocket",
                        }
                        pinned.copy_child_tree(
                            existing_key, (temp_name, "incoming", name), ignore_top=lock_files
                        )
                    pinned.verify()
                    if import_from_zip is _ORIG_IMPORT_FROM_ZIP:
                        success, result = import_from_zip(
                            zip_path,
                            str(stage),
                            pinned=pinned,
                            base_rel_parts=(temp_name, "incoming"),
                        )
                    else:
                        success, result = import_from_zip(zip_path, str(stage))
                    if not success:
                        return False, result
                    profile = result
                    expected_engine = (
                        meta.get("engine") if meta.get("engine") in ENGINES else "camoufox"
                    )
                    if profile.name != name or profile.engine != expected_engine:
                        return False, "Profile archive changed during import"
                    if (
                        existing_key is not None
                        and profile.core_version != self.profiles[existing_key].core_version
                    ):
                        return False, "Profile core binding changed during import"
                    pinned.ensure_subdir((temp_name, "incoming", profile.name))
                    if pinned.exists(profile.name) and not (
                        existing_key is not None
                        and pinned.exists(existing_key)
                        and pinned.same_entry(profile.name, existing_key)
                    ):
                        return False, "Target profile data directory already exists"
                    previous = dict(self.profiles)
                    previous_tx = dict(self._import_tx)
                    backup = temporary / "previous"
                    published = False
                    moved_old = False
                    saved_store = False
                    import_tx = uuid.uuid4().hex
                    import_journal: str | None = None
                    try:
                        pinned.clear_resolved_import_journals(
                            (existing_key or profile.name, profile.name),
                            profiles_file=PROFILES_FILE,
                            strict=True,
                        )
                        import_journal = pinned.record_pending_import(
                            existing_key, profile.name, temp_name, tx_id=import_tx
                        )
                        if existing_key is not None and pinned.exists(existing_key):
                            pinned.rename_child_into_subdir(existing_key, temp_name, "previous")
                            moved_old = True
                        pinned.rename_from_subdir(
                            (temp_name, "incoming"), profile.name, profile.name
                        )
                        published = True
                        if existing_key is not None:
                            del self.profiles[existing_key]
                            self._import_tx.pop(existing_key, None)
                        self.profiles[profile.name] = profile
                        self._import_tx[profile.name] = import_tx
                        pinned.verify()
                        self.save_profiles(strict=True)
                        saved_store = True
                        pinned.clear_pending_import(import_journal, strict=True)
                    except Exception as exc:
                        # Disarm staging cleanup before any fallible rollback step so a
                        # failed metadata revert never deletes the original backup in previous/.
                        rollback_cleanup = cleanup.pop_all()
                        if not isinstance(exc, ProfileStoreChangedError):
                            self.profiles.clear()
                            self.profiles.update(previous)
                            self._import_tx.clear()
                            self._import_tx.update(previous_tx)
                            if saved_store:
                                try:
                                    self.save_profiles(strict=True)
                                except Exception:
                                    # Rollback save failed: reload from disk so in-memory
                                    # rows match the committed store while previous/ survives.
                                    self.profiles.clear()
                                    self._import_tx.clear()
                                    self._store_digest = None
                                    self._load_profiles()
                                    raise
                        retained = f"; old session retained at {backup}" if moved_old else ""
                        try:
                            pinned.verify()
                            if published:
                                pinned.remove_child_tree(profile.name, rmtree_fn=shutil.rmtree)
                            if moved_old and existing_key is not None:
                                pinned.rename_from_subdir((temp_name,), "previous", existing_key)
                            pinned.clear_pending_import(import_journal)
                        except StorageVolumeUnmountedError as rollback:
                            raise StorageVolumeUnmountedError(
                                rollback.errno or errno.ENODEV,
                                f"Import recovery failed; storage changed{retained}",
                                str(backup if moved_old else temporary),
                            ) from rollback
                        except OSError as rollback:
                            raise OSError(f"Import recovery failed{retained}") from rollback
                        rollback_cleanup.close()
                        raise
                    logger.info("Registered imported profile: %s", profile.name)
                    return True, profile.name
