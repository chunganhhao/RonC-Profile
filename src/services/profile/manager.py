import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from typing import Any

from ...core.config import DATA_DIR, PROFILES_FILE
from ...core.logging import get_logger
from ...models.profile import (
    CANVAS_MODES,
    ENGINES,
    WEBRTC_MODES,
    Profile,
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

logger = get_logger("profile.manager")


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


class ProfileManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.profiles: dict[str, Profile] = {}
        self._store_digest = None
        self._load_profiles()
        pathlib.Path(DATA_DIR).mkdir(exist_ok=True, parents=True)

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

    def _load_profiles(self) -> None:
        if pathlib.Path(PROFILES_FILE).exists():
            try:
                with pathlib.Path(PROFILES_FILE).open("rb") as f:
                    raw = f.read()
                    self._store_digest = hashlib.sha256(raw).hexdigest()
                    data = json.loads(raw)
                    for name, p_data in data.items():
                        if not validate_profile_name(name)[0] or p_data.get("name") != name:
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
                            "notes": split_recovery_and_notes(
                                p_data.get("recovery_email"), p_data.get("notes")
                            )[1]
                            or None,
                            "start_url": normalize_start_url(p_data.get("start_url")) or None,
                            "webrtc_mode": _webrtc_or_default(p_data.get("webrtc_mode")),
                            "canvas_mode": _canvas_or_default(p_data.get("canvas_mode")),
                        }
                        self.profiles[name] = Profile(**clean_data)
                logger.info("Loaded %d profiles", len(self.profiles))
            except Exception as e:
                logger.exception("Error loading profiles: %s", e)

    def _check_store_current(self) -> None:
        path = pathlib.Path(PROFILES_FILE)
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if current != self._store_digest:
            self.profiles.clear()
            self._store_digest = None
            self._load_profiles()
            raise ProfileStoreChangedError(
                "Profile metadata changed in another process; reload before retrying"
            )

    def save_profiles(self, *, strict: bool = False) -> None:
        """Atomic write: write to a temp file then os.replace."""
        path = pathlib.Path(PROFILES_FILE)
        tmp = path.with_suffix(".json.tmp")
        try:
            with self._lock, store_lease(path):
                self._check_store_current()
                payload = json.dumps(
                    {name: p.to_dict() for name, p in self.profiles.items()}, indent=4
                )
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
        notes: str | None = None,
        start_url: str | None = None,
        webrtc_mode: str = "proxy",
        canvas_mode: str = "auto",
        initial_fingerprint: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock, store_lease(PROFILES_FILE):
            self._check_store_current()
            if self._find_by_casefold(name) is not None:
                return False
            try:
                clean_tags = normalize_tags(tags) if tags is not None else None
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
            data_dir = self._data_path(name)
            try:
                pathlib.Path(data_dir).mkdir(exist_ok=True, parents=True)
            except OSError as e:
                logger.warning("Could not create data dir for %s: %s", name, e)
                return False
            if isinstance(initial_fingerprint, dict):
                if engine == "camoufox":
                    with contextlib.suppress(Exception):
                        fp_service.save_fingerprint(data_dir, os_type, initial_fingerprint)
                elif engine == "chrome" and "seed" in initial_fingerprint:
                    with contextlib.suppress(Exception):
                        fp_service.save_chrome_persona(data_dir, initial_fingerprint)
            clean_ext_ids = (
                [str(x).strip() for x in extension_ids if isinstance(x, str) and x.strip()]
                if isinstance(extension_ids, list)
                else []
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
                notes=(notes or "").strip() or None,
                start_url=normalize_start_url(start_url) or None,
                webrtc_mode=_webrtc_or_default(webrtc_mode),
                canvas_mode=_canvas_or_default(canvas_mode),
            )
            if display_order is not None:
                self._move_profile(name, display_order)
            self.save_profiles()
            logger.info("Created profile: %s", name)
            return True

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
                # Case-insensitive uniqueness: a case-only rename of the profile
                # itself is allowed; renaming to another profile's casefold is not.
                conflicting = self._find_by_casefold(new_name)
                if conflicting is not None and conflicting != original_name:
                    return False

            originals = dict(self.profiles)
            previous = {name: replace(p) for name, p in originals.items()}
            renamed_dir = False
            # Rename before metadata commit; failed persistence rolls it back.
            if new_name != original_name:
                old_dir = pathlib.Path(self._data_path(original_name))
                new_dir = pathlib.Path(self._data_path(new_name))
                if old_dir.exists():
                    if new_dir.exists():
                        # On case-insensitive filesystems (Windows/macOS) a
                        # case-only rename sees the target as existing because
                        # both paths point to the same directory entry.  Allow
                        # the rename only in that situation.
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
                        old_dir.rename(new_dir)
                        renamed_dir = True
                    except OSError as e:
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
                eff_rec, eff_notes = split_recovery_and_notes(profile.recovery_email, profile.notes)
                profile.recovery_email = eff_rec or None
                profile.notes = eff_notes or None
            if recovery_email is not None:
                profile.recovery_email = recovery_email.strip() or None
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

            if display_order is not None:
                self._move_profile(new_name, display_order)
            try:
                self.save_profiles(strict=True)
            except Exception as exc:
                if not isinstance(exc, ProfileStoreChangedError):
                    for name, original in originals.items():
                        original.__dict__.update(vars(previous[name]))
                    self.profiles.clear()
                    self.profiles.update(originals)
                if renamed_dir:
                    try:
                        new_dir.rename(old_dir)
                    except OSError as rollback:
                        raise OSError(
                            f"Profile save failed; could not restore data directory {new_dir} to {old_dir}"
                        ) from rollback
                raise

            # A failed metadata commit must not reset or replace browser identity.
            target_dir = self._data_path(new_name)
            if isinstance(initial_fingerprint, dict) and profile.engine == "camoufox":
                with contextlib.suppress(Exception):
                    fp_service.save_fingerprint(target_dir, new_os, initial_fingerprint)
            elif (
                isinstance(initial_fingerprint, dict)
                and profile.engine == "chrome"
                and "seed" in initial_fingerprint
            ):
                with contextlib.suppress(Exception):
                    fp_service.save_chrome_persona(target_dir, initial_fingerprint)
            elif new_os != old_os:
                with contextlib.suppress(Exception):
                    fp_service.reset(target_dir)
            logger.info("Updated profile: %s -> %s", original_name, new_name)
            return True

    def delete_profile(self, name: str) -> bool:
        with self._lock, store_lease(PROFILES_FILE), profile_lease(self._data_path(name)):
            self._check_store_current()
            if name not in self.profiles:
                return False
            del self.profiles[name]
            if any(p.display_order is not None for p in self.profiles.values()):
                for index, profile in enumerate(self._ordered_profiles(), 1):
                    profile.display_order = index
            self.save_profiles()
            # Inside the lock: a profile re-created under the same (or, on a
            # case-insensitive file system, a case-variant) name must not have
            # its fresh data dir removed by this delete.
            shutil.rmtree(self._data_path(name), ignore_errors=True)
        logger.info("Deleted profile: %s", name)
        return True

    def list_profiles(self) -> list[Profile]:
        with self._lock:
            with contextlib.suppress(ProfileStoreChangedError):
                self._check_store_current()
            return self._ordered_profiles()

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
            profile = self.profiles[name]
            data_path = self._data_path(name)
        return export_to_zip(profile, data_path, export_path, include_data)

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
                    return False, f"Profile '{name}' already exists"
                if meta.get("core_version") != self.profiles[existing_key].core_version:
                    return (
                        False,
                        "Cannot change an existing core binding through import; use backed-up core management",
                    )
            # Extraction and metadata commit must both succeed before discarding sessions.
            base = pathlib.Path(DATA_DIR)
            base.mkdir(parents=True, exist_ok=True)
            with contextlib.ExitStack() as guards, contextlib.ExitStack() as cleanup:
                temporary = tempfile.mkdtemp(dir=base, prefix=".import_")
                cleanup.callback(shutil.rmtree, temporary, ignore_errors=True)
                stage = pathlib.Path(temporary) / "incoming"
                stage.mkdir()
                old_dir = pathlib.Path(self._data_path(existing_key)) if existing_key else None
                staged_dir = stage / name
                if old_dir is not None and old_dir.exists():
                    guards.enter_context(
                        native_profile_guard(old_dir, self.profiles[existing_key].engine)
                    )
                if (
                    old_dir is not None
                    and old_dir.exists()
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
                    shutil.copytree(
                        old_dir,
                        staged_dir,
                        ignore=lambda source, names: (
                            lock_files.intersection(names)
                            if pathlib.Path(source) == old_dir
                            else set()
                        ),
                    )
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
                target = pathlib.Path(self._data_path(profile.name))
                staged_dir = stage / profile.name
                staged_dir.mkdir(exist_ok=True)
                if target.exists() and not (
                    old_dir is not None and old_dir.exists() and target.samefile(old_dir)
                ):
                    return False, "Target profile data directory already exists"
                previous = dict(self.profiles)
                backup = pathlib.Path(temporary) / "previous"
                published = False
                moved_old = False
                try:
                    if old_dir is not None and old_dir.exists():
                        old_dir.rename(backup)
                        moved_old = True
                    staged_dir.rename(target)
                    published = True
                    if existing_key is not None:
                        del self.profiles[existing_key]
                    self.profiles[profile.name] = profile
                    self.save_profiles(strict=True)
                except Exception as exc:
                    if not isinstance(exc, ProfileStoreChangedError):
                        self.profiles.clear()
                        self.profiles.update(previous)
                    # If recovery itself fails, retain the old session in this directory.
                    rollback_cleanup = cleanup.pop_all()
                    try:
                        if published:
                            shutil.rmtree(target)
                        if moved_old:
                            backup.rename(old_dir)
                    except OSError as rollback:
                        raise OSError(
                            f"Import recovery failed; old session retained at {backup}"
                        ) from rollback
                    rollback_cleanup.close()
                    raise
                logger.info("Registered imported profile: %s", profile.name)
                return True, profile.name
