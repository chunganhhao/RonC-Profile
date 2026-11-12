from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...core import config as app_config
from ...core.logging import get_logger

logger = get_logger("browser.extensions")

PIN_MODES = ("custom", "force_pinned", "force_unpinned")
_MSG_RE = re.compile(r"^__MSG_([A-Za-z0-9_@]+)__$")


def compute_chrome_extension_id(ext_dir: str | Path, manifest: dict[str, Any] | None = None) -> str:
    """Compute the 32-char (a-p) Chromium extension ID for an unpacked extension directory."""
    if isinstance(manifest, dict):
        raw_key = str(manifest.get("key") or "").strip()
        if raw_key:
            with contextlib.suppress(Exception):
                der = base64.b64decode(raw_key, validate=False)
                if der:
                    digest = hashlib.sha256(der).hexdigest()[:32]
                    return "".join(chr(ord("a") + int(c, 16)) for c in digest)
    resolved = str(Path(ext_dir).resolve())
    path_bytes = (
        resolved.encode("utf-16le") if sys.platform.startswith("win") else resolved.encode("utf-8")
    )
    digest = hashlib.sha256(path_bytes).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _resolve_msg_placeholder(value: str, ext_dir: Path, default_locale: str = "en") -> str:
    text = (value or "").strip()
    m = _MSG_RE.match(text)
    if not m:
        return text
    msg_key = m.group(1)
    locales_dir = ext_dir / "_locales"
    if not locales_dir.is_dir():
        return text
    candidates = [default_locale, "en", "en_US", "vi"]
    for child in sorted(locales_dir.iterdir()):
        if child.is_dir() and child.name not in candidates:
            candidates.append(child.name)
    for loc in candidates:
        if not loc:
            continue
        msg_file = locales_dir / loc / "messages.json"
        if not msg_file.is_file():
            continue
        with contextlib.suppress(Exception):
            data = json.loads(msg_file.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k.lower() == msg_key.lower() and isinstance(v, dict):
                        resolved = str(v.get("message") or "").strip()
                        if resolved:
                            return resolved
    return text


def _pick_manifest_icon(manifest: dict[str, Any], ext_dir: Path) -> str:
    icon_candidates: list[tuple[int, str]] = []
    icons = manifest.get("icons")
    if isinstance(icons, dict):
        for k, v in icons.items():
            if isinstance(v, str) and v.strip():
                size = int(k) if str(k).isdigit() else 48
                icon_candidates.append((size, v.strip()))
    for act_key in ("action", "browser_action", "page_action"):
        act = manifest.get(act_key)
        if isinstance(act, dict):
            def_icon = act.get("default_icon")
            if isinstance(def_icon, str) and def_icon.strip():
                icon_candidates.append((32, def_icon.strip()))
            elif isinstance(def_icon, dict):
                for k, v in def_icon.items():
                    if isinstance(v, str) and v.strip():
                        size = int(k) if str(k).isdigit() else 32
                        icon_candidates.append((size, v.strip()))
    icon_candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, rel in icon_candidates:
        clean_rel = rel.lstrip("/\\")
        target = (ext_dir / clean_rel).resolve()
        try:
            target.relative_to(ext_dir.resolve())
        except ValueError:
            continue
        if target.is_file():
            return clean_rel
    return ""


@dataclass
class ExtensionEntry:
    id: str
    name: str
    version: str
    description: str
    path: str
    chrome_id: str
    enabled: bool = True
    pin_mode: str = "force_pinned"  # "custom" | "force_pinned" | "force_unpinned"
    apply_to_all: bool = False
    icon_rel_path: str = ""
    source_zip: str = ""

    @property
    def chrome_ext_id(self) -> str:
        return self.chrome_id

    @property
    def apply_to_all_chrome(self) -> bool:
        return self.apply_to_all

    @apply_to_all_chrome.setter
    def apply_to_all_chrome(self, val: bool) -> None:
        self.apply_to_all = bool(val)

    @property
    def icon_abs_path(self) -> str | None:
        if not self.icon_rel_path or not self.path:
            return None
        base = Path(self.path).resolve()
        p = (base / self.icon_rel_path).resolve()
        if not p.is_relative_to(base):
            return None
        return str(p) if p.is_file() else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["chrome_ext_id"] = self.chrome_id
        d["apply_to_all_chrome"] = self.apply_to_all
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtensionEntry | None:
        eid = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        path = str(data.get("path") or "").strip()
        if not eid or not name or not path:
            return None
        pin_mode = str(data.get("pin_mode") or "force_pinned").strip()
        if pin_mode not in PIN_MODES:
            pin_mode = "force_pinned"
        chrome_id = str(data.get("chrome_id") or data.get("chrome_ext_id") or "").strip()
        if not chrome_id and os.path.isdir(path):
            chrome_id = compute_chrome_extension_id(path)
        apply_all = data.get("apply_to_all")
        if apply_all is None:
            apply_all = data.get("apply_to_all_chrome", False)
        return cls(
            id=eid,
            name=name,
            version=str(data.get("version") or "1.0.0").strip() or "1.0.0",
            description=str(data.get("description") or "").strip(),
            path=path,
            chrome_id=chrome_id,
            enabled=bool(data.get("enabled", True)),
            pin_mode=pin_mode,
            apply_to_all=bool(apply_all),
            icon_rel_path=str(data.get("icon_rel_path") or "").strip(),
            source_zip=str(data.get("source_zip") or "").strip(),
        )


class ExtensionService:
    """Manage unpacked Chromium extensions imported from .zip archives."""

    def __init__(
        self,
        file_path: str | None = None,
        extensions_dir: str | None = None,
        *,
        extensions_file: str | None = None,
    ) -> None:
        self._explicit_file = file_path or extensions_file
        self._explicit_dir = extensions_dir
        self._lock = threading.RLock()
        self._entries: dict[str, ExtensionEntry] = {}
        self.reload()

    @property
    def file_path(self) -> str:
        return self._explicit_file or app_config.EXTENSIONS_FILE

    @property
    def extensions_dir(self) -> str:
        return self._explicit_dir or app_config.EXTENSIONS_DIR

    def reload(self) -> None:
        with self._lock:
            self._entries.clear()
            path = self.file_path
            if not os.path.exists(path):
                return
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                items = raw if isinstance(raw, list) else raw.get("extensions", [])
                for item in items:
                    if isinstance(item, dict):
                        entry = ExtensionEntry.from_dict(item)
                        if entry is not None:
                            self._entries[entry.id] = entry
            except Exception as e:
                logger.warning("Could not load extensions pool %s: %s", path, e)

    def _save(self) -> None:
        path = self.file_path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        payload = json.dumps(
            {"extensions": [e.to_dict() for e in self._entries.values()]},
            indent=2,
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".ext_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def list_entries(self) -> list[ExtensionEntry]:
        with self._lock:
            self.reload()
            return list(self._entries.values())

    def get(self, ext_id: str) -> ExtensionEntry | None:
        with self._lock:
            self.reload()
            return self._entries.get(ext_id)

    def get_entry(self, ext_id: str) -> ExtensionEntry | None:
        return self.get(ext_id)

    def extension_dir(self, ext_id: str) -> str:
        entry = self.get(ext_id)
        if entry and entry.path:
            return entry.path
        return os.path.join(self.extensions_dir, ext_id)

    def install_from_zip(
        self,
        zip_source: str | Path | bytes,
        filename: str = "",
    ) -> ExtensionEntry:
        """Validate and extract a Chromium extension .zip into extensions_dir."""
        try:
            if isinstance(zip_source, (str, Path)):
                src_path = Path(zip_source)
                if not src_path.is_file():
                    raise ValueError(f"Zip file not found: {src_path}")
                if not filename:
                    filename = src_path.name
                zf_ctx = zipfile.ZipFile(src_path, "r")
            else:
                zf_ctx = zipfile.ZipFile(io.BytesIO(zip_source), "r")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Invalid .zip archive: {exc}") from exc

        with zf_ctx as zf:
            members = [
                info
                for info in zf.infolist()
                if not info.filename.startswith("__MACOSX/")
                and not info.filename.endswith(".DS_Store")
            ]
            # shortcut: fixed caps; raise if real extensions exceed them.
            if len(members) > 5000:
                raise ValueError("Too many files inside .zip archive (max 5000)")
            if sum(info.file_size for info in members) > 300 * 1024 * 1024:
                raise ValueError("Uncompressed extension exceeds 300 MB")
            # Locate manifest.json closest to archive root
            manifest_members: list[tuple[int, str]] = []
            for info in members:
                if info.is_dir():
                    continue
                norm = info.filename.replace("\\", "/").strip("/")
                parts = norm.split("/")
                if any(p in ("..", "") for p in parts):
                    raise ValueError("Unsafe path inside .zip archive")
                if parts[-1] == "manifest.json":
                    manifest_members.append((len(parts), norm))
            if not manifest_members:
                raise ValueError("No manifest.json found in .zip archive")
            manifest_members.sort(key=lambda item: item[0])
            best_manifest_rel = manifest_members[0][1]
            prefix = (
                best_manifest_rel[: -len("manifest.json")]
                if best_manifest_rel.endswith("manifest.json")
                else ""
            )

            try:
                raw_manifest = zf.read(best_manifest_rel).decode("utf-8-sig")
                manifest = json.loads(raw_manifest)
                if not isinstance(manifest, dict):
                    raise ValueError("manifest.json must be a JSON object")
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Invalid manifest.json: {exc}") from exc

            # Compute deterministic ID from manifest name + version + source filename
            raw_name = str(manifest.get("name") or "").strip() or Path(filename or "ext").stem
            raw_ver = str(manifest.get("version") or "1.0.0").strip() or "1.0.0"
            slug = re.sub(r"[^a-z0-9]+", "_", raw_name.lower()).strip("_")[:24] or "ext"
            digest = hashlib.sha1(f"{raw_name}:{raw_ver}:{filename}".encode()).hexdigest()[:8]
            ext_id = f"{slug}_{digest}"

            base_dir = Path(self.extensions_dir).resolve()
            base_dir.mkdir(parents=True, exist_ok=True)
            target_dir = (base_dir / ext_id).resolve()
            stage_dir = Path(tempfile.mkdtemp(dir=base_dir, prefix=f".stage_{ext_id}_"))

            try:
                for info in members:
                    if info.is_dir():
                        continue
                    norm = info.filename.replace("\\", "/").strip("/")
                    if prefix and not norm.startswith(prefix):
                        continue
                    rel_str = norm[len(prefix) :].strip("/")
                    if not rel_str:
                        continue
                    rel_parts = rel_str.split("/")
                    if any(p in ("..", ".") for p in rel_parts):
                        raise ValueError("Unsafe zip path traversal")
                    dest_file = (stage_dir / Path(*rel_parts)).resolve()
                    dest_file.relative_to(stage_dir)
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src_f, open(dest_file, "wb") as dst_f:
                        shutil.copyfileobj(src_f, dst_f)
            except Exception as exc:
                shutil.rmtree(stage_dir, ignore_errors=True)
                raise ValueError(f"Failed to extract extension: {exc}") from exc

            # shortcut: atomic swap via staging directory so failed reimport preserves working extension
            backup_dir = None
            try:
                if target_dir.exists():
                    backup_dir = Path(tempfile.mkdtemp(dir=base_dir, prefix=f".bk_{ext_id}_"))
                    backup_dir.rmdir()
                    target_dir.rename(backup_dir)
                stage_dir.rename(target_dir)
                if backup_dir and backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception as exc:
                if backup_dir and backup_dir.exists() and not target_dir.exists():
                    backup_dir.rename(target_dir)
                shutil.rmtree(stage_dir, ignore_errors=True)
                raise ValueError(f"Failed to activate extension: {exc}") from exc

        default_locale = str(manifest.get("default_locale") or "en").strip()
        resolved_name = _resolve_msg_placeholder(raw_name, target_dir, default_locale)
        if not resolved_name or resolved_name.startswith("__MSG_"):
            resolved_name = Path(filename or ext_id).stem
        raw_desc = str(manifest.get("description") or "").strip()
        resolved_desc = _resolve_msg_placeholder(raw_desc, target_dir, default_locale)
        if resolved_desc.startswith("__MSG_"):
            resolved_desc = ""

        icon_rel = _pick_manifest_icon(manifest, target_dir)
        chrome_id = compute_chrome_extension_id(target_dir, manifest)

        with self._lock:
            self.reload()
            existing = self._entries.get(ext_id)
            entry = ExtensionEntry(
                id=ext_id,
                name=resolved_name,
                version=raw_ver,
                description=resolved_desc,
                path=str(target_dir),
                chrome_id=chrome_id,
                enabled=existing.enabled if existing else True,
                pin_mode=existing.pin_mode if existing else "force_pinned",
                apply_to_all=existing.apply_to_all if existing else False,
                icon_rel_path=icon_rel,
                source_zip=filename or f"{ext_id}.zip",
            )
            self._entries[ext_id] = entry
            self._save()
            return entry

    def update_extension(
        self,
        ext_id: str,
        *,
        enabled: bool | None = None,
        pin_mode: str | None = None,
        apply_to_all: bool | None = None,
        apply_to_all_chrome: bool | None = None,
    ) -> ExtensionEntry | None:
        with self._lock:
            self.reload()
            entry = self._entries.get(ext_id)
            if entry is None:
                return None
            if enabled is not None:
                entry.enabled = bool(enabled)
            if pin_mode is not None and pin_mode in PIN_MODES:
                entry.pin_mode = pin_mode
            effective_all = apply_to_all if apply_to_all is not None else apply_to_all_chrome
            if effective_all is not None:
                entry.apply_to_all = bool(effective_all)
            self._save()
            return entry

    def remove_extension(
        self,
        ext_id: str,
        profile_manager: Any = None,
        *,
        pm: Any = None,
    ) -> bool:
        target_pm = profile_manager if profile_manager is not None else pm
        with self._lock:
            self.reload()
            entry = self._entries.pop(ext_id, None)
            if entry is None:
                return False
            self._save()
            if entry.path and os.path.isdir(entry.path):
                with contextlib.suppress(Exception):
                    base_dir = Path(self.extensions_dir).resolve()
                    target = Path(entry.path).resolve()
                    target.relative_to(base_dir)
                    shutil.rmtree(target, ignore_errors=True)
        if target_pm is not None:
            with contextlib.suppress(Exception):
                changed = False
                for prof in target_pm.list_profiles():
                    if ext_id in getattr(prof, "extension_ids", []):
                        prof.extension_ids = [x for x in prof.extension_ids if x != ext_id]
                        changed = True
                if changed:
                    target_pm.save_profiles()
        return True

    def assign_profiles(
        self,
        ext_id: str,
        profile_names: list[str],
        profile_manager: Any,
        *,
        pin_mode: str | None = None,
        apply_to_all: bool | None = None,
    ) -> int:
        with self._lock:
            self.reload()
            entry = self._entries.get(ext_id)
            if entry is None:
                return 0
            if pin_mode is not None and pin_mode in PIN_MODES:
                entry.pin_mode = pin_mode
            if apply_to_all is not None:
                entry.apply_to_all = bool(apply_to_all)
            self._save()
        if profile_manager is None:
            return 0
        target_set = set(profile_names)
        updated_count = 0
        for prof in profile_manager.list_profiles():
            cur_ids = list(getattr(prof, "extension_ids", []) or [])
            has_ext = ext_id in cur_ids
            should_have = prof.name in target_set
            if should_have and not has_ext:
                cur_ids.append(ext_id)
                prof.extension_ids = cur_ids
                updated_count += 1
            elif not should_have and has_ext:
                prof.extension_ids = [x for x in cur_ids if x != ext_id]
                updated_count += 1
        if updated_count:
            profile_manager.save_profiles()
        return updated_count

    def usage_counts(self, profile_manager: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        entries = self.list_entries()
        profiles = profile_manager.list_profiles() if profile_manager is not None else []
        chrome_profiles = [p for p in profiles if getattr(p, "engine", "camoufox") == "chrome"]
        for entry in entries:
            if entry.apply_to_all:
                counts[entry.id] = len(chrome_profiles) or len(profiles)
            else:
                counts[entry.id] = sum(
                    1 for p in profiles if entry.id in (getattr(p, "extension_ids", None) or [])
                )
        return counts

    def resolve_for_profile(
        self,
        profile_extension_ids: list[str] | None = None,
    ) -> tuple[list[str], list[str], list[str]]:
        """Return (unpacked_paths, force_pinned_chrome_ids, force_unpinned_chrome_ids)."""
        assigned = set(profile_extension_ids or [])
        paths: list[str] = []
        pinned: list[str] = []
        unpinned: list[str] = []
        for entry in self.list_entries():
            if not entry.enabled:
                continue
            if not (entry.apply_to_all or entry.id in assigned):
                continue
            ext_dir = Path(entry.path)
            if not (ext_dir / "manifest.json").is_file():
                continue
            paths.append(str(ext_dir.resolve()))
            cid = entry.chrome_id or compute_chrome_extension_id(ext_dir)
            if entry.pin_mode == "force_pinned":
                pinned.append(cid)
            elif entry.pin_mode == "force_unpinned":
                unpinned.append(cid)
        return paths, pinned, unpinned
