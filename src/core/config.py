from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import json
import os
import pathlib
import re
import shutil
import stat
import struct
import sys
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass

if os.getenv("FOXPROFILE_IGNORE_DOTENV") != "1":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


class StorageVolumeUnmountedError(OSError):
    """Raised when a macOS /Volumes/<name> profile data path is not mounted."""


class PendingProfileDeleteError(OSError):
    """Raised when a profile has an unresolved staged delete directory on disk."""


# 65 chars (> 64-char max profile name): cannot collide with any valid legacy/future profile.
STAGING_DIR_NAME = ".ronc-staged-deletes-internal-do-not-use-as-profile-name-reserved"
STAGED_DELETE_PREFIX = ".deleting_"
_PENDING_RENAME_PREFIX = ".renaming_"
_PENDING_IMPORT_PREFIX = ".importing_"
_RESERVED_PROFILE_PREFIXES = (
    ".deleting_",
    ".import_",
    ".importing_",
    ".renaming_",
    ".ronc-staged-deletes",
)
_STAGED_SLOT_RE = re.compile(r"^\.deleting_[0-9a-f]{32}$")
_STAGED_DELETE_RE = re.compile(r"^\.deleting_[0-9a-f]{32}_(?P<name>.+)$")
_PENDING_RENAME_RE = re.compile(r"^\.renaming_[0-9a-f]{32}\.json$")
_PENDING_IMPORT_RE = re.compile(r"^\.importing_[0-9a-f]{32}\.json$")
_TX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ORIG_PATH_RENAME = pathlib.Path.rename
_ORIG_PATH_REPLACE = pathlib.Path.replace
_ORIG_SHUTIL_RMTREE = shutil.rmtree


def is_valid_import_tx(value: object) -> bool:
    """Return True if *value* is a valid 32-hex import transaction token."""
    return isinstance(value, str) and bool(_TX_ID_RE.fullmatch(value))


def is_reserved_profile_name(name: str) -> bool:
    """Return True if *name* uses an internal staging/import prefix."""
    lower = name.casefold()
    return lower == STAGING_DIR_NAME.casefold() or any(
        lower.startswith(prefix) for prefix in _RESERVED_PROFILE_PREFIXES
    )


@dataclass(frozen=True)
class PendingDeleteEntry:
    """One unresolved staged delete, interrupted rename, or interrupted import entry."""

    path: pathlib.Path
    slot_name: str | None
    verified: bool
    kind: str = "delete"
    rename_from: str | None = None
    rename_to: str | None = None
    import_temp: str | None = None
    import_state: str | None = None


_DARWIN_ATTR_BIT_MAP_COUNT = 5
_DARWIN_ATTR_CMN_FSID = 0x00000004
_DARWIN_ATTR_VOL_INFO = 0x80000000
_DARWIN_ATTR_VOL_UUID = 0x00040000


class _DarwinAttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


@functools.lru_cache(maxsize=1)
def _darwin_getattrlist():
    try:
        libc = ctypes.CDLL("libSystem.B.dylib", use_errno=True)
    except OSError:
        return None
    fn = getattr(libc, "getattrlist", None)
    if fn is None:
        return None
    fn.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_DarwinAttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint,
    ]
    fn.restype = ctypes.c_int
    return fn


def _darwin_volume_identity(real_path: str) -> tuple[str | None, tuple[int, int] | None]:
    fn = _darwin_getattrlist()
    if fn is None:
        return None, None
    raw_path = os.fsencode(real_path)
    buf = ctypes.create_string_buffer(64)
    al = _DarwinAttrList(
        bitmapcount=_DARWIN_ATTR_BIT_MAP_COUNT,
        commonattr=_DARWIN_ATTR_CMN_FSID,
        volattr=_DARWIN_ATTR_VOL_INFO | _DARWIN_ATTR_VOL_UUID,
    )
    ctypes.set_errno(0)
    rc = fn(raw_path, ctypes.byref(al), buf, ctypes.sizeof(buf), 0)
    if rc == 0:
        length = struct.unpack_from("I", buf.raw, 0)[0]
        fsid = struct.unpack_from("ii", buf.raw, 4) if length >= 12 else None
        vol_uuid = None
        if length >= 28:
            raw_uuid = buf.raw[12:28]
            if any(raw_uuid):
                vol_uuid = str(uuid.UUID(bytes=raw_uuid)).upper()
        return vol_uuid, fsid
    err = ctypes.get_errno() or errno.EIO
    if err not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL):
        raise OSError(err, os.strerror(err), real_path)
    # Filesystems without ATTR_VOL_UUID (e.g. exFAT/network mounts) still expose fsid.
    al_fsid = _DarwinAttrList(
        bitmapcount=_DARWIN_ATTR_BIT_MAP_COUNT,
        commonattr=_DARWIN_ATTR_CMN_FSID,
    )
    ctypes.set_errno(0)
    rc = fn(raw_path, ctypes.byref(al_fsid), buf, ctypes.sizeof(buf), 0)
    if rc != 0:
        err = ctypes.get_errno() or errno.EIO
        raise OSError(err, os.strerror(err), real_path)
    length = struct.unpack_from("I", buf.raw, 0)[0]
    fsid = struct.unpack_from("ii", buf.raw, 4) if length >= 12 else None
    return None, fsid


@dataclass(frozen=True)
class StorageIdentity:
    """Identity pin for a mounted volume and profile data root directory."""

    real_path: str
    volume_uuid: str | None
    fsid: tuple[int, int] | int | None
    dev: int
    ino: int
    birthtime: float | None


def require_data_dir_mounted(data_dir: str | os.PathLike[str]) -> pathlib.Path:
    """Refuse unmounted macOS /Volumes/<name> targets before any read/write."""
    path = pathlib.Path(data_dir)
    if sys.platform == "darwin":
        raw_str = os.fspath(path)
        # Keep symlinks and '..' intact before realpath so '<link>/../data'
        # resolves at the symlink target rather than collapsing lexically first.
        joined = raw_str if os.path.isabs(raw_str) else os.path.join(os.getcwd(), raw_str)
        try:
            os.path.realpath(joined, strict=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageVolumeUnmountedError(
                errno.ENODEV,
                f"Cannot safely resolve profile data path '{raw_str}': {exc.strerror or exc}",
                raw_str,
            ) from exc
        lexical = pathlib.Path(os.path.normpath(joined))
        real = pathlib.Path(os.path.realpath(joined))
        candidates = [lexical] if real == lexical else [lexical, real]
        for candidate in candidates:
            if (
                len(candidate.parts) >= 2
                and candidate.parts[0] == "/"
                and candidate.parts[1].lower() == "volumes"
            ):
                if len(candidate.parts) == 2:
                    raise StorageVolumeUnmountedError(
                        errno.ENODEV,
                        "Refusing macOS '/Volumes' mount root as profile data directory",
                        str(candidate),
                    )
                volume = pathlib.Path("/", "Volumes", candidate.parts[2])
                mounted = os.path.ismount(volume)
                if not mounted and volume.is_symlink():
                    try:
                        mounted = os.path.ismount(volume.resolve(strict=True))
                    except (OSError, RuntimeError):
                        mounted = False
                if not mounted:
                    raise StorageVolumeUnmountedError(
                        errno.ENODEV,
                        f"External storage volume '{volume}' is not mounted; "
                        "mount it before accessing profile data",
                        str(candidate),
                    )
    return path


def storage_root_identity(data_dir: str | os.PathLike[str]) -> StorageIdentity:
    """Capture the mounted volume UUID/fsid and directory inode of *data_dir*."""
    require_data_dir_mounted(data_dir)
    raw_str = os.fspath(data_dir)
    joined = raw_str if os.path.isabs(raw_str) else os.path.join(os.getcwd(), raw_str)
    try:
        real = os.path.realpath(joined, strict=True)
        st = os.stat(real)
    except OSError as exc:
        raise StorageVolumeUnmountedError(
            errno.ENODEV,
            f"Profile data directory root '{raw_str}' is unavailable: {exc.strerror or exc}",
            raw_str,
        ) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise StorageVolumeUnmountedError(
            errno.ENODEV,
            f"Profile data directory root '{raw_str}' is not a directory",
            raw_str,
        )
    vol_uuid: str | None = None
    fsid: tuple[int, int] | int | None = None
    try:
        if sys.platform == "darwin":
            vol_uuid, fsid = _darwin_volume_identity(real)
        elif hasattr(os, "statvfs"):
            fsid = os.statvfs(real).f_fsid
    except OSError as exc:
        raise StorageVolumeUnmountedError(
            errno.ENODEV,
            f"Cannot verify storage volume for '{raw_str}': {exc.strerror or exc}",
            raw_str,
        ) from exc
    return StorageIdentity(
        real_path=real,
        volume_uuid=vol_uuid,
        fsid=fsid,
        dev=st.st_dev,
        ino=st.st_ino,
        birthtime=getattr(st, "st_birthtime", None),
    )


def require_same_storage_root(
    data_dir: str | os.PathLike[str], expected: StorageIdentity
) -> StorageIdentity:
    """Fail closed if *data_dir* no longer points to the same mounted volume and root."""
    current = storage_root_identity(data_dir)
    if current != expected:
        raise StorageVolumeUnmountedError(
            errno.ENODEV,
            f"External storage volume or data root '{data_dir}' changed during operation; "
            "aborting to protect profile data",
            os.fspath(data_dir),
        )
    return current


def _load_profile_store_rows(
    profiles_file: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, object]] | None:
    path = pathlib.Path(profiles_file if profiles_file is not None else PROFILES_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {key: val for key, val in raw.items() if isinstance(key, str) and isinstance(val, dict)}


def _load_known_profile_names(
    profiles_file: str | os.PathLike[str] | None = None,
) -> set[str]:
    rows = _load_profile_store_rows(profiles_file)
    return set(rows) if rows is not None else set()


def _is_safe_single_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in (".", "..")
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _read_verified_stage_name(slot_path: pathlib.Path) -> str | None:
    if slot_path.is_symlink() or not slot_path.is_dir():
        return None
    meta_path = slot_path / "meta.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("ronc_staged_delete") != 1:
        return None
    owner = meta.get("name")
    if not isinstance(owner, str) or not owner.strip():
        return None
    if not os.path.lexists(slot_path / "payload"):
        return None
    return owner


def _read_pending_rename(journal_path: pathlib.Path) -> tuple[str, str, int] | None:
    if journal_path.is_symlink() or not journal_path.is_file():
        return None
    try:
        meta = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("ronc_pending_rename") != 1:
        return None
    old_name = meta.get("old_name")
    new_name = meta.get("new_name")
    ino = meta.get("ino")
    if (
        not _is_safe_single_name(old_name)
        or not _is_safe_single_name(new_name)
        or old_name == new_name
        or type(ino) is not int
    ):
        return None
    return old_name, new_name, ino


def _parse_pending_import(
    meta: object,
) -> tuple[str | None, str | None, str, str, int | None, int] | None:
    if not isinstance(meta, dict) or meta.get("ronc_pending_import") != 1:
        return None
    tx_id = meta.get("tx_id")
    existing_name = meta.get("existing_name")
    target_name = meta.get("target_name")
    temp_name = meta.get("temp_name")
    prev_ino = meta.get("prev_ino")
    incoming_ino = meta.get("incoming_ino")
    if (
        (tx_id is not None and not is_valid_import_tx(tx_id))
        or (existing_name is not None and not _is_safe_single_name(existing_name))
        or not _is_safe_single_name(target_name)
        or not _is_safe_single_name(temp_name)
        or not temp_name.startswith(".import_")
        or (prev_ino is not None and type(prev_ino) is not int)
        or type(incoming_ino) is not int
    ):
        return None
    return tx_id, existing_name, target_name, temp_name, prev_ino, incoming_ino


def _read_pending_import(
    journal_path: pathlib.Path,
) -> tuple[str | None, str | None, str, str, int | None, int] | None:
    if journal_path.is_symlink() or not journal_path.is_file():
        return None
    try:
        meta = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse_pending_import(meta)


def _lstat_ino(path: pathlib.Path) -> int | None:
    try:
        return os.lstat(path).st_ino
    except OSError:
        return None


def _classify_import_journal(
    lstat_rel: Callable[[tuple[str, ...]], int | None],
    tx_id: str | None,
    existing_name: str | None,
    target_name: str,
    temp_name: str,
    prev_ino: int | None,
    incoming_ino: int,
    store_rows: dict[str, dict[str, object]] | None,
    store_known: bool,
) -> str | None:
    """Return active fence import_state, or None only when metadata & directory are verified."""
    prev_cur_ino = lstat_rel((temp_name, "previous"))
    prev_still_staged = (
        prev_ino is not None
        and prev_cur_ino is not None
        and (prev_ino <= 0 or prev_cur_ino == prev_ino)
    )
    target_cur_ino = lstat_rel((target_name,))
    incoming_published = incoming_ino > 0 and target_cur_ino == incoming_ino
    existing_cur_ino = lstat_rel((existing_name,)) if existing_name is not None else None
    has_prior_dir = existing_name is not None and prev_ino is not None and prev_ino > 0
    orig_mismatch = has_prior_dir and existing_cur_ino != prev_ino
    unexpected_no_prior_dir = (
        existing_name is not None
        and prev_ino is None
        and (existing_cur_ino is not None or target_cur_ino is not None)
    )
    unexpected_target_copy = (
        has_prior_dir
        and target_name != existing_name
        and target_cur_ino is not None
        and target_cur_ino != prev_ino
    )

    if store_rows is not None:
        target_row = store_rows.get(target_name)
        existing_row = store_rows.get(existing_name) if existing_name is not None else None
        committed = (
            tx_id is not None and target_row is not None and target_row.get("_import_tx") == tx_id
        )
        if committed:
            if incoming_published and not prev_still_staged:
                return None
            if incoming_published and prev_still_staged:
                return "committed_cleanup"
            return "committed_mismatch_staged" if prev_still_staged else "committed_mismatch"
        if existing_name is not None:
            if tx_id is None:
                return (
                    "unresolved"
                    if (
                        prev_still_staged
                        or incoming_published
                        or orig_mismatch
                        or unexpected_no_prior_dir
                        or unexpected_target_copy
                    )
                    else None
                )
            if existing_row is None:
                return None
            if prev_still_staged and incoming_published:
                return "pre_commit_both"
            if prev_still_staged:
                return "pre_commit_staged"
            if incoming_published or unexpected_target_copy:
                return "pre_commit_incoming"
            if orig_mismatch:
                return "pre_commit_mismatch"
            if unexpected_no_prior_dir:
                return (
                    "pre_commit_incoming"
                    if target_cur_ino is not None
                    else "pre_commit_unexpected_orig"
                )
            return None
        if target_cur_ino is None:
            return None
        return "unresolved" if tx_id is None else "pre_commit_incoming"

    if existing_name is None and not store_known:
        return "pre_commit_incoming" if target_cur_ino is not None else None
    if (
        prev_still_staged
        or incoming_published
        or orig_mismatch
        or unexpected_no_prior_dir
        or unexpected_target_copy
    ):
        return "unresolved"
    return None


def scan_pending_deletes(
    data_dir: str | os.PathLike[str],
    *,
    known_profiles: Collection[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> dict[str, list[PendingDeleteEntry]]:
    """Map profile casefold names to staged deletes, interrupted renames/imports, and legacy siblings."""
    root = require_data_dir_mounted(data_dir)
    found: dict[str, list[PendingDeleteEntry]] = {}
    rename_candidates: list[tuple[pathlib.Path, str, str, str, int]] = []
    import_candidates: list[
        tuple[pathlib.Path, str, str | None, str | None, str, str, int | None, int]
    ] = []

    # 1. Verified internal staging namespace (outside user-addressable profile names).
    stage_root = root / STAGING_DIR_NAME
    if not stage_root.is_symlink():
        try:
            with os.scandir(stage_root) as entries:
                for entry in entries:
                    if _STAGED_SLOT_RE.fullmatch(entry.name):
                        slot_path = stage_root / entry.name
                        owner = _read_verified_stage_name(slot_path)
                        if owner is not None:
                            found.setdefault(owner.casefold(), []).append(
                                PendingDeleteEntry(
                                    path=slot_path, slot_name=entry.name, verified=True
                                )
                            )
                    elif _PENDING_RENAME_RE.fullmatch(entry.name):
                        journal_path = stage_root / entry.name
                        parsed = _read_pending_rename(journal_path)
                        if parsed is not None:
                            old_name, new_name, ino = parsed
                            rename_candidates.append(
                                (journal_path, entry.name, old_name, new_name, ino)
                            )
                    elif _PENDING_IMPORT_RE.fullmatch(entry.name):
                        journal_path = stage_root / entry.name
                        parsed_imp = _read_pending_import(journal_path)
                        if parsed_imp is not None:
                            (
                                tx_id,
                                existing_name,
                                target_name,
                                temp_name,
                                prev_ino,
                                incoming_ino,
                            ) = parsed_imp
                            import_candidates.append(
                                (
                                    journal_path,
                                    entry.name,
                                    tx_id,
                                    existing_name,
                                    target_name,
                                    temp_name,
                                    prev_ino,
                                    incoming_ino,
                                )
                            )
        except FileNotFoundError:
            require_data_dir_mounted(data_dir)
        except OSError:
            require_data_dir_mounted(data_dir)
            raise

    # 2. Legacy sibling directories directly under data_dir: ignore any directory claimed
    #    by an existing profile row; treat unclaimed matches as manual-recovery-only.
    legacy_candidates: list[tuple[str, str]] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                match = _STAGED_DELETE_RE.fullmatch(entry.name)
                if match:
                    legacy_candidates.append((entry.name, match.group("name")))
    except FileNotFoundError:
        require_data_dir_mounted(data_dir)
        return {key: sorted(items, key=lambda item: item.path.name) for key, items in found.items()}
    except OSError:
        require_data_dir_mounted(data_dir)
        raise

    exact_claimed: set[str] | None = None
    claimed: set[str] | None = None
    store_rows: dict[str, dict[str, object]] | None = None
    store_known = False
    if legacy_candidates or rename_candidates or import_candidates:
        store_path = pathlib.Path(profiles_file if profiles_file is not None else PROFILES_FILE)
        if import_candidates or known_profiles is None:
            store_rows = _load_profile_store_rows(store_path)
        if known_profiles is not None:
            exact_claimed = set(known_profiles)
            claimed = {name.casefold() for name in exact_claimed}
            store_known = True
        else:
            store_known = store_path.exists()
            exact_claimed = set(store_rows) if store_rows is not None else set()
            claimed = {name.casefold() for name in exact_claimed}

    for journal_path, slot_name, old_name, new_name, expected_ino in rename_candidates:
        old_cf, new_cf = old_name.casefold(), new_name.casefold()
        if store_known and exact_claimed is not None:
            if old_name not in exact_claimed or new_name in exact_claimed:
                continue
            if old_cf != new_cf and claimed is not None and new_cf in claimed:
                continue
        new_ino = _lstat_ino(root / new_name)
        if new_ino is None or (expected_ino > 0 and new_ino != expected_ino):
            continue
        old_ino = _lstat_ino(root / old_name)
        if old_ino is not None and (expected_ino <= 0 or old_ino == expected_ino):
            continue
        rename_entry = PendingDeleteEntry(
            path=journal_path,
            slot_name=slot_name,
            verified=False,
            kind="rename",
            rename_from=old_name,
            rename_to=new_name,
        )
        found.setdefault(old_cf, []).append(rename_entry)
        if new_cf != old_cf:
            found.setdefault(new_cf, []).append(rename_entry)

    for (
        journal_path,
        slot_name,
        tx_id,
        existing_name,
        target_name,
        temp_name,
        prev_ino,
        incoming_ino,
    ) in import_candidates:
        import_state = _classify_import_journal(
            lambda parts: _lstat_ino(root.joinpath(*parts)),
            tx_id,
            existing_name,
            target_name,
            temp_name,
            prev_ino,
            incoming_ino,
            store_rows,
            store_known,
        )
        if import_state is None:
            continue

        import_entry = PendingDeleteEntry(
            path=journal_path,
            slot_name=slot_name,
            verified=False,
            kind="import",
            rename_from=existing_name,
            rename_to=target_name,
            import_temp=temp_name,
            import_state=import_state,
        )
        target_cf = target_name.casefold()
        found.setdefault(target_cf, []).append(import_entry)
        if existing_name is not None and existing_name.casefold() != target_cf:
            found.setdefault(existing_name.casefold(), []).append(import_entry)

    if legacy_candidates and claimed is not None:
        for entry_name, owner_name in legacy_candidates:
            if entry_name.casefold() in claimed:
                continue
            found.setdefault(owner_name.casefold(), []).append(
                PendingDeleteEntry(path=root / entry_name, slot_name=None, verified=False)
            )

    return {key: sorted(items, key=lambda item: item.path.name) for key, items in found.items()}


def find_pending_delete_entries(
    data_dir: str | os.PathLike[str],
    name: str,
    *,
    known_profiles: Collection[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> list[PendingDeleteEntry]:
    """Return verified and unverified legacy pending delete entries matching *name*."""
    return scan_pending_deletes(
        data_dir, known_profiles=known_profiles, profiles_file=profiles_file
    ).get(name.casefold(), [])


def find_pending_deletes(
    data_dir: str | os.PathLike[str],
    name: str,
    *,
    known_profiles: Collection[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> list[pathlib.Path]:
    """Return staged delete directory paths matching *name*."""
    return [
        entry.path
        for entry in find_pending_delete_entries(
            data_dir, name, known_profiles=known_profiles, profiles_file=profiles_file
        )
    ]


def format_pending_delete_message(
    name: str, profile_path: str | os.PathLike[str], entries: list[PendingDeleteEntry]
) -> str:
    imports = [entry for entry in entries if entry.kind == "import"]
    if imports:
        first = imports[0]
        data_root = pathlib.Path(profile_path).parent
        src = first.rename_from or first.rename_to or name
        dst = first.rename_to or name
        orig_path = data_root / src
        target_path = data_root / dst
        prev_dir = data_root / (first.import_temp or "") / "previous"
        state = first.import_state
        if state == "committed_cleanup":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: metadata "
                f"committed for '{dst}'); '{target_path}' already holds the committed session "
                f"while the previous session backup remains at '{prev_dir}' — do not overwrite "
                f"'{target_path}' with '{prev_dir}'; archive or remove '{prev_dir}' to finish "
                "cleanup before accessing profile data"
            )
        if state == "committed_mismatch_staged":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: metadata "
                f"committed for '{dst}'); '{target_path}' does not match the committed incoming "
                f"session while the previous backup remains at '{prev_dir}' — restore the "
                f"committed incoming directory to '{target_path}' and archive or remove "
                f"'{prev_dir}' before accessing profile data"
            )
        if state == "committed_mismatch":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: metadata "
                f"committed for '{dst}'); '{target_path}' does not match the committed incoming "
                "session — do not pair old session data with committed metadata; restore the "
                f"committed incoming directory to '{target_path}' before accessing profile data"
            )
        if state == "pre_commit_both":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: "
                f"'{src}' -> '{dst}'); metadata still references '{src}', uncommitted incoming "
                f"data is at '{target_path}', and the original session backup is at '{prev_dir}' "
                f"— move '{target_path}' aside and restore '{prev_dir}' to '{orig_path}' before "
                "accessing profile data"
            )
        if state == "pre_commit_staged":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: "
                f"'{src}' -> '{dst}'); metadata still references '{src}' and the original "
                f"session backup is at '{prev_dir}' — restore '{prev_dir}' to '{orig_path}' "
                "before accessing profile data"
            )
        if state == "pre_commit_incoming":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: "
                f"'{src}' -> '{dst}'); uncommitted incoming data at '{target_path}' does not "
                f"match current metadata — inspect and move or remove '{target_path}' before "
                "accessing profile data"
            )
        if state == "pre_commit_unexpected_orig":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: "
                f"'{src}' -> '{dst}'); unexpected data directory at '{orig_path}' does not "
                f"match current metadata — inspect and move or remove '{orig_path}' before "
                "accessing profile data"
            )
        if state == "pre_commit_mismatch":
            return (
                f"Profile '{name}' has an interrupted import ({first.path.name}: "
                f"'{src}' -> '{dst}'); metadata still references '{src}', but '{orig_path}' "
                "does not match the original session directory — restore the original "
                f"session directory to '{orig_path}' before accessing profile data"
            )
        return (
            f"Profile '{name}' has an interrupted import ({first.path.name}: "
            f"'{src}' -> '{dst}'); metadata state could not be verified — inspect profiles.json "
            f"and '{target_path}' before accessing profile data"
        )
    renames = [entry for entry in entries if entry.kind == "rename"]
    if renames:
        first = renames[0]
        return (
            f"Profile '{name}' has an interrupted rename ({first.path.name}: "
            f"'{first.rename_from}' -> '{first.rename_to}'); restore '{first.rename_to}' "
            f"back to '{first.rename_from}' before accessing profile data"
        )
    staged_names = ", ".join(entry.path.name for entry in entries)
    staged_list = ", ".join(str(entry.path) for entry in entries)
    if all(entry.verified for entry in entries):
        return (
            f"Profile '{name}' has interrupted delete data ({staged_names}) staged at "
            f"'{staged_list}'; restore payload to '{profile_path}' or retry delete to finish cleanup"
        )
    return (
        f"Profile '{name}' has unverified legacy staged data ({staged_names}) at "
        f"'{staged_list}'; inspect and resolve manually before continuing"
    )


def require_no_pending_delete(
    profile_dir: str | os.PathLike[str],
    *,
    known_profiles: Collection[str] | None = None,
    profiles_file: str | os.PathLike[str] | None = None,
) -> pathlib.Path:
    """Fence profile data operations while an interrupted staged delete remains on disk."""
    path = pathlib.Path(profile_dir)
    entries = find_pending_delete_entries(
        path.parent, path.name, known_profiles=known_profiles, profiles_file=profiles_file
    )
    if entries:
        raise PendingProfileDeleteError(
            errno.EBUSY,
            format_pending_delete_message(path.name, path, entries),
            str(entries[0].path),
        )
    return path


class PinnedStorageRoot:
    """Pin a verified data root (with a POSIX dir_fd) so path swaps never mutate another disk."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        mount_check: Callable[[str | os.PathLike[str]], pathlib.Path] | None = None,
        identity_fn: Callable[[str | os.PathLike[str]], StorageIdentity] | None = None,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self._mount_check = mount_check or require_data_dir_mounted
        self._identity_fn = identity_fn or storage_root_identity
        self.identity: StorageIdentity | None = None
        self.fd: int | None = None

    def __enter__(self) -> PinnedStorageRoot:
        self._mount_check(self.data_dir)
        self.identity = self._identity_fn(self.data_dir)
        if os.name != "nt" and os.rename in os.supports_dir_fd:
            open_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(self.identity.real_path, open_flags)
            except OSError as exc:
                raise StorageVolumeUnmountedError(
                    errno.ENODEV,
                    f"Profile data directory root '{self.data_dir}' is unavailable: "
                    f"{exc.strerror or exc}",
                    str(self.data_dir),
                ) from exc
            try:
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) != (self.identity.dev, self.identity.ino):
                    raise StorageVolumeUnmountedError(
                        errno.ENODEV,
                        f"External storage volume or data root '{self.data_dir}' changed while "
                        "opening; aborting to protect profile data",
                        str(self.data_dir),
                    )
                self._verify_path_identity()
            except BaseException:
                os.close(fd)
                raise
            self.fd = fd
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            with contextlib.suppress(OSError):
                os.close(fd)

    def _verify_path_identity(self) -> StorageIdentity:
        assert self.identity is not None
        self._mount_check(self.data_dir)
        current = self._identity_fn(self.data_dir)
        if current != self.identity:
            raise StorageVolumeUnmountedError(
                errno.ENODEV,
                f"External storage volume or data root '{self.data_dir}' changed during "
                "operation; aborting to protect profile data",
                str(self.data_dir),
            )
        return current

    def verify(self) -> StorageIdentity:
        assert self.identity is not None
        if self.fd is not None:
            try:
                st = os.fstat(self.fd)
            except OSError as exc:
                raise StorageVolumeUnmountedError(
                    errno.ENODEV,
                    f"External storage volume or data root '{self.data_dir}' became unavailable",
                    str(self.data_dir),
                ) from exc
            if (st.st_dev, st.st_ino) != (self.identity.dev, self.identity.ino):
                raise StorageVolumeUnmountedError(
                    errno.ENODEV,
                    f"External storage volume or data root '{self.data_dir}' changed during "
                    "operation; aborting to protect profile data",
                    str(self.data_dir),
                )
        return self._verify_path_identity()

    def _open_dir(self, name: str, *, dir_fd: int | None) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        return os.open(name, flags, dir_fd=dir_fd)

    def exists_or_symlink(self, name: str) -> bool:
        try:
            os.lstat(name if self.fd is not None else str(self.data_dir / name), dir_fd=self.fd)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            self.verify()
            raise

    def exists(self, name: str) -> bool:
        try:
            os.stat(name if self.fd is not None else str(self.data_dir / name), dir_fd=self.fd)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            self.verify()
            raise

    def same_entry(self, name_a: str, name_b: str) -> bool:
        try:
            st_a = os.stat(
                name_a if self.fd is not None else str(self.data_dir / name_a), dir_fd=self.fd
            )
            st_b = os.stat(
                name_b if self.fd is not None else str(self.data_dir / name_b), dir_fd=self.fd
            )
            return os.path.samestat(st_a, st_b)
        except OSError:
            return False

    def rename_child(self, src_name: str, dst_name: str) -> None:
        self.verify()
        try:
            if self.fd is not None and pathlib.Path.rename is _ORIG_PATH_RENAME:
                os.rename(src_name, dst_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                (self.data_dir / src_name).rename(self.data_dir / dst_name)
        except OSError:
            self.verify()
            raise

    def mkdir_child(self, name: str, mode: int = 0o700) -> pathlib.Path:
        self.verify()
        try:
            if self.fd is not None:
                os.mkdir(name, mode, dir_fd=self.fd)
            else:
                (self.data_dir / name).mkdir(mode=mode)
        except OSError:
            self.verify()
            raise
        return self.data_dir / name

    def remove_child_tree(
        self,
        name: str,
        *,
        ignore_errors: bool = False,
        rmtree_fn: Callable[..., object] = shutil.rmtree,
    ) -> None:
        if not ignore_errors:
            self.verify()
        elif self.fd is None:
            try:
                self.verify()
            except OSError:
                return
        try:
            if self.fd is not None:
                try:
                    st = os.lstat(name, dir_fd=self.fd)
                except FileNotFoundError:
                    return
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    os.unlink(name, dir_fd=self.fd)
                elif rmtree_fn is _ORIG_SHUTIL_RMTREE:
                    shutil.rmtree(name, ignore_errors=ignore_errors, dir_fd=self.fd)
                else:
                    rmtree_fn(self.data_dir / name, ignore_errors=ignore_errors)
            else:
                target = self.data_dir / name
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    target.unlink()
                elif target.exists():
                    rmtree_fn(target, ignore_errors=ignore_errors)
        except OSError:
            if not ignore_errors:
                self.verify()
                raise

    def rename_child_into_subdir(self, src_name: str, subdir_name: str, dst_name: str) -> None:
        self.verify()
        try:
            if self.fd is not None and pathlib.Path.rename is _ORIG_PATH_RENAME:
                sub_fd = self._open_dir(subdir_name, dir_fd=self.fd)
                try:
                    os.rename(src_name, dst_name, src_dir_fd=self.fd, dst_dir_fd=sub_fd)
                finally:
                    os.close(sub_fd)
            else:
                (self.data_dir / src_name).rename(self.data_dir / subdir_name / dst_name)
        except OSError:
            self.verify()
            raise

    def rename_from_subdir(
        self, subdir_parts: tuple[str, ...], src_name: str, dst_name: str
    ) -> None:
        self.verify()
        try:
            if self.fd is not None and pathlib.Path.rename is _ORIG_PATH_RENAME:
                fds: list[int] = []
                parent_fd = self.fd
                try:
                    for part in subdir_parts:
                        parent_fd = self._open_dir(part, dir_fd=parent_fd)
                        fds.append(parent_fd)
                    os.rename(src_name, dst_name, src_dir_fd=parent_fd, dst_dir_fd=self.fd)
                finally:
                    for fd in reversed(fds):
                        os.close(fd)
            else:
                (self.data_dir.joinpath(*subdir_parts) / src_name).rename(self.data_dir / dst_name)
        except OSError:
            self.verify()
            raise

    def stage_profile_for_delete(self, name: str) -> PendingDeleteEntry:
        """Atomically stage *name* into the verified internal staging namespace via dir_fd."""
        self.verify()
        slot_name = f"{STAGED_DELETE_PREFIX}{uuid.uuid4().hex}"
        slot_path = self.data_dir / STAGING_DIR_NAME / slot_name
        meta_bytes = json.dumps({"ronc_staged_delete": 1, "name": name}).encode("utf-8")
        if self.fd is not None:
            try:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(STAGING_DIR_NAME, 0o700, dir_fd=self.fd)
                stage_fd = self._open_dir(STAGING_DIR_NAME, dir_fd=self.fd)
                try:
                    os.mkdir(slot_name, 0o700, dir_fd=stage_fd)
                    slot_fd = self._open_dir(slot_name, dir_fd=stage_fd)
                    try:
                        meta_flags = (
                            os.O_CREAT
                            | os.O_EXCL
                            | os.O_WRONLY
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0)
                        )
                        meta_fd = os.open("meta.json", meta_flags, 0o600, dir_fd=slot_fd)
                        with os.fdopen(meta_fd, "wb") as out:
                            out.write(meta_bytes)
                            out.flush()
                            os.fsync(out.fileno())
                        if pathlib.Path.rename is _ORIG_PATH_RENAME:
                            os.rename(name, "payload", src_dir_fd=self.fd, dst_dir_fd=slot_fd)
                        else:
                            (self.data_dir / name).rename(slot_path / "payload")
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.unlink("meta.json", dir_fd=slot_fd)
                        os.close(slot_fd)
                        with contextlib.suppress(OSError):
                            os.rmdir(slot_name, dir_fd=stage_fd)
                        raise
                    os.close(slot_fd)
                finally:
                    os.close(stage_fd)
            except OSError:
                self.verify()
                raise
        else:
            try:
                stage_root = self.data_dir / STAGING_DIR_NAME
                stage_root.mkdir(mode=0o700, exist_ok=True)
                slot_path.mkdir(mode=0o700)
                (slot_path / "meta.json").write_bytes(meta_bytes)
                (self.data_dir / name).rename(slot_path / "payload")
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(slot_path)
                self.verify()
                raise
        return PendingDeleteEntry(path=slot_path, slot_name=slot_name, verified=True)

    def restore_staged_delete(self, entry: PendingDeleteEntry, target_name: str) -> None:
        """Restore a verified staged payload back to *target_name* via dir_fd."""
        if not entry.verified or not entry.slot_name:
            raise PendingProfileDeleteError(
                errno.EBUSY,
                f"Cannot restore unverified staged entry '{entry.path}'",
                str(entry.path),
            )
        self.verify()
        if self.fd is not None:
            stage_fd = self._open_dir(STAGING_DIR_NAME, dir_fd=self.fd)
            try:
                slot_fd = self._open_dir(entry.slot_name, dir_fd=stage_fd)
                try:
                    if pathlib.Path.rename is _ORIG_PATH_RENAME:
                        os.rename("payload", target_name, src_dir_fd=slot_fd, dst_dir_fd=self.fd)
                    else:
                        (entry.path / "payload").rename(self.data_dir / target_name)
                    with contextlib.suppress(OSError):
                        os.unlink("meta.json", dir_fd=slot_fd)
                finally:
                    os.close(slot_fd)
                with contextlib.suppress(OSError):
                    os.rmdir(entry.slot_name, dir_fd=stage_fd)
            finally:
                os.close(stage_fd)
        else:
            (entry.path / "payload").rename(self.data_dir / target_name)
            with contextlib.suppress(OSError):
                (entry.path / "meta.json").unlink(missing_ok=True)
                entry.path.rmdir()

    def remove_staged_delete(
        self,
        entry: PendingDeleteEntry,
        *,
        rmtree_fn: Callable[..., object] = shutil.rmtree,
    ) -> None:
        """Permanently delete a verified staged delete slot via dir_fd."""
        if not entry.verified or not entry.slot_name:
            raise PendingProfileDeleteError(
                errno.EBUSY,
                f"Refusing to remove unverified staged entry '{entry.path}'",
                str(entry.path),
            )
        self.verify()
        if self.fd is not None:
            stage_fd = self._open_dir(STAGING_DIR_NAME, dir_fd=self.fd)
            try:
                slot_fd = self._open_dir(entry.slot_name, dir_fd=stage_fd)
                try:
                    st = os.lstat("payload", dir_fd=slot_fd)
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                        os.unlink("payload", dir_fd=slot_fd)
                    elif rmtree_fn is _ORIG_SHUTIL_RMTREE:
                        shutil.rmtree("payload", dir_fd=slot_fd)
                    else:
                        rmtree_fn(entry.path / "payload")
                    with contextlib.suppress(OSError):
                        os.unlink("meta.json", dir_fd=slot_fd)
                finally:
                    os.close(slot_fd)
                os.rmdir(entry.slot_name, dir_fd=stage_fd)
            finally:
                os.close(stage_fd)
            with contextlib.suppress(OSError):
                os.rmdir(STAGING_DIR_NAME, dir_fd=self.fd)
        else:
            payload = entry.path / "payload"
            if payload.is_symlink() or not payload.is_dir():
                payload.unlink()
            else:
                rmtree_fn(payload)
            with contextlib.suppress(OSError):
                (entry.path / "meta.json").unlink(missing_ok=True)
            entry.path.rmdir()

    def _validate_rel_parts(self, parts: tuple[str, ...]) -> None:
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\\" in part:
                raise ValueError(f"Unsafe relative storage path component: {part!r}")

    @contextlib.contextmanager
    def _subdir_fd(self, rel_parts: tuple[str, ...], *, create: bool = False, mode: int = 0o755):
        self._validate_rel_parts(rel_parts)
        if self.fd is None:
            target = self.data_dir.joinpath(*rel_parts)
            if create:
                target.mkdir(mode=mode, parents=True, exist_ok=True)
            yield None
            return
        cur_fd = self.fd
        opened: list[int] = []
        try:
            for part in rel_parts:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode, dir_fd=cur_fd)
                cur_fd = self._open_dir(part, dir_fd=cur_fd)
                opened.append(cur_fd)
            yield cur_fd
        finally:
            for fd in reversed(opened):
                with contextlib.suppress(OSError):
                    os.close(fd)

    def ensure_subdir(self, rel_parts: tuple[str, ...], mode: int = 0o755) -> pathlib.Path:
        self.verify()
        try:
            with self._subdir_fd(rel_parts, create=True, mode=mode):
                pass
        except OSError:
            self.verify()
            raise
        return self.data_dir.joinpath(*rel_parts)

    def read_file_bytes(self, rel_dir_parts: tuple[str, ...], filename: str) -> bytes | None:
        self._validate_rel_parts((*rel_dir_parts, filename))
        if self.fd is not None:
            try:
                with self._subdir_fd(rel_dir_parts, create=False) as dir_fd:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                    file_fd = os.open(filename, flags, dir_fd=dir_fd)
                    with os.fdopen(file_fd, "rb") as stream:
                        return stream.read()
            except FileNotFoundError:
                return None
            except OSError:
                self.verify()
                raise
        target = self.data_dir.joinpath(*rel_dir_parts, filename)
        if not target.is_file():
            return None
        return target.read_bytes()

    def write_file_atomic(
        self,
        rel_dir_parts: tuple[str, ...],
        filename: str,
        content: bytes,
        *,
        mode: int = 0o644,
    ) -> pathlib.Path:
        self._validate_rel_parts((*rel_dir_parts, filename))
        self.verify()
        tmp_name = f".ronc_tmp_{uuid.uuid4().hex}.tmp"
        if self.fd is not None:
            try:
                with self._subdir_fd(rel_dir_parts, create=True) as dir_fd:
                    flags = (
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_WRONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    file_fd = os.open(tmp_name, flags, mode, dir_fd=dir_fd)
                    try:
                        with os.fdopen(file_fd, "wb") as out:
                            out.write(content)
                            out.flush()
                            os.fsync(out.fileno())
                        if pathlib.Path.replace is _ORIG_PATH_REPLACE:
                            os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                        else:
                            dest_dir = self.data_dir.joinpath(*rel_dir_parts)
                            (dest_dir / tmp_name).replace(dest_dir / filename)
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_name, dir_fd=dir_fd)
                        raise
            except OSError:
                self.verify()
                raise
        else:
            dest_dir = self.data_dir.joinpath(*rel_dir_parts)
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_dir / tmp_name
            try:
                tmp_path.write_bytes(content)
                tmp_path.replace(dest_dir / filename)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)
                self.verify()
                raise
        return self.data_dir.joinpath(*rel_dir_parts, filename)

    @contextlib.contextmanager
    def open_file_write(self, rel_dir_parts: tuple[str, ...], filename: str, *, mode: int = 0o644):
        self._validate_rel_parts((*rel_dir_parts, filename))
        if self.fd is not None:
            try:
                with self._subdir_fd(rel_dir_parts, create=True) as dir_fd:
                    flags = (
                        os.O_CREAT
                        | os.O_TRUNC
                        | os.O_WRONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                    )
                    file_fd = os.open(filename, flags, mode, dir_fd=dir_fd)
                    with os.fdopen(file_fd, "wb") as out:
                        yield out
            except OSError:
                self.verify()
                raise
        else:
            dest_dir = self.data_dir.joinpath(*rel_dir_parts)
            dest_dir.mkdir(parents=True, exist_ok=True)
            with open(dest_dir / filename, "wb") as out:
                yield out

    def unlink_file(self, rel_dir_parts: tuple[str, ...], filename: str) -> bool:
        self._validate_rel_parts((*rel_dir_parts, filename))
        self.verify()
        if self.fd is not None:
            try:
                with self._subdir_fd(rel_dir_parts, create=False) as dir_fd:
                    os.unlink(filename, dir_fd=dir_fd)
                return True
            except FileNotFoundError:
                self.verify()
                return False
            except OSError:
                self.verify()
                raise
        target = self.data_dir.joinpath(*rel_dir_parts, filename)
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            self.verify()
            return False
        except OSError:
            self.verify()
            raise

    def rmdir_if_empty(self, rel_parts: tuple[str, ...]) -> None:
        if not rel_parts:
            return
        self._validate_rel_parts(rel_parts)
        if self.fd is not None:
            with (
                contextlib.suppress(OSError),
                self._subdir_fd(rel_parts[:-1], create=False) as parent_fd,
            ):
                os.rmdir(rel_parts[-1], dir_fd=parent_fd)
        else:
            with contextlib.suppress(OSError):
                self.data_dir.joinpath(*rel_parts).rmdir()

    def copy_child_tree(
        self,
        src_name: str,
        dst_rel_parts: tuple[str, ...],
        *,
        ignore_top: Collection[str] = (),
    ) -> None:
        """Copy directory *src_name* to *dst_rel_parts* strictly via dir_fd on POSIX."""
        self._validate_rel_parts((src_name, *dst_rel_parts))
        self.verify()
        ignored = frozenset(ignore_top)
        if self.fd is None:
            src_path = self.data_dir / src_name
            dst_path = self.data_dir.joinpath(*dst_rel_parts)
            shutil.copytree(
                src_path,
                dst_path,
                ignore=lambda source, names: (
                    ignored.intersection(names) if pathlib.Path(source) == src_path else set()
                ),
            )
            return

        def _copy_fds(src_fd: int, dst_fd: int, depth: int) -> None:
            with os.scandir(src_fd) as entries:
                for entry in entries:
                    if depth == 0 and entry.name in ignored:
                        continue
                    st = os.lstat(entry.name, dir_fd=src_fd)
                    if stat.S_ISLNK(st.st_mode):
                        os.symlink(
                            os.readlink(entry.name, dir_fd=src_fd), entry.name, dir_fd=dst_fd
                        )
                    elif stat.S_ISDIR(st.st_mode):
                        os.mkdir(entry.name, stat.S_IMODE(st.st_mode) or 0o755, dir_fd=dst_fd)
                        child_src = self._open_dir(entry.name, dir_fd=src_fd)
                        try:
                            child_dst = self._open_dir(entry.name, dir_fd=dst_fd)
                            try:
                                _copy_fds(child_src, child_dst, depth + 1)
                            finally:
                                os.close(child_dst)
                        finally:
                            os.close(child_src)
                    elif stat.S_ISREG(st.st_mode):
                        rflags = (
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                        )
                        wflags = (
                            os.O_CREAT
                            | os.O_EXCL
                            | os.O_WRONLY
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0)
                        )
                        in_fd = os.open(entry.name, rflags, dir_fd=src_fd)
                        with os.fdopen(in_fd, "rb") as src_stream:
                            out_fd = os.open(
                                entry.name,
                                wflags,
                                stat.S_IMODE(st.st_mode) or 0o644,
                                dir_fd=dst_fd,
                            )
                            with os.fdopen(out_fd, "wb") as dst_stream:
                                shutil.copyfileobj(src_stream, dst_stream)

        try:
            src_fd = self._open_dir(src_name, dir_fd=self.fd)
            try:
                with self._subdir_fd(dst_rel_parts, create=True) as dst_fd:
                    assert dst_fd is not None
                    _copy_fds(src_fd, dst_fd, 0)
            finally:
                os.close(src_fd)
        except OSError:
            self.verify()
            raise

    def record_pending_rename(self, old_name: str, new_name: str) -> str | None:
        """Persist an inode-pinned rename journal in STAGING_DIR_NAME before moving data."""
        if old_name == new_name:
            return None
        self.verify()
        try:
            src_ino = os.lstat(
                old_name if self.fd is not None else str(self.data_dir / old_name),
                dir_fd=self.fd,
            ).st_ino
        except OSError:
            self.verify()
            raise
        journal_name = f"{_PENDING_RENAME_PREFIX}{uuid.uuid4().hex}.json"
        payload = json.dumps(
            {
                "ronc_pending_rename": 1,
                "old_name": old_name,
                "new_name": new_name,
                "ino": src_ino,
            }
        ).encode("utf-8")
        self.write_file_atomic((STAGING_DIR_NAME,), journal_name, payload, mode=0o600)
        return journal_name

    def record_pending_import(
        self,
        existing_name: str | None,
        target_name: str,
        temp_name: str,
        *,
        tx_id: str | None = None,
    ) -> str:
        """Persist an inode-pinned import journal in STAGING_DIR_NAME before moving data."""
        self.verify()
        prev_ino: int | None = None
        if existing_name is not None:
            try:
                prev_ino = os.lstat(
                    existing_name if self.fd is not None else str(self.data_dir / existing_name),
                    dir_fd=self.fd,
                ).st_ino
            except FileNotFoundError:
                prev_ino = None
            except OSError:
                self.verify()
                raise
        try:
            if self.fd is not None:
                with self._subdir_fd((temp_name, "incoming"), create=False) as inc_fd:
                    incoming_ino = os.lstat(target_name, dir_fd=inc_fd).st_ino
            else:
                incoming_ino = os.lstat(self.data_dir / temp_name / "incoming" / target_name).st_ino
        except OSError:
            self.verify()
            raise
        tx = tx_id if is_valid_import_tx(tx_id) else uuid.uuid4().hex
        journal_name = f"{_PENDING_IMPORT_PREFIX}{tx}.json"
        payload = json.dumps(
            {
                "ronc_pending_import": 1,
                "tx_id": tx,
                "existing_name": existing_name,
                "target_name": target_name,
                "temp_name": temp_name,
                "prev_ino": prev_ino,
                "incoming_ino": incoming_ino,
            }
        ).encode("utf-8")
        self.write_file_atomic((STAGING_DIR_NAME,), journal_name, payload, mode=0o600)
        return journal_name

    def _clear_staging_journal(self, journal_name: str | None, *, strict: bool = False) -> None:
        if not journal_name:
            return
        try:
            self.unlink_file((STAGING_DIR_NAME,), journal_name)
            self.rmdir_if_empty((STAGING_DIR_NAME,))
        except OSError:
            if strict:
                raise

    def clear_pending_rename(self, journal_name: str | None) -> None:
        """Remove a completed or rolled-back rename journal via dir_fd."""
        self._clear_staging_journal(journal_name)

    def clear_pending_import(self, journal_name: str | None, *, strict: bool = False) -> None:
        """Remove a completed or rolled-back import journal via dir_fd."""
        self._clear_staging_journal(journal_name, strict=strict)

    def _lstat_rel_ino(self, rel_parts: tuple[str, ...]) -> int | None:
        if self.fd is None:
            return _lstat_ino(self.data_dir.joinpath(*rel_parts))
        try:
            if len(rel_parts) == 1:
                return os.lstat(rel_parts[0], dir_fd=self.fd).st_ino
            with self._subdir_fd(rel_parts[:-1], create=False) as parent_fd:
                return os.lstat(rel_parts[-1], dir_fd=parent_fd).st_ino
        except OSError:
            return None

    def clear_resolved_import_journals(
        self,
        names: Collection[str],
        *,
        profiles_file: str | os.PathLike[str] | None = None,
        strict: bool = False,
    ) -> None:
        """Retire already-resolved import journals touching *names* strictly via dir_fd."""
        target_cfs = {name.casefold() for name in names if name}
        if not target_cfs:
            return
        parsed_entries: list[tuple[str, str | None, str | None, str, str, int | None, int]] = []
        if self.fd is not None:
            try:
                with self._subdir_fd((STAGING_DIR_NAME,), create=False) as stage_fd:
                    assert stage_fd is not None
                    rflags = (
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                    )
                    with os.scandir(stage_fd) as entries:
                        for entry in entries:
                            if not _PENDING_IMPORT_RE.fullmatch(entry.name):
                                continue
                            try:
                                fd = os.open(entry.name, rflags, dir_fd=stage_fd)
                            except OSError:
                                continue
                            try:
                                with os.fdopen(fd, "rb") as stream:
                                    meta = json.loads(stream.read().decode("utf-8"))
                            except (OSError, ValueError):
                                continue
                            parsed = _parse_pending_import(meta)
                            if parsed is not None:
                                parsed_entries.append((entry.name, *parsed))
            except FileNotFoundError:
                return
            except OSError:
                self.verify()
                if strict:
                    raise
                return
        else:
            stage_root = self.data_dir / STAGING_DIR_NAME
            if not stage_root.is_dir() or stage_root.is_symlink():
                return
            try:
                with os.scandir(stage_root) as entries:
                    for entry in entries:
                        if not _PENDING_IMPORT_RE.fullmatch(entry.name):
                            continue
                        parsed = _read_pending_import(stage_root / entry.name)
                        if parsed is not None:
                            parsed_entries.append((entry.name, *parsed))
            except FileNotFoundError:
                return
            except OSError:
                self.verify()
                if strict:
                    raise
                return
        if not parsed_entries:
            return
        self.verify()
        store_path = pathlib.Path(profiles_file if profiles_file is not None else PROFILES_FILE)
        store_known = store_path.exists()
        store_rows = _load_profile_store_rows(store_path)
        for (
            journal_name,
            tx_id,
            existing_name,
            target_name,
            temp_name,
            prev_ino,
            incoming_ino,
        ) in parsed_entries:
            if target_name.casefold() not in target_cfs and (
                existing_name is None or existing_name.casefold() not in target_cfs
            ):
                continue
            if (
                _classify_import_journal(
                    self._lstat_rel_ino,
                    tx_id,
                    existing_name,
                    target_name,
                    temp_name,
                    prev_ino,
                    incoming_ino,
                    store_rows,
                    store_known,
                )
                is None
            ):
                self.clear_pending_import(journal_name, strict=strict)


def ensure_data_dir(data_dir: str | os.PathLike[str]) -> pathlib.Path:
    """Ensure *data_dir* exists after verifying its external volume is mounted."""
    path = require_data_dir_mounted(data_dir)
    path.mkdir(exist_ok=True, parents=True)
    return path


def _env(key: str, default: str) -> str:
    """Read the FOXPROFILE_<key> environment variable."""
    return os.getenv(f"FOXPROFILE_{key}", default)


PROFILES_FILE = _env("PROFILES_FILE", "profiles.json")
PROXY_POOL_FILE = _env("PROXY_POOL_FILE", "proxies.json")
WARMING_FILE = _env("WARMING_FILE", "warming_tasks.json")
EXTENSIONS_FILE = _env("EXTENSIONS_FILE", "extensions.json")
EXTENSIONS_DIR = _env("EXTENSIONS_DIR", "extensions_data")
PREFERENCES_FILE = _env("PREFERENCES_FILE", "preferences.json")
DATA_DIR = _env("DATA_DIR", "camoufox_data")
LOG_DIR = _env("LOG_DIR", "logs")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
PROXY_CHECK_TIMEOUT = int(_env("PROXY_TIMEOUT", "10"))
LANGUAGE = _env("LANG", "en")
THEME_MODE = _env("THEME", "dark").strip().lower()

API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = int(_env("API_PORT", "8000"))

# Seconds the API waits for a launched browser to report ready or failed.
LAUNCH_WAIT_TIMEOUT = int(_env("LAUNCH_TIMEOUT", "90"))

FINGERPRINT_FILE = "fingerprint.json"


def _headless(value: str) -> bool | str:
    """'true'/'false', or 'virtual' (Linux: hidden Xvfb display, more stealthy)."""
    value = value.strip().lower()
    if value == "virtual":
        return "virtual"
    return value in ("1", "true", "yes")


# Browsers open hidden when true; server mode turns this on by default.
HEADLESS = _headless(_env("HEADLESS", "false"))

# Reopen the tabs a profile had open when it was last stopped.
RESTORE_TABS = _env("RESTORE_TABS", "true").strip().lower() in ("1", "true", "yes")

# Shared secret for the REST API, the web panel and the MCP server. Required
# whenever the API listens on anything other than the loopback interface.
API_TOKEN = _env("API_TOKEN", "")
