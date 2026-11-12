from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ...core import config as app_config
from ...core.logging import get_logger
from ...utils.proxy_parser import mask_proxy, normalize_proxy
from ...utils.validation import validate_proxy_format

if TYPE_CHECKING:
    from ...interfaces.protocols import IProfileManager, IProxyService

logger = get_logger("proxy.pool")


@dataclass
class ProxyPoolEntry:
    id: str
    proxy: str
    masked: str
    label: str = ""
    status: str = "untested"  # "untested" | "working" | "dead"
    exit_ip: str = ""
    latency_ms: int = 0
    last_checked: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ProxyPoolEntry | None:
        raw_proxy = str(data.get("proxy") or "").strip()
        if not raw_proxy:
            return None
        valid, _ = validate_proxy_format(raw_proxy)
        if not valid:
            return None
        norm = normalize_proxy(raw_proxy)
        masked = mask_proxy(norm) or norm
        pid = str(data.get("id") or _make_id(norm))
        status = str(data.get("status") or "untested")
        if status not in ("untested", "working", "dead"):
            status = "untested"
        return cls(
            id=pid,
            proxy=norm,
            masked=masked,
            label=str(data.get("label") or "").strip(),
            status=status,
            exit_ip=str(data.get("exit_ip") or "").strip(),
            latency_ms=int(data.get("latency_ms") or 0),
            last_checked=float(data.get("last_checked") or 0.0),
        )


def _make_id(normalized_proxy: str) -> str:
    return hashlib.sha1(normalized_proxy.encode("utf-8")).hexdigest()[:10]


class ProxyPoolService:
    """Centralized proxy pool storage, health checker, and auto-assigner."""

    def __init__(self, file_path: str | None = None) -> None:
        self._explicit_path = file_path
        self._lock = threading.RLock()
        self._entries: dict[str, ProxyPoolEntry] = {}
        self.reload()

    @property
    def file_path(self) -> str:
        return self._explicit_path or app_config.PROXY_POOL_FILE

    def reload(self) -> None:
        with self._lock:
            self._entries.clear()
            path = self.file_path
            if not os.path.exists(path):
                return
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                items = raw if isinstance(raw, list) else raw.get("proxies", [])
                for item in items:
                    if isinstance(item, dict):
                        entry = ProxyPoolEntry.from_dict(item)
                        if entry:
                            self._entries[entry.id] = entry
            except Exception as e:
                logger.error("Failed to load proxy pool from %s: %s", path, e)

    def _save(self) -> None:
        with self._lock:
            path = self.file_path
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            payload = [e.to_dict() for e in self._entries.values()]
            fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".proxies_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    def list_entries(self) -> list[ProxyPoolEntry]:
        with self._lock:
            return list(self._entries.values())

    def get_entry(self, proxy_id: str) -> ProxyPoolEntry | None:
        with self._lock:
            return self._entries.get(proxy_id)

    def _upsert_locked(self, raw_str: str, label: str = "") -> ProxyPoolEntry | None:
        cleaned = (raw_str or "").strip()
        if not cleaned or not validate_proxy_format(cleaned)[0]:
            return None
        norm = normalize_proxy(cleaned)
        pid = _make_id(norm)
        clean_label = label.strip()
        existing = self._entries.get(pid)
        if existing:
            if clean_label:
                existing.label = clean_label
            return existing
        entry = ProxyPoolEntry(
            id=pid,
            proxy=norm,
            masked=mask_proxy(norm) or norm,
            label=clean_label,
        )
        self._entries[pid] = entry
        return entry

    def add_proxy(self, raw_str: str, label: str = "") -> ProxyPoolEntry | None:
        with self._lock:
            entry = self._upsert_locked(raw_str, label)
            if entry is not None:
                self._save()
            return entry

    def bulk_import(self, text: str, default_label: str = "") -> tuple[int, int]:
        """Parse multi-line proxy list. Supports `proxy|label` or `proxy #label`.

        Returns (added_or_updated_count, invalid_count).
        """
        added = 0
        invalid = 0
        with self._lock:
            for raw_line in (text or "").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                label = default_label.strip()
                for sep in ("|", " #"):
                    if sep in line:
                        proxy_part, label_part = line.split(sep, 1)
                        line = proxy_part.strip()
                        if label_part.strip():
                            label = label_part.strip()
                        break
                if self._upsert_locked(line, label) is not None:
                    added += 1
                else:
                    invalid += 1
            if added > 0:
                self._save()
        return added, invalid

    def remove_proxy(self, proxy_id: str) -> bool:
        with self._lock:
            if proxy_id not in self._entries:
                return False
            del self._entries[proxy_id]
            self._save()
            return True

    def clear_dead(self) -> int:
        with self._lock:
            dead_ids = [pid for pid, e in self._entries.items() if e.status == "dead"]
            for pid in dead_ids:
                del self._entries[pid]
            if dead_ids:
                self._save()
            return len(dead_ids)

    def check_entry(
        self,
        proxy_id: str,
        proxy_service: IProxyService,
        timeout: int = 10,
    ) -> ProxyPoolEntry | None:
        with self._lock:
            entry = self._entries.get(proxy_id)
            if not entry:
                return None
            target_proxy = entry.proxy

        t0 = time.monotonic()
        ok, msg = proxy_service.check_proxy_sync(target_proxy, timeout=timeout)
        elapsed_ms = max(1, int((time.monotonic() - t0) * 1000))

        with self._lock:
            entry = self._entries.get(proxy_id)
            if not entry:
                return None
            entry.last_checked = time.time()
            entry.latency_ms = elapsed_ms if ok else 0
            if ok:
                entry.status = "working"
                # msg is typically the exit IP or contains IP
                entry.exit_ip = msg.strip()
            else:
                entry.status = "dead"
                entry.exit_ip = ""
            self._save()
            return entry

    def check_all(
        self,
        proxy_service: IProxyService,
        timeout: int = 10,
    ) -> list[ProxyPoolEntry]:
        ids = [e.id for e in self.list_entries()]
        results: list[ProxyPoolEntry] = []
        for pid in ids:
            res = self.check_entry(pid, proxy_service, timeout=timeout)
            if res:
                results.append(res)
        return results

    def usage_counts(self, pm: IProfileManager) -> dict[str, int]:
        """Count how many profiles currently use each pool entry."""
        counts: dict[str, int] = {}
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
            counts[e.id] = 0
        for prof in pm.list_profiles():
            if not prof.proxy:
                continue
            norm = normalize_proxy(prof.proxy)
            pid = _make_id(norm)
            if pid in counts:
                counts[pid] += 1
        return counts

    def pick_least_used(
        self,
        pm: IProfileManager,
        only_working: bool = False,
    ) -> ProxyPoolEntry | None:
        with self._lock:
            candidates = [
                e
                for e in self._entries.values()
                if (e.status == "working" if only_working else e.status != "dead")
            ]
            if not candidates and not only_working:
                candidates = list(self._entries.values())
        if not candidates:
            return None
        counts = self.usage_counts(pm)
        candidates.sort(key=lambda e: (counts.get(e.id, 0), 0 if e.status == "working" else 1))
        return candidates[0]

    def assign_round_robin(
        self,
        profile_names: list[str],
        pm: IProfileManager,
        only_working: bool = False,
    ) -> int:
        """Assign pool proxies across `profile_names` in round-robin order."""
        with self._lock:
            pool = [
                e
                for e in self._entries.values()
                if (e.status == "working" if only_working else e.status != "dead")
            ]
            if not pool and not only_working:
                pool = list(self._entries.values())
        if not pool or not profile_names:
            return 0

        counts = self.usage_counts(pm)
        pool.sort(key=lambda e: (counts.get(e.id, 0), 0 if e.status == "working" else 1))

        updated = 0
        save_fn = getattr(pm, "save_profiles", None)
        if callable(save_fn):
            pm_lock = getattr(pm, "_lock", None) or contextlib.nullcontext()
            with pm_lock:
                for idx, name in enumerate(profile_names):
                    prof = pm.profiles.get(name)
                    if not prof:
                        continue
                    prof.proxy = pool[idx % len(pool)].proxy or None
                    updated += 1
                if updated > 0:
                    save_fn()
            return updated

        for idx, name in enumerate(profile_names):
            prof = pm.profiles.get(name)
            if not prof:
                continue
            chosen = pool[idx % len(pool)]
            ok = pm.update_profile(
                prof.name,
                prof.name,
                chosen.proxy,
                prof.os_type,
                prof.timezone,
                prof.locale,
            )
            if ok:
                updated += 1
        return updated
