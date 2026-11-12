"""Read-only core inventory and explicit, checksum-pinned staging.

Inventory/staging never changes profiles. Explicit apply verifies a private
backup before changing only future core binding; never starts a browser.
"""

from __future__ import annotations

import configparser
import contextlib
import importlib.metadata
import json
import os
import platform
import threading
from pathlib import Path

from chrome_engine import fetch
from chrome_engine.release import RELEASES, Asset, Release, UnsupportedPlatformError, host_platform

from .lease import profile_lease

# Reviewed paired stack. Add candidates only after compatibility/E2E review.
CAMOUFOX_VERSION = "158.0.1-beta.41"
CAMOUFOX_SHA = "9038f86d666c9780b6fff199d488737b8596c01f24e3e517845a0ecfb0cf8add"
CAMOUFOX_RELEASE = Release(
    CAMOUFOX_VERSION,
    {
        "macos": Asset(
            "https://github.com/daijro/camoufox/releases/download/v158.0.1-beta.41/camoufox-158.0.1-beta.41-mac.arm64.zip",
            CAMOUFOX_SHA,
            1297694189,
            "zip",
            "Camoufox.app/Contents/MacOS/camoufox",
        )
    },
)
PAIRED_STACK = {"camoufox": "0.5.9", "playwright": "1.62.0"}
EXTERNAL_STORAGE_ROOT = Path("/Volumes/KINGSTON/RonC-Profile-Storage")
_STAGE_LOCK = threading.Lock()


class CoreError(ValueError):
    pass


def _default_storage_root() -> Path:
    if not os.environ.get("FOXPROFILE_CHROME_HOME"):
        volume = EXTERNAL_STORAGE_ROOT.parent
        with contextlib.suppress(OSError):
            if volume.is_dir() and os.access(volume, os.W_OK):
                return EXTERNAL_STORAGE_ROOT
    return fetch.engine_home().parent


def staging_home() -> Path:
    return Path(
        os.environ.get("FOXPROFILE_CORE_HOME", str(_default_storage_root() / "core-staging"))
    )


def backup_home() -> Path:
    return Path(
        os.environ.get("FOXPROFILE_CORE_BACKUP_HOME", str(_default_storage_root() / "core-backups"))
    )


def _disk_free_bytes(path: Path) -> int | None:
    import shutil

    probe = path
    try:
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        return shutil.disk_usage(probe).free if probe.exists() else None
    except OSError:
        return None


def approved(engine: str, version: str) -> Release:
    if engine == "camoufox" and version == CAMOUFOX_VERSION:
        return CAMOUFOX_RELEASE
    if engine == "chrome" and version in RELEASES:
        return RELEASES[version]
    raise CoreError(
        "Unapproved engine/version; arbitrary URLs and latest downloads are not allowed"
    )


def _host_asset(engine: str, release: Release) -> Asset:
    try:
        host = host_platform()
    except UnsupportedPlatformError as exc:
        raise CoreError(str(exc)) from exc
    if engine == "camoufox" and (
        host != "macos" or platform.machine().lower() not in {"arm64", "aarch64"}
    ):
        raise CoreError("This paired Camoufox staging asset is approved for macOS arm64 only")
    try:
        return release.asset_for(host)
    except UnsupportedPlatformError as exc:
        raise CoreError(str(exc)) from exc


def _stack() -> dict[str, str]:
    return {name: importlib.metadata.version(name) for name in PAIRED_STACK}


def _camoufox_inventory() -> dict:
    from camoufox.browser_pin import effective_pin, matches
    from camoufox.multiversion import BROWSERS_DIR, CONFIG_FILE, INSTALL_DIR, list_installed
    from camoufox.pkgman import build_file

    # load_config/list_installed/effective_pin are readers. get_active_path and
    # camoufox_path can WRITE/clean/download and must not be used in this view.
    config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    pin = effective_pin(config)
    installed = list_installed()
    active = None
    if pin:
        active = next(
            (
                i.path
                for i in installed
                if matches(pin, i.repo_name, i.version.version or "", i.version.build)
            ),
            None,
        )
    elif config.get("active_version"):
        candidate = (INSTALL_DIR / config["active_version"]).resolve()
        if (
            candidate.is_relative_to(BROWSERS_DIR.resolve())
            and (candidate / "version.json").is_file()
        ):
            active = candidate
    elif not config.get("channel") and not config.get("pinned") and installed:
        active = installed[0].path
    override = os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip()
    exe = (
        Path(override).absolute()
        if override
        else (
            active / "Camoufox.app/Contents/MacOS/camoufox"
            if active and platform.system() == "Darwin"
            else active / ("camoufox.exe" if os.name == "nt" else "camoufox")
            if active
            else None
        )
    )
    version = None
    if exe and exe.is_file():
        parser = configparser.ConfigParser()
        parser.read(build_file(exe, "application.ini"))
        version = parser.get("App", "Version", fallback=None)
    return {
        "engine": "camoufox",
        "selected_version": version,
        "installed": bool(exe and exe.is_file()),
        "override": bool(override),
        "source": "binary metadata (not attached live browser)",
        "executable": str(exe) if exe and exe.is_file() else None,
    }


def inventory() -> dict:
    rows = []
    try:
        rows.append(_camoufox_inventory())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        rows.append(
            {"engine": "camoufox", "selected_version": None, "installed": False, "error": str(exc)}
        )
    from chrome_engine.release import DEFAULT_VERSION

    rows.append(
        {
            "engine": "chrome",
            "selected_version": DEFAULT_VERSION,
            "installed": fetch.installed_executable() is not None,
            "override": False,
            "source": "app pin; stock Chrome is not this engine",
        }
    )
    stack = _stack()
    candidates = []
    for engine, releases in (("camoufox", [CAMOUFOX_RELEASE]), ("chrome", RELEASES.values())):
        for release in releases:
            reason = None
            try:
                _host_asset(engine, release)
                if stack != PAIRED_STACK:
                    raise CoreError(
                        "Runtime libraries differ from the reviewed stack; re-review required"
                    )
            except ValueError as exc:
                reason = str(exc)
            candidates.append(
                {
                    "engine": engine,
                    "version": release.version,
                    "staged": fetch.installed_release(release, staging_home() / engine) is not None,
                    "can_stage": reason is None,
                    "blocked_reason": reason,
                }
            )
    return {
        "cores": rows,
        "libraries": stack,
        "candidates": candidates,
        "storage_home": str(staging_home()),
        "backup_home": str(backup_home()),
        "free_bytes": _disk_free_bytes(staging_home()),
        "activation_allowed": os.name != "nt",
        "activation_blocked_reason": "Windows private backup ACL verification is not supported yet"
        if os.name == "nt"
        else None,
        "policy": "per-profile apply only after explicit backup and apply confirmations; no restart or downgrade",
    }


def stage(engine: str, version: str, *, confirm_download: bool = False) -> dict:
    if not confirm_download:
        raise CoreError("Explicit download confirmation is required")
    release = approved(engine, version)
    asset = _host_asset(engine, release)
    from camoufox.pkgman import INSTALL_DIR

    from ...core.config import DATA_DIR

    home = staging_home().resolve()
    destination = home / engine
    if destination.is_symlink() or (destination / release.version).is_symlink():
        raise CoreError("Core staging cannot follow engine/version symbolic links")
    for protected in (
        Path(DATA_DIR).resolve(),
        fetch.engine_home().resolve(),
        INSTALL_DIR.resolve(),
    ):
        if home.is_relative_to(protected) or protected.is_relative_to(home):
            raise CoreError(
                "Core staging must be separate from profile data and active core caches"
            )
        if destination.resolve().is_relative_to(protected):
            raise CoreError(
                "Core staging must be separate from profile data and active core caches"
            )
    if _stack() != PAIRED_STACK:
        raise CoreError("Runtime libraries differ from the reviewed stack; re-review required")
    if not _STAGE_LOCK.acquire(blocking=False):
        raise CoreError("Another core download is already running; refresh before retrying")
    try:

        def prepare_camoufox(root, executable):
            # Camoufox's own installer grants execute bits to bundled helpers;
            # ZipFile does not preserve them. Only touch this unactivated tree.
            for file in root.rglob("*"):
                if file.is_file():
                    file.chmod(file.stat().st_mode | 0o111)
            from .search import ensure_google_search

            ensure_google_search(str(executable))

        # Prune in another desktop/API process must not remove a live download.
        with profile_lease(home):
            exe = fetch.install_release(
                release,
                asset,
                destination,
                prepare=prepare_camoufox if engine == "camoufox" else None,
            )
        return {"engine": engine, "version": version, "staged": exe.is_file(), "activated": False}
    finally:
        _STAGE_LOCK.release()


def bound_executable(engine: str, version: str) -> Path:
    release = approved(engine, version)
    _host_asset(engine, release)
    if _stack() != PAIRED_STACK:
        raise CoreError("Bound core requires its reviewed runtime stack; refusing fallback")
    exe = fetch.installed_release(release, staging_home() / engine)
    if exe is None:
        raise CoreError("Approved core is not staged; download and verify it first")
    return exe


def backup_and_apply(
    pm, bl, name: str, engine: str, version: str, *, confirm_backup=False, confirm_apply=False
) -> dict:
    """Back up one stopped profile, then change only its future core binding.

    Never opens a browser, rewrites browser storage, restores data, or downgrades.
    Backup is local/private and contains credentials; no cloud/Git upload.
    """
    import hashlib
    import re
    import shutil
    import tempfile
    import zipfile
    from datetime import datetime, timezone

    from ..profile.transfer import export_to_zip
    from .lease import native_profile_guard, profile_lease, store_lease

    if not (confirm_backup and confirm_apply):
        raise CoreError("Separate backup and apply confirmations are required")
    if os.name == "nt":
        raise CoreError(
            "Core apply on Windows is blocked until private backup ACL verification is supported; staging is available"
        )
    bound_executable(engine, version)
    from ...core.config import PROFILES_FILE

    with bl.exclusive(name), pm._lock, store_lease(PROFILES_FILE):
        pm._check_store_current()
        bound_executable(engine, version)
        profile = pm.profiles.get(name)
        if profile is None:
            raise CoreError("Profile not found")
        if profile.engine != engine:
            raise CoreError("Cross-engine migration is not supported")
        if profile.core_version == version:
            return {"applied": False, "reason": "already bound to this core"}
        current = profile.core_version
        previous_exe = bound_executable(engine, current) if current else None
        if current is None:
            selected = next(c for c in inventory()["cores"] if c["engine"] == engine)
            current = selected["selected_version"]
            if engine == "chrome":
                previous_exe = fetch.installed_executable()
            elif selected.get("executable"):
                previous_exe = Path(selected["executable"])

        def numbers(text):
            match = re.fullmatch(r"(\d+(?:\.\d+)+)(?:-(alpha|beta|rc)[.-]?(\d+))?", text or "")
            if not match:
                raise CoreError("Cannot establish current/target core version safely")
            base = tuple(int(v) for v in match[1].split("."))
            if len(base) > 4:
                raise CoreError("Unknown core version format")
            return (
                base
                + (0,) * (4 - len(base))
                + ({"alpha": 0, "beta": 1, "rc": 2, None: 3}[match[2]], int(match[3] or 0))
            )

        if numbers(version) < numbers(current):
            raise CoreError(
                "Core downgrade is blocked; use a separately reviewed snapshot recovery"
            )
        source = Path(pm._data_path(name))
        if source.is_symlink():
            raise CoreError("Cannot back up a symlinked profile directory")
        root = backup_home().resolve()
        data_root = source.parent.resolve()
        for protected in (data_root, staging_home().resolve(), fetch.engine_home().resolve()):
            if root.is_relative_to(protected) or protected.is_relative_to(root):
                raise CoreError("Backup must be separate from profile data and core caches")
        locks = ("lock", "SingletonLock", "SingletonCookie", "SingletonSocket")
        persistent_locks = frozenset({".parentlock", "parent.lock"})

        def snapshot():
            for lock in locks:
                p = source / lock
                if p.exists() or p.is_symlink():
                    raise CoreError(
                        "Browser lock artifact present; close and verify the profile first (no lock is removed automatically)"
                    )
            files = {}
            if source.exists():
                for base, dirs, names in os.walk(source, followlinks=False):
                    for item in dirs + names:
                        if (Path(base) / item).is_symlink():
                            raise CoreError("Profile contains symbolic links; backup refused")
                    for item in names:
                        if Path(base) == source and item in persistent_locks:
                            continue
                        p = Path(base) / item
                        st = p.stat()
                        if not p.is_file():
                            raise CoreError("Profile contains non-regular files")
                        files[str(p.relative_to(source))] = (st.st_size, st.st_mtime_ns, st.st_ino)
            return files

        with profile_lease(source), native_profile_guard(source, engine):
            before = snapshot()
            if before and not (previous_exe and previous_exe.is_file()):
                raise CoreError(
                    "Previous core binary is missing; safe recovery cannot be established"
                )
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.stat().st_mode & 0o077:
                raise CoreError("Backup root must be private (0700)")
            if shutil.disk_usage(root).free < sum(v[0] for v in before.values()) + 10 * 1024 * 1024:
                raise CoreError("Insufficient free disk space for a complete backup")
            directory = Path(tempfile.mkdtemp(prefix="core-", dir=root))
            ok, archive = export_to_zip(
                profile,
                str(source),
                str(directory),
                include_data=True,
                exclude_files=persistent_locks,
            )
            if not ok:
                raise CoreError("Backup failed; core binding was not changed")
            archive = Path(archive)
            archive.chmod(0o600)
            with zipfile.ZipFile(archive) as z:
                if z.testzip() is not None:
                    raise CoreError("Backup verification failed; binding unchanged")
                if {n.removeprefix("data/") for n in z.namelist() if n.startswith("data/")} != set(
                    before
                ):
                    raise CoreError("Backup file list mismatch; binding unchanged")

                def digest(stream):
                    value = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        value.update(chunk)
                    return value.hexdigest()

                for relative in before:
                    with (
                        (source / relative).open("rb") as original,
                        z.open("data/" + relative.replace(os.sep, "/")) as saved,
                    ):
                        if digest(original) != digest(saved):
                            raise CoreError("Backup content mismatch; binding unchanged")
            if snapshot() != before:
                raise CoreError("Profile data changed during backup; binding unchanged")
            manifest = directory / "recovery.json"
            manifest.write_text(
                json.dumps(
                    {
                        "engine": engine,
                        "previous_core": current,
                        "target_core": version,
                        "previous_binding": profile.core_version,
                        "excluded_native_lockfiles": list(persistent_locks),
                        "previous_executable": str(previous_exe) if previous_exe else None,
                        "libraries": _stack(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "warning": "Restore requires matching old data snapshot AND old runtime; never open migrated data with old core.",
                    },
                    indent=2,
                )
            )
            manifest.chmod(0o600)
            previous = profile.core_version
            profile.core_version = version
            try:
                pm.save_profiles(strict=True)
            except Exception:
                profile.core_version = previous
                raise
            return {
                "applied": True,
                "version": version,
                "backup_path": str(archive),
                "browser_started": False,
                "profile_data_changed": False,
            }


def launch_binding(name: str, engine: str, requested: str = "") -> str:
    """Resolve persisted binding under store lease; refuse corrupt/stale launch data."""
    from ...core.config import PROFILES_FILE
    from .lease import store_lease

    path = Path(PROFILES_FILE)
    with store_lease(path):
        if not path.exists():
            return requested  # Standalone runner with no managed profile store.
        data = json.loads(path.read_text(encoding="utf-8"))
        item = data.get(name)
        if not isinstance(item, dict) or item.get("engine", "camoufox") != engine:
            raise CoreError("Profile removed or engine changed; refresh before launching")
        version = item.get("core_version")
        if version is not None and not isinstance(version, str):
            raise CoreError("Invalid stored core binding; refusing legacy fallback")
        return version or ""


def _tree_size(path: Path) -> int:
    total = 0
    for base, _, names in os.walk(path, followlinks=False):
        for name in names:
            item = Path(base) / name
            if not item.is_symlink():
                with contextlib.suppress(OSError):
                    total += item.stat().st_size
    return total


def prune_unused(pm, bl, *, confirm_prune: bool = False) -> dict:
    """Remove unbound staged cores and superseded/orphaned backup snapshots."""
    import shutil
    import zipfile

    from ...core.config import DATA_DIR, PROFILES_FILE
    from .lease import store_lease

    if not confirm_prune:
        raise CoreError("Explicit prune confirmation is required")
    if os.name == "nt":
        raise CoreError(
            "Core/backup pruning on Windows is blocked until ACL verification is supported"
        )
    if not _STAGE_LOCK.acquire(blocking=False):
        raise CoreError("Another core operation is already running; refresh before retrying")
    try:
        from camoufox.pkgman import INSTALL_DIR

        stage_root = staging_home()
        bkp_root = backup_home()
        if stage_root.is_symlink() or bkp_root.is_symlink():
            raise CoreError("Cannot prune symlinked storage roots")
        stage_resolved = stage_root.resolve()
        bkp_resolved = bkp_root.resolve()
        if stage_resolved.is_relative_to(bkp_resolved) or bkp_resolved.is_relative_to(
            stage_resolved
        ):
            raise CoreError("Staging and backup directories must be separate")
        for protected in (
            Path(DATA_DIR).resolve(),
            fetch.engine_home().resolve(),
            INSTALL_DIR.resolve(),
        ):
            for candidate in (stage_resolved, bkp_resolved):
                if candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
                    raise CoreError(
                        "Storage roots must be separate from profile data and active caches"
                    )
        with profile_lease(stage_resolved), pm._lock, store_lease(PROFILES_FILE):
            pm._check_store_current()
            if bl and (bl.running_profile_names() or getattr(bl, "_busy", None)):
                raise CoreError("Stop all running profiles before pruning cores or backups")
            protected_cores = {
                (p.engine, p.core_version) for p in pm.profiles.values() if p.core_version
            }
            to_delete_backups: list[Path] = []
            if bkp_resolved.is_dir():
                grouped: dict[str, list[tuple[str, int, str, Path, tuple[str, str] | None]]] = {}
                for entry in sorted(bkp_resolved.iterdir()):
                    if not entry.name.startswith("core-"):
                        continue
                    if entry.is_symlink() or not entry.is_dir():
                        raise CoreError("Backup directory contains an invalid core snapshot entry")
                    zips = [z for z in entry.glob("*.zip") if z.is_file() and not z.is_symlink()]
                    manifest = entry / "recovery.json"
                    prof_name = None
                    created_at = ""
                    prev_core_pair: tuple[str, str] | None = None
                    if len(zips) == 1 and manifest.is_file() and not manifest.is_symlink():
                        try:
                            meta = json.loads(manifest.read_text(encoding="utf-8"))
                            if isinstance(meta, dict):
                                created_at = str(meta.get("created_at") or "")
                                eng = meta.get("engine")
                                prev_ver = meta.get("previous_core")
                                if isinstance(eng, str) and isinstance(prev_ver, str) and prev_ver:
                                    prev_core_pair = (eng, prev_ver)
                            with zipfile.ZipFile(zips[0]) as zf:
                                if zf.testzip() is not None:
                                    raise CoreError("Backup payload verification failed")
                                pdata = json.loads(zf.read("profile.json").decode("utf-8"))
                                if isinstance(pdata, dict):
                                    prof_name = pdata.get("name")
                        except (
                            OSError,
                            ValueError,
                            KeyError,
                            TypeError,
                            AttributeError,
                            zipfile.BadZipFile,
                        ):
                            prof_name = None
                    if not isinstance(prof_name, str) or not prof_name or prev_core_pair is None:
                        raise CoreError(
                            "Backup directory contains an unreadable snapshot; inspect before pruning"
                        )
                    grouped.setdefault(prof_name, []).append(
                        (
                            created_at,
                            entry.stat().st_mtime_ns,
                            entry.name,
                            entry,
                            prev_core_pair,
                        )
                    )
                for items in grouped.values():
                    items.sort(key=lambda t: (t[0], t[1], t[2]))
                    if items[-1][4] is not None:
                        protected_cores.add(items[-1][4])
                    to_delete_backups.extend(item[3] for item in items[:-1])
            to_delete_parts: list[Path] = []
            to_delete_cores: list[tuple[str, Path]] = []
            if stage_resolved.is_dir():
                for engine in ("camoufox", "chrome"):
                    edir = stage_resolved / engine
                    if edir.is_symlink():
                        raise CoreError("Staging directory contains symbolic links")
                    if not edir.is_dir():
                        continue
                    for child in sorted(edir.iterdir()):
                        if child.is_symlink():
                            raise CoreError("Staging directory contains symbolic links")
                        if child.is_file() and child.name.endswith(".part"):
                            to_delete_parts.append(child)
                        elif child.is_dir() and (engine, child.name) not in protected_cores:
                            to_delete_cores.append((engine, child))
            removed_backups = 0
            removed_cores: list[str] = []
            freed_bytes = 0
            for folder in to_delete_backups:
                freed_bytes += _tree_size(folder)
                shutil.rmtree(folder)
                removed_backups += 1
            for part in to_delete_parts:
                freed_bytes += part.stat().st_size
                part.unlink(missing_ok=True)
            for engine, child in to_delete_cores:
                freed_bytes += _tree_size(child)
                shutil.rmtree(child)
                for lock_name in (f"{child.name}.lock", f".{child.name}.lock"):
                    lock_file = child.parent / lock_name
                    if lock_file.is_file() and not lock_file.is_symlink():
                        lock_file.unlink(missing_ok=True)
                removed_cores.append(f"{engine}:{child.name}")
            return {
                "pruned": True,
                "removed_cores": removed_cores,
                "removed_backups": removed_backups,
                "freed_bytes": freed_bytes,
            }
    finally:
        _STAGE_LOCK.release()
