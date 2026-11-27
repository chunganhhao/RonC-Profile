import contextlib
import errno
import json
import os
import pathlib
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Collection
from datetime import datetime
from itertools import count

from ...core.config import (
    PinnedStorageRoot,
    StorageIdentity,
    StorageVolumeUnmountedError,
    require_data_dir_mounted,
    require_no_pending_delete,
    require_same_storage_root,
    storage_root_identity,
)
from ...core.logging import get_logger
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
    validate_proxy_format,
    validate_timezone,
)
from ..browser.fingerprint import IDENTITY_FILES

logger = get_logger("profile.transfer")

_EXPORT_STAGING_PREFIX = ".ronc-profile-export-"
_EXPORT_STAGING_SUFFIX = ".tmp"


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def export_to_zip(
    profile: Profile,
    profile_data_dir: str,
    export_path: str,
    include_data: bool = True,
    exclude_files: frozenset[str] = frozenset(),
    *,
    expected_root_identity: StorageIdentity | None = None,
    known_profiles: Collection[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> tuple[bool, str]:
    """Create a ZIP archive of a profile and optionally its browser data."""
    try:
        require_data_dir_mounted(profile_data_dir)
        source_path = pathlib.Path(profile_data_dir)
        root_identity = (
            require_same_storage_root(source_path.parent, expected_root_identity)
            if expected_root_identity is not None
            else storage_root_identity(source_path.parent)
        )
        require_no_pending_delete(
            source_path, known_profiles=known_profiles, profiles_file=profiles_file
        )
        source_existed = source_path.exists()
        if not source_existed:
            require_same_storage_root(source_path.parent, root_identity)
            require_no_pending_delete(
                source_path, known_profiles=known_profiles, profiles_file=profiles_file
            )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{profile.name}_{timestamp}"
        fd, staging_path = tempfile.mkstemp(
            dir=export_path, prefix=_EXPORT_STAGING_PREFIX, suffix=_EXPORT_STAGING_SUFFIX
        )
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(pathlib.Path(staging_path).unlink, missing_ok=True)
            with (
                os.fdopen(fd, "w+b") as staging,
                zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as zipf,
            ):
                zipf.writestr("profile.json", json.dumps(profile.to_dict(), indent=2))

                if include_data and source_existed:
                    archive_stat = os.fstat(staging.fileno())
                    for root, _dirs, files in os.walk(profile_data_dir, onerror=_raise_walk_error):
                        for file in files:
                            if file.startswith(_EXPORT_STAGING_PREFIX) and file.endswith(
                                _EXPORT_STAGING_SUFFIX
                            ):
                                continue
                            file_path = os.path.join(root, file)
                            relative_path = os.path.relpath(file_path, profile_data_dir)
                            if relative_path in exclude_files:
                                continue
                            source_stat = os.lstat(file_path)
                            if not stat.S_ISREG(source_stat.st_mode) or os.path.samestat(
                                source_stat, archive_stat
                            ):
                                continue
                            zipf.write(file_path, os.path.join("data", relative_path))
                else:
                    # The fingerprint is the profile's identity, not browser data.
                    for identity in IDENTITY_FILES:
                        fp_file = os.path.join(profile_data_dir, identity)
                        if pathlib.Path(fp_file).exists():
                            zipf.write(fp_file, os.path.join("data", identity))

            require_data_dir_mounted(profile_data_dir)
            require_same_storage_root(source_path.parent, root_identity)
            require_no_pending_delete(
                source_path, known_profiles=known_profiles, profiles_file=profiles_file
            )
            if source_existed and not source_path.exists():
                raise OSError(
                    errno.ENODEV,
                    f"Profile data directory '{source_path}' became unavailable during export",
                    str(source_path),
                )
            zip_path = _publish_zip(staging_path, export_path, stem)

        logger.info("Exported profile %s to %s", profile.name, zip_path)
        return True, zip_path
    except Exception as e:
        logger.exception("Error exporting profile %s: %s", profile.name, e)
        return False, str(e)


def _publish_zip(staging_path: str, export_path: str, stem: str) -> str:
    for index in count():
        suffix = f"_{index}" if index else ""
        zip_path = os.path.join(export_path, f"{stem}{suffix}.zip")
        try:
            if os.name == "nt":
                os.rename(staging_path, zip_path)
            else:
                os.link(staging_path, zip_path)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM):
                raise
        else:
            return zip_path
        # Filesystems without links still need an exclusive filename reservation.
        try:
            with open(zip_path, "xb"):
                pass
        except FileExistsError:
            continue
        try:
            os.replace(staging_path, zip_path)
        except BaseException:
            pathlib.Path(zip_path).unlink(missing_ok=True)
            raise
        return zip_path


def read_profile_meta(zip_path: str) -> dict | None:
    """profile.json of an archive without extracting anything, or None if unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            return json.loads(zipf.read("profile.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def _safe_archive_rel_parts(entry_name: str) -> tuple[str, ...] | None:
    rel = entry_name.removeprefix("data/")
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    parts = [part for part in pathlib.PurePosixPath(rel).parts if part not in ("", ".")]
    if not parts or any(part in ("/", "..") or ":" in part for part in parts):
        return None
    return tuple(parts)


def import_from_zip(
    zip_path: str,
    data_dir: str,
    *,
    pinned: PinnedStorageRoot | None = None,
    base_rel_parts: tuple[str, ...] = (),
) -> tuple[bool, Profile | str]:
    """Extract a profile from a ZIP archive.

    Returns (True, Profile) on success or (False, error_message) on failure.
    The caller is responsible for registering the profile in the manager.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            if "profile.json" not in zipf.namelist():
                return False, "Invalid profile archive (missing profile.json)"

            profile_data = json.loads(zipf.read("profile.json"))
            if not isinstance(profile_data, dict):
                return False, "Invalid profile metadata"
            name = profile_data.get("name")

            if not name:
                return False, "Invalid profile data (missing name)"
            valid, msg = validate_profile_name(name)
            if not valid:
                return False, f"Invalid profile name in archive: {msg}"

            os_type = profile_data.get("os_type")
            if os_type is not None and not isinstance(os_type, str):
                return False, "Invalid os_type in archive (must be text)"
            tags = normalize_tags(profile_data.get("tags", []))
            backup_codes = normalize_backup_codes(profile_data.get("backup_codes")) or None
            display_order = profile_data.get("display_order")
            if not valid_display_order(display_order):
                return False, "Invalid display order in archive"
            proxy = profile_data.get("proxy")
            if proxy in ("", "None"):
                proxy = None  # Legacy CLI direct-connection sentinel.
            if proxy is not None and (
                not isinstance(proxy, str) or not validate_proxy_format(proxy)[0]
            ):
                return False, "Invalid proxy configuration in archive"
            profile = Profile(
                name=name,
                core_version=profile_data.get("core_version")
                if isinstance(profile_data.get("core_version"), str)
                else None,
                proxy=proxy,
                os_type=os_type if os_type is not None else "windows",
                timezone=valid_or_none(profile_data.get("timezone"), validate_timezone),
                locale=valid_or_none(profile_data.get("locale"), validate_locale),
                engine=profile_data.get("engine")
                if profile_data.get("engine") in ENGINES
                else "camoufox",
                group=tags[0] if tags else (profile_data.get("group") or "").strip() or None,
                tags=tags,
                extension_ids=[
                    str(x).strip()
                    for x in (profile_data.get("extension_ids") or [])
                    if isinstance(x, str) and x.strip()
                ]
                if isinstance(profile_data.get("extension_ids"), list)
                else [],
                display_order=display_order,
                account_uid=(profile_data.get("account_uid") or "").strip() or None,
                account_password=profile_data.get("account_password") or None,
                two_fa_secret=clean_totp_secret(profile_data.get("two_fa_secret")) or None,
                recovery_email=split_recovery_and_notes(
                    profile_data.get("recovery_email"), profile_data.get("notes")
                )[0]
                or None,
                backup_codes=backup_codes,
                notes=split_recovery_and_notes(
                    profile_data.get("recovery_email"), profile_data.get("notes")
                )[1]
                or None,
                start_url=normalize_start_url(profile_data.get("start_url")) or None,
                webrtc_mode=profile_data.get("webrtc_mode")
                if profile_data.get("webrtc_mode") in WEBRTC_MODES
                else "proxy",
                canvas_mode=profile_data.get("canvas_mode")
                if profile_data.get("canvas_mode") in CANVAS_MODES
                else "auto",
            )

            data_files = [f for f in zipf.namelist() if f.startswith("data/")]
            if data_files:
                if pinned is not None:
                    dest_root_parts = (*base_rel_parts, name)
                    pinned.ensure_subdir(dest_root_parts)
                    for file in data_files:
                        if file.endswith("/"):
                            continue
                        rel_parts = _safe_archive_rel_parts(file)
                        if rel_parts is None:
                            return False, f"Unsafe path in archive: {file}"
                        with (
                            zipf.open(file) as src,
                            pinned.open_file_write(
                                (*dest_root_parts, *rel_parts[:-1]), rel_parts[-1]
                            ) as dst,
                        ):
                            shutil.copyfileobj(src, dst)
                else:
                    profile_data_dir = os.path.join(data_dir, name)
                    root = pathlib.Path(profile_data_dir).resolve()
                    root.mkdir(exist_ok=True, parents=True)

                    for file in data_files:
                        if file.endswith("/"):
                            continue
                        target_path = os.path.join(
                            profile_data_dir,
                            os.path.relpath(file, "data"),
                        )
                        # Reject entries such as data/../../evil that would be
                        # written outside the profile's own directory.
                        if not pathlib.Path(target_path).resolve().is_relative_to(root):
                            return False, f"Unsafe path in archive: {file}"
                        pathlib.Path(target_path).parent.mkdir(
                            exist_ok=True,
                            parents=True,
                        )
                        with zipf.open(file) as src, open(target_path, "wb") as dst:
                            dst.write(src.read())

            logger.info("Imported profile from zip: %s", name)
            return True, profile
    except StorageVolumeUnmountedError:
        raise
    except zipfile.BadZipFile:
        return False, "Invalid zip file"
    except Exception as e:
        logger.exception("Error importing profile: %s", e)
        return False, str(e)
