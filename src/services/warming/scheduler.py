from __future__ import annotations

import contextlib
import datetime
import hashlib
import inspect
import json
import math
import os
import random
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...core import config as app_config
from ...core.logging import get_logger
from ..browser.control import ControlError, PartialActionError, _check_url
from ..browser.launcher import BrowserControlError
from ..browser.lease import discard_lease, profile_lease, waiting_lease

if TYPE_CHECKING:
    from ...interfaces.protocols import IBrowserLauncher, IProfileManager

logger = get_logger("warming.scheduler")

WARMING_PRESETS: dict[str, list[str]] = {
    "youtube_trust": [
        "https://www.youtube.com",
        "https://www.google.com/search?q=technology+trends",
        "https://news.google.com",
    ],
    "social_news": [
        "https://www.wikipedia.org",
        "https://www.reddit.com",
        "https://news.ycombinator.com",
    ],
    "ecommerce_cookie": [
        "https://www.amazon.com",
        "https://www.ebay.com",
        "https://www.etsy.com",
    ],
    "custom": [],
    "script": [],
}

MAX_WARMING_STEPS = 100
MAX_SCRIPT_JSON_CHARS = 64_000
MAX_STEP_TIMEOUT_MS = 25_000
MAX_WAIT_SECONDS = 120.0
MAX_SELECTOR_CHARS = 500
MAX_TYPE_TEXT_CHARS = 2_000
MAX_KEY_CHARS = 64
MAX_URL_CHARS = 2_048
MAX_STEP_FIELDS = 16
WARMING_ALLOWED_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle", "commit")
WARMING_SCRIPT_ACTIONS = frozenset(
    {
        "navigate",
        "wait",
        "scroll",
        "click",
        "click_at",
        "type",
        "keyboard_type",
        "press",
        "wait_for",
        "back",
        "tab_new",
        "tab_select",
        "tab_close",
    }
)
_TAB_SWITCH_ACTIONS = frozenset({"tab_new", "tab_select", "tab_close"})
SAMPLE_WARMING_SCRIPT: list[dict[str, Any]] = [
    {"action": "navigate", "url": "https://news.google.com"},
    {"action": "wait", "min_s": 3, "max_s": 7},
    {"action": "scroll", "delta_y": 420, "humanize": True},
    {"action": "wait", "min_s": 2, "max_s": 5},
]

_RAW_SCENARIO_PLAYBOOKS: dict[str, list[dict[str, Any]]] = {
    "youtube_trust": [
        {"action": "navigate", "url": "https://news.google.com"},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "scroll", "delta_y": 520, "steps": 8, "humanize": True},
        {"action": "wait", "min_s": 6, "max_s": 12},
        {"action": "scroll", "delta_y": -220, "steps": 5, "humanize": True},
        {"action": "wait", "min_s": 3, "max_s": 6},
        {"action": "navigate", "url": "https://www.google.com/search?q=technology+trends"},
        {"action": "wait", "min_s": 5, "max_s": 9},
        {"action": "scroll", "delta_y": 580, "steps": 9, "humanize": True},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "navigate", "url": "https://www.youtube.com"},
        {"action": "wait", "min_s": 6, "max_s": 12},
        {"action": "scroll", "delta_y": 680, "steps": 10, "humanize": True},
        {"action": "wait", "min_s": 6, "max_s": 11},
    ],
    "social_news": [
        {"action": "navigate", "url": "https://www.wikipedia.org"},
        {"action": "wait", "min_s": 4, "max_s": 7},
        {
            "action": "type",
            "selector": "#searchInput",
            "text": "Web browser",
            "submit": True,
            "clear": True,
            "timeout": 8000,
            "optional": True,
        },
        {"action": "wait", "min_s": 5, "max_s": 9},
        {"action": "scroll", "delta_y": 560, "steps": 8, "humanize": True},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "press", "key": "PageDown", "optional": True},
        {"action": "wait", "min_s": 4, "max_s": 8},
        {"action": "scroll", "delta_y": -240, "steps": 6, "humanize": True},
        {"action": "back", "optional": True},
        {"action": "navigate", "url": "https://news.ycombinator.com"},
        {"action": "wait", "min_s": 4, "max_s": 8},
        {"action": "scroll", "delta_y": 520, "steps": 8, "humanize": True},
        {"action": "navigate", "url": "https://www.reddit.com"},
        {"action": "wait", "min_s": 5, "max_s": 10},
    ],
    "ecommerce_cookie": [
        {"action": "navigate", "url": "https://www.amazon.com"},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "scroll", "delta_y": 580, "steps": 9, "humanize": True},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "scroll", "delta_y": 620, "steps": 9, "humanize": True},
        {"action": "wait", "min_s": 4, "max_s": 8},
        {"action": "scroll", "delta_y": -260, "steps": 6, "humanize": True},
        {"action": "navigate", "url": "https://www.ebay.com"},
        {"action": "wait", "min_s": 5, "max_s": 9},
        {"action": "scroll", "delta_y": 540, "steps": 8, "humanize": True},
        {"action": "wait", "min_s": 5, "max_s": 9},
        {"action": "navigate", "url": "https://www.etsy.com"},
        {"action": "wait", "min_s": 5, "max_s": 10},
        {"action": "scroll", "delta_y": 560, "steps": 8, "humanize": True},
    ],
}

WARMING_SCENARIO_META: dict[str, dict[str, Any]] = {
    "youtube_trust": {"recommended_minutes": 4},
    "social_news": {"recommended_minutes": 4},
    "ecommerce_cookie": {"recommended_minutes": 4},
    "custom": {"recommended_minutes": 3},
    "script": {"recommended_minutes": 3},
}

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# How often a running task rereads the shared file for a Stop or Delete sent to another process.
SHARED_STOP_POLL_SECONDS = 0.25
# A navigation step can take 25 s before the owner sees a Stop; then it closes the browser.
PEER_STOP_WAIT_SECONDS = 60.0
# A run's result has no caller to retry it, so its save waits out another process's write.
STORE_WAIT_SECONDS = 5.0


class WarmingTargetSaveError(OSError):
    pass


class WarmingTaskNotFound(LookupError):
    pass


class WarmingStopTimeout(TimeoutError):
    pass


_SCRIPT_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "navigate": frozenset({"action", "url", "wait_until", "optional"}),
    "wait": frozenset({"action", "seconds", "min_s", "max_s"}),
    "scroll": frozenset({"action", "delta_y", "delta_x", "steps", "humanize", "optional"}),
    "click": frozenset({"action", "selector", "timeout", "optional"}),
    "click_at": frozenset({"action", "x", "y", "humanize", "optional"}),
    "type": frozenset({"action", "selector", "text", "submit", "clear", "timeout", "optional"}),
    "keyboard_type": frozenset({"action", "text", "humanize", "optional"}),
    "press": frozenset({"action", "key", "selector", "optional"}),
    "wait_for": frozenset({"action", "selector", "timeout", "optional"}),
    "back": frozenset({"action", "optional"}),
    "tab_new": frozenset({"action", "url", "optional"}),
    "tab_select": frozenset({"action", "index", "optional"}),
    "tab_close": frozenset({"action", "index", "optional"}),
}


def _ensure_utf8(value: str, field_name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} contains invalid Unicode characters") from exc
    return value


def _validate_script_url(raw: Any, *, field_name: str = "url") -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    _ensure_utf8(raw, field_name)
    if len(raw.strip()) > MAX_URL_CHARS:
        raise ValueError(f"{field_name} exceeds {MAX_URL_CHARS} characters")
    try:
        checked = _check_url(raw)
    except ControlError as exc:
        raise ValueError(str(exc)) from exc
    if len(checked) > MAX_URL_CHARS:
        raise ValueError(f"{field_name} exceeds {MAX_URL_CHARS} characters")
    if urlparse(checked).scheme.lower() not in ("http", "https"):
        raise ValueError(f"Only http/https URLs are allowed for {field_name}")
    return checked


def _require_str(raw: Any, field_name: str, *, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string")
    _ensure_utf8(raw, field_name)
    if not allow_empty and not raw.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(raw) > max_len:
        raise ValueError(f"{field_name} exceeds {max_len} characters")
    return raw if allow_empty else raw.strip()


def _require_bool(raw: Any, field_name: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return raw


def _require_int(raw: Any, field_name: str, *, lo: int, hi: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{field_name} must be an integer")
    if not lo <= raw <= hi:
        raise ValueError(f"{field_name} must be between {lo} and {hi}")
    return raw


def _require_number(raw: Any, field_name: str, *, lo: float, hi: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    if isinstance(raw, int):
        if not lo <= raw <= hi:
            raise ValueError(f"{field_name} must be between {lo} and {hi}")
        return float(raw)
    if not math.isfinite(raw) or not lo <= raw <= hi:
        raise ValueError(f"{field_name} must be between {lo} and {hi}")
    return raw


def _normalize_step(idx: int, step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValueError(f"Step {idx} must be a JSON object")
    if len(step) > MAX_STEP_FIELDS:
        raise ValueError(f"Step {idx} has too many fields (max {MAX_STEP_FIELDS})")
    action = step.get("action")
    if not isinstance(action, str) or action not in WARMING_SCRIPT_ACTIONS:
        raise ValueError(f"Step {idx}: unsupported action {action!a}")
    extra = set(step) - _SCRIPT_ALLOWED_KEYS[action]
    if extra:
        extra_names = ", ".join(ascii(str(k)) for k in sorted(extra, key=str))
        raise ValueError(f"Step {idx} ({action}): unsupported field(s): {extra_names}")

    if action == "wait":
        has_seconds = "seconds" in step
        has_range = "min_s" in step or "max_s" in step
        if has_seconds == has_range:
            raise ValueError(
                f"Step {idx} (wait): specify either 'seconds' or both 'min_s' and 'max_s'"
            )
        if has_seconds:
            return {
                "action": "wait",
                "seconds": _require_number(step["seconds"], "seconds", lo=0.0, hi=MAX_WAIT_SECONDS),
            }
        if "min_s" not in step or "max_s" not in step:
            raise ValueError(f"Step {idx} (wait): both 'min_s' and 'max_s' are required")
        min_s = _require_number(step["min_s"], "min_s", lo=0.0, hi=MAX_WAIT_SECONDS)
        max_s = _require_number(step["max_s"], "max_s", lo=0.0, hi=MAX_WAIT_SECONDS)
        if min_s > max_s:
            raise ValueError(f"Step {idx} (wait): min_s must be <= max_s")
        return {"action": "wait", "min_s": min_s, "max_s": max_s}

    out: dict[str, Any] = {"action": action}
    if action == "navigate":
        out["url"] = _validate_script_url(step.get("url"))
        wait_until = step.get("wait_until", "domcontentloaded")
        if not isinstance(wait_until, str) or wait_until not in WARMING_ALLOWED_WAIT_UNTIL:
            raise ValueError(
                f"Step {idx} (navigate): wait_until must be one of {WARMING_ALLOWED_WAIT_UNTIL}"
            )
        out["wait_until"] = wait_until
    elif action == "scroll":
        out["delta_y"] = _require_number(
            step.get("delta_y", 380.0), "delta_y", lo=-10_000.0, hi=10_000.0
        )
        out["delta_x"] = _require_number(
            step.get("delta_x", 0.0), "delta_x", lo=-10_000.0, hi=10_000.0
        )
        out["steps"] = _require_int(step.get("steps", 6), "steps", lo=1, hi=50)
        out["humanize"] = _require_bool(step.get("humanize", True), "humanize")
    elif action == "click":
        out["selector"] = _require_str(step.get("selector"), "selector", max_len=MAX_SELECTOR_CHARS)
        out["timeout"] = _require_int(
            step.get("timeout", 10_000), "timeout", lo=100, hi=MAX_STEP_TIMEOUT_MS
        )
    elif action == "click_at":
        out["x"] = _require_number(step.get("x"), "x", lo=0.0, hi=10_000.0)
        out["y"] = _require_number(step.get("y"), "y", lo=0.0, hi=10_000.0)
        out["humanize"] = _require_bool(step.get("humanize", True), "humanize")
    elif action == "type":
        out["selector"] = _require_str(step.get("selector"), "selector", max_len=MAX_SELECTOR_CHARS)
        out["text"] = _require_str(
            step.get("text"), "text", max_len=MAX_TYPE_TEXT_CHARS, allow_empty=True
        )
        out["submit"] = _require_bool(step.get("submit", False), "submit")
        out["clear"] = _require_bool(step.get("clear", True), "clear")
        out["timeout"] = _require_int(
            step.get("timeout", 10_000), "timeout", lo=100, hi=MAX_STEP_TIMEOUT_MS
        )
    elif action == "keyboard_type":
        out["text"] = _require_str(
            step.get("text"), "text", max_len=MAX_TYPE_TEXT_CHARS, allow_empty=True
        )
        out["humanize"] = _require_bool(step.get("humanize", True), "humanize")
    elif action == "press":
        out["key"] = _require_str(step.get("key"), "key", max_len=MAX_KEY_CHARS)
        if step.get("selector") is not None:
            out["selector"] = _require_str(step["selector"], "selector", max_len=MAX_SELECTOR_CHARS)
    elif action == "wait_for":
        out["selector"] = _require_str(step.get("selector"), "selector", max_len=MAX_SELECTOR_CHARS)
        out["timeout"] = _require_int(
            step.get("timeout", 15_000), "timeout", lo=100, hi=MAX_STEP_TIMEOUT_MS
        )
    elif action == "tab_new":
        if step.get("url") is not None:
            out["url"] = _validate_script_url(step["url"])
    elif action in ("tab_select", "tab_close"):
        out["index"] = _require_int(step.get("index"), "index", lo=0, hi=100)

    if "optional" in step and _require_bool(step["optional"], "optional"):
        out["optional"] = True
    return out


def normalize_warming_steps(steps: list[dict[str, Any]] | str) -> list[dict[str, Any]]:
    """Validate and normalize a local warming JSON script into safe browser steps."""
    if isinstance(steps, str):
        raw_text = steps.strip()
        if not raw_text:
            raise ValueError("Script JSON cannot be empty")
        if len(raw_text) > MAX_SCRIPT_JSON_CHARS:
            raise ValueError(f"Script JSON exceeds {MAX_SCRIPT_JSON_CHARS} characters")
        _ensure_utf8(raw_text, "script")
        try:
            raw_list = json.loads(raw_text)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid JSON syntax: {exc}") from exc
    else:
        raw_list = steps

    if not isinstance(raw_list, list) or not (1 <= len(raw_list) <= MAX_WARMING_STEPS):
        raise ValueError(f"Script must be a JSON array of 1 to {MAX_WARMING_STEPS} step(s)")

    normalized = [_normalize_step(idx, step) for idx, step in enumerate(raw_list, 1)]
    if not any(step["action"] != "wait" for step in normalized):
        raise ValueError("Script must include at least one browser action step")
    try:
        encoded = json.dumps(normalized, ensure_ascii=False)
        encoded.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Script JSON contains invalid Unicode characters") from exc
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Script JSON cannot be serialized: {type(exc).__name__}") from exc
    if len(encoded) > MAX_SCRIPT_JSON_CHARS:
        raise ValueError(f"Script JSON exceeds {MAX_SCRIPT_JSON_CHARS} characters")
    return normalized


WARMING_SCENARIO_PLAYBOOKS: dict[str, list[dict[str, Any]]] = {
    key: normalize_warming_steps(steps) for key, steps in _RAW_SCENARIO_PLAYBOOKS.items()
}


def _redact_url_for_display(raw: Any, *, empty_label: str = "URL") -> str:
    """Return `host[:port][/path]` without userinfo/query/fragment, or safe fallback."""
    if not isinstance(raw, str):
        return "URL"
    text = raw.strip()
    if not text:
        return empty_label
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        path = parsed.path or ""
    except ValueError:
        return "URL"
    if not host:
        return "URL"
    if len(path) > 22:
        path = path[:19] + "..."
    return f"{host}{port}{path}"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def describe_warming_step(step: Any) -> tuple[str, str]:
    """Return a short, secret-free `(action, detail)` summary for UI step previews."""
    if not isinstance(step, dict):
        return "step", ""
    raw_action = step.get("action")
    action = raw_action.strip() if isinstance(raw_action, str) and raw_action.strip() else "step"
    if action in ("navigate", "tab_new"):
        raw_url = step.get("url")
        if raw_url is None:
            return action, "Blank tab"
        return action, _redact_url_for_display(raw_url, empty_label="Blank tab")
    if action == "wait":
        sec = _finite_number(step.get("seconds"))
        if sec is not None:
            return "wait", f"{sec:g}s"
        min_s = _finite_number(step.get("min_s"))
        max_s = _finite_number(step.get("max_s"))
        return (
            "wait",
            f"{min_s if min_s is not None else 1:g}-{max_s if max_s is not None else 3:g}s",
        )
    if action == "scroll":
        raw_dy = _finite_number(step.get("delta_y"))
        dy = int(round(raw_dy if raw_dy is not None else 380.0))
        direction = "Down" if dy >= 0 else "Up"
        return "scroll", f"{direction} {abs(dy)}px"
    if action == "type":
        raw_sel = step.get("selector")
        raw_txt = step.get("text")
        sel = raw_sel[:24] if isinstance(raw_sel, str) else ""
        txt = raw_txt[:20] if isinstance(raw_txt, str) else ""
        suffix = " + Enter" if step.get("submit") is True else ""
        return "type", f'{sel} "{txt}"{suffix}'.strip()
    if action == "keyboard_type":
        raw_txt = step.get("text")
        txt = raw_txt[:24] if isinstance(raw_txt, str) else ""
        return "keyboard", f'"{txt}"'
    if action in ("click", "wait_for"):
        raw_sel = step.get("selector")
        return action, raw_sel[:34] if isinstance(raw_sel, str) else ""
    if action == "click_at":
        x = _finite_number(step.get("x"))
        y = _finite_number(step.get("y"))
        return "click_at", f"({int(x if x is not None else 0)}, {int(y if y is not None else 0)})"
    if action == "press":
        raw_key = step.get("key")
        key = raw_key if isinstance(raw_key, str) else ""
        raw_sel = step.get("selector")
        sel = raw_sel[:20] if isinstance(raw_sel, str) else ""
        return "press", f"{key} ({sel})" if sel else key
    if action == "back":
        return "back", "History back"
    if action in ("tab_select", "tab_close"):
        idx = _finite_number(step.get("index"))
        return action, f"Tab #{int(idx if idx is not None else 0)}"
    return action, ""


def list_warming_scenarios() -> list[dict[str, Any]]:
    """Return metadata and human-like playbooks for every Warming scenario."""
    scenarios: list[dict[str, Any]] = []
    for scenario_id, meta in WARMING_SCENARIO_META.items():
        if scenario_id in WARMING_SCENARIO_PLAYBOOKS:
            sample_steps = [dict(s) for s in WARMING_SCENARIO_PLAYBOOKS[scenario_id]]
        elif scenario_id == "script":
            sample_steps = [dict(s) for s in SAMPLE_WARMING_SCRIPT]
        else:
            sample_steps = []
        actions: list[str] = []
        for s in sample_steps:
            act = str(s["action"])
            if act not in actions:
                actions.append(act)
        if not actions:
            actions = ["navigate", "scroll", "wait"]
        scenarios.append(
            {
                "id": scenario_id,
                "recommended_minutes": int(meta["recommended_minutes"]),
                "default_urls": list(WARMING_PRESETS.get(scenario_id, [])),
                "sample_steps": sample_steps,
                "interactive_actions": actions,
            }
        )
    return scenarios


def validate_schedule_time(value: str) -> bool:
    val = (value or "").strip()
    if not val:
        return True
    return bool(_TIME_RE.match(val))


def normalize_warming_urls(urls: list[str] | str) -> list[str]:
    raw_list = urls.split() if isinstance(urls, str) else [str(u).strip() for u in urls]
    cleaned: list[str] = []
    for u in raw_list:
        if not u:
            continue
        try:
            checked = _check_url(u)
            parsed = urlparse(checked)
            _ = parsed.port
        except (ControlError, ValueError):
            continue
        if (
            parsed.scheme.lower() in ("http", "https")
            and parsed.hostname
            and "\\" not in parsed.netloc
            and not any(ch.isspace() for ch in parsed.netloc)
        ):
            cleaned.append(checked)
    return cleaned


@dataclass
class WarmingTask:
    id: str
    name: str
    profile_names: list[str] = field(default_factory=list)
    preset: str = "youtube_trust"
    custom_urls: list[str] = field(default_factory=list)
    script_steps: list[dict[str, Any]] = field(default_factory=list)
    duration_minutes: int = 3
    schedule_time: str = ""  # "HH:MM" or "" for manual
    repeat_daily: bool = True
    enabled: bool = True
    status: str = "idle"  # "idle" | "running" | "completed" | "failed"
    last_run: float = 0.0
    last_result: str = ""
    last_triggered_slot: str = ""

    def resolved_urls(self) -> list[str]:
        if self.preset == "script":
            return []
        if self.preset == "custom" or self.custom_urls:
            return normalize_warming_urls(self.custom_urls)
        return list(WARMING_PRESETS.get(self.preset) or WARMING_PRESETS["youtube_trust"])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WarmingTask | None:
        raw_preset = str(data.get("preset") or "youtube_trust")
        raw_steps = data.get("script_steps")
        name = str(data.get("name") or "").strip()
        if not name:
            if raw_preset == "script" or (raw_steps is not None and raw_steps != []):
                raise ValueError("Script warming task is missing a valid name")
            return None
        tid = str(data.get("id") or secrets.token_hex(5))
        preset = raw_preset if raw_preset in WARMING_PRESETS else "youtube_trust"
        sched = str(data.get("schedule_time") or "").strip()
        if not validate_schedule_time(sched):
            sched = ""
        dur = max(1, min(60, int(data.get("duration_minutes") or 3)))
        pnames = [str(p).strip() for p in (data.get("profile_names") or []) if str(p).strip()]
        raw_custom = data.get("custom_urls") or []
        if isinstance(raw_custom, str):
            raw_tokens = raw_custom.split()
        elif isinstance(raw_custom, list):
            raw_tokens = [str(u).strip() for u in raw_custom if str(u).strip()]
        else:
            raw_tokens = []
        valid_urls = normalize_warming_urls(raw_tokens)
        # Keep legacy unaccepted override tokens intact instead of collapsing to preset defaults.
        urls = valid_urls if len(valid_urls) == len(raw_tokens) else raw_tokens
        raw_steps = data.get("script_steps")
        if preset == "script":
            steps = normalize_warming_steps(raw_steps)
        elif raw_steps is not None and raw_steps != []:
            raise ValueError("script_steps is only valid for preset='script'")
        else:
            steps = []
        status = str(data.get("status") or "idle")
        if status not in ("idle", "running", "completed", "failed"):
            status = "idle"
        return cls(
            id=tid,
            name=name,
            profile_names=pnames,
            preset=preset,
            custom_urls=urls,
            script_steps=steps,
            duration_minutes=dur,
            schedule_time=sched,
            repeat_daily=bool(data.get("repeat_daily", True)),
            enabled=bool(data.get("enabled", True)),
            status=status,
            last_run=float(data.get("last_run") or 0.0),
            last_result=str(data.get("last_result") or ""),
            last_triggered_slot=str(data.get("last_triggered_slot") or ""),
        )


class WarmingSchedulerService:
    """Manages account warming presets, scheduled tasks, and automated browsing journeys.

    In-process script warming enforces per-profile exclusivity against other warming
    tasks (script<->script and script<->URL); external manual/API/MCP browser actions
    remain serialized per action in control.py rather than locked across sequences.
    """

    def __init__(self, file_path: str | None = None) -> None:
        self._explicit_path = file_path
        self._lock = threading.RLock()
        self._store_locked = False
        self._lease_lock = threading.RLock()
        self._tasks: dict[str, WarmingTask] = {}
        self._saved_tasks: dict[str, dict] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._profile_active_users: dict[str, int] = {}
        self._profile_launched_by_warming: set[str] = set()
        self._profile_script_owners: dict[str, threading.Event] = {}
        self._daemon_stop = threading.Event()
        self._daemon_thread: threading.Thread | None = None
        self._daemon_log_fn: Callable[[str], None] | None = None
        self._daemon_on_update: Callable[[], None] | None = None
        self.reload()

    @property
    def file_path(self) -> str:
        return self._explicit_path or app_config.WARMING_FILE

    def reload(self) -> None:
        with self._lock:
            try:
                self._tasks = self._read_tasks()
            except OSError as e:
                logger.error("Failed to load warming tasks from %s: %s", self.file_path, e)
                self._tasks = {}
            for task in self._tasks.values():
                if task.status == "running":
                    task.status = "idle"
            self._saved_tasks = self._snapshot()

    def _read_tasks(self) -> dict[str, WarmingTask]:
        try:
            with open(self.file_path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise OSError("Invalid warming task store") from exc
        items = raw.get("tasks", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise OSError("Invalid warming task store")
        tasks = {}
        try:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if not item.get("id"):
                    payload = json.dumps([index, item], sort_keys=True).encode()
                    item = {**item, "id": hashlib.sha256(payload).hexdigest()[:10]}
                if (task := WarmingTask.from_dict(item)) is not None:
                    tasks[task.id] = task
        except (TypeError, ValueError, OverflowError) as exc:
            raise OSError("Invalid warming task store") from exc
        return tasks

    def _snapshot(self) -> dict[str, dict]:
        return {tid: task.to_dict() for tid, task in self._tasks.items()}

    @contextlib.contextmanager
    def _store_lease(self, *, timeout: float = STORE_WAIT_SECONDS) -> Iterator[None]:
        with self._lock:
            if self._store_locked:
                yield
                return
            with waiting_lease(self.file_path, timeout):
                self._store_locked = True
                try:
                    yield
                finally:
                    self._store_locked = False

    def _save(self) -> None:
        with self._store_lease():
            path = self.file_path
            disk = {tid: task.to_dict() for tid, task in self._read_tasks().items()}
            current = self._snapshot()
            # Persist only local changes so stale snapshots cannot undo another writer's edits.
            for tid in self._saved_tasks.keys() - current.keys():
                disk.pop(tid, None)
            for tid, task in current.items():
                previous = self._saved_tasks.get(tid)
                if previous is None:
                    disk[tid] = task
                elif tid in disk:
                    disk[tid].update(
                        {key: value for key, value in task.items() if value != previous[key]}
                    )
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".warming_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(list(disk.values()), f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            self._saved_tasks = current

    def list_tasks(self) -> list[WarmingTask]:
        with self._lock:
            return list(self._tasks.values())

    def get_task(self, task_id: str) -> WarmingTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    @contextlib.contextmanager
    def retarget_profile(self, old_name: str, new_name: str) -> Iterator[None]:
        with contextlib.ExitStack() as lease:
            try:
                lease.enter_context(self._store_lease())
                latest = self._read_tasks()
            except OSError as exc:
                logger.exception("Could not read warming targets for rename")
                raise WarmingTargetSaveError("Could not save renamed warming targets") from exc
            for tid, task in latest.items():
                if task.status == "running" and tid not in self._cancel_flags:
                    task.status = "idle"
                if tid in self._tasks:
                    current = self._tasks[tid]
                    current.__dict__.update(vars(task))
                    latest[tid] = current
            self._tasks = latest
            self._saved_tasks = self._snapshot()
            previous = [
                (task, task.profile_names)
                for task in self._tasks.values()
                if old_name in task.profile_names
            ]
            if not previous:
                yield
                return
            for task, names in previous:
                task.profile_names = [new_name if name == old_name else name for name in names]
            try:
                try:
                    self._save()
                except OSError as exc:
                    logger.exception("Could not save renamed warming targets")
                    raise WarmingTargetSaveError("Could not save renamed warming targets") from exc
            except Exception:
                for task, names in previous:
                    task.profile_names = names
                raise
            try:
                yield
            except Exception:
                for task, names in previous:
                    task.profile_names = names
                self._save()
                raise

    def add_task(
        self,
        name: str,
        profile_names: list[str],
        preset: str = "youtube_trust",
        custom_urls: list[str] | str | None = None,
        duration_minutes: int = 3,
        schedule_time: str = "",
        repeat_daily: bool = True,
        enabled: bool = True,
        script_steps: list[dict[str, Any]] | str | None = None,
    ) -> WarmingTask | None:
        clean_name = (name or "").strip()
        clean_profiles = [p.strip() for p in profile_names if p and p.strip()]
        if not clean_name or not clean_profiles:
            return None
        sched = (schedule_time or "").strip()
        if not validate_schedule_time(sched):
            return None
        if preset not in WARMING_PRESETS:
            preset = "youtube_trust"
        if preset == "script":
            if script_steps is None:
                return None
            try:
                steps = normalize_warming_steps(script_steps)
            except ValueError:
                return None
            urls: list[str] = []
        else:
            if script_steps:
                return None
            steps = []
            raw_tokens = (
                custom_urls.split()
                if isinstance(custom_urls, str)
                else [str(u).strip() for u in (custom_urls or []) if str(u).strip()]
            )
            urls = normalize_warming_urls(raw_tokens)
            if (preset == "custom" or raw_tokens) and not urls:
                return None
        with self._lock:
            tid = secrets.token_hex(5)
            task = WarmingTask(
                id=tid,
                name=clean_name,
                profile_names=clean_profiles,
                preset=preset,
                custom_urls=urls,
                script_steps=steps,
                duration_minutes=max(1, min(60, int(duration_minutes or 3))),
                schedule_time=sched,
                repeat_daily=repeat_daily,
                enabled=enabled,
            )
            self._tasks[tid] = task
            try:
                self._save()
            except Exception:
                del self._tasks[tid]
                raise
            return task

    def delete_task(self, task_id: str) -> bool:
        with self._store_lease():
            if task_id not in self._tasks:
                return False
            previous_tasks = self._tasks.copy()
            del self._tasks[task_id]
            try:
                self._save()
            except Exception:
                self._tasks = previous_tasks
                raise
            cancel = self._cancel_flags.pop(task_id, None)
            if cancel:
                cancel.set()
            else:
                # Runs take this lease only under the store lease, after checking the task exists.
                discard_lease(self._run_lease_path(task_id))
            return True

    def stop_task(self, task_id: str) -> bool:
        """Stop a run, waiting for the process that owns it when that is another process."""
        with self._store_lease():
            task = self._tasks.get(task_id)
            if not task:
                return False
            previous = task.status, self._saved_tasks.get(task_id)
            saved = self._read_tasks().get(task_id)
            if saved is not None and task_id in self._saved_tasks:
                # Start from the shared status: this process may never have seen a peer's run start.
                task.status = saved.status
                self._saved_tasks[task_id] = {**self._saved_tasks[task_id], "status": saved.status}
            task.status = "idle"
            try:
                self._save()
            except Exception:
                task.status = previous[0]
                if previous[1] is not None:
                    self._saved_tasks[task_id] = previous[1]
                raise
            cancel = self._cancel_flags.get(task_id)
            if cancel:
                cancel.set()
                return True
        try:
            with waiting_lease(self._run_lease_path(task_id), PEER_STOP_WAIT_SECONDS):
                pass
        except (BlockingIOError, PermissionError) as exc:
            raise WarmingStopTimeout(task_id) from exc
        return True

    def _run_lease_path(self, task_id: str) -> str:
        return f"{self.file_path}.run-{task_id}"

    def _own_run(
        self,
        run_lease: contextlib.ExitStack,
        task: WarmingTask,
        cancel_ev: threading.Event,
        *args,
    ) -> bool:
        """Run the task while holding its cross-process run lease, released after the result is saved."""
        with run_lease:
            threading.Thread(
                target=self._watch_shared_stop, args=(task.id, cancel_ev), daemon=True
            ).start()
            return self._execute_task(task, cancel_ev, *args)

    def _watch_shared_stop(self, task_id: str, cancel_ev: threading.Event) -> None:
        """Carry a Stop or Delete that a peer process wrote to the shared file to this run."""
        while True:
            time.sleep(SHARED_STOP_POLL_SECONDS)
            if cancel_ev.is_set() or self._cancel_flags.get(task_id) is not cancel_ev:
                return
            try:
                saved = self._read_tasks().get(task_id)
            except OSError:
                continue
            if saved is None or saved.status != "running":
                cancel_ev.set()
                return

    def execute_task_sync(
        self,
        task_id: str,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        step_dwell_seconds: float | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> bool:
        """Execute a warming task synchronously."""
        with self._store_lease():
            started = self._begin_task(task_id)
        if started is None:
            return False
        task, cancel_ev, _, run_lease = started
        return self._own_run(
            run_lease, task, cancel_ev, pm, bl, log_fn, step_dwell_seconds, on_update
        )

    def _begin_task(
        self, task_id: str, *, scheduled_slot: str | None = None, require_exists: bool = False
    ) -> tuple[WarmingTask, threading.Event, tuple[str, str], contextlib.ExitStack] | None:
        task = self._tasks.get(task_id)
        if task is None or task_id not in self._read_tasks():
            if require_exists:
                raise WarmingTaskNotFound(task_id)
            return None
        if task.status == "running":
            return None
        run_lease = contextlib.ExitStack()
        try:
            run_lease.enter_context(profile_lease(self._run_lease_path(task_id)))
        # Another process is running this task: it owns the result.
        except (BlockingIOError, PermissionError):
            return None
        previous_state = task.status, task.last_triggered_slot
        task.status = "running"
        if scheduled_slot is not None:
            task.last_triggered_slot = scheduled_slot
        cancel_ev = threading.Event()
        self._cancel_flags[task_id] = cancel_ev
        try:
            self._save()
        except Exception:
            task.status, task.last_triggered_slot = previous_state
            self._cancel_flags.pop(task_id, None)
            run_lease.close()
            raise
        return replace(task), cancel_ev, previous_state, run_lease

    @staticmethod
    def _safe_step_error(exc: Exception) -> str:
        status = getattr(exc, "status", None)
        if isinstance(exc, BrowserControlError) or isinstance(status, int):
            return f"{type(exc).__name__} (HTTP {status})"
        return type(exc).__name__

    @staticmethod
    def _is_unknown_state_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, PartialActionError)):
            return True
        if getattr(exc, "unknown_state", False) is True:
            return True
        if getattr(exc, "error_code", None) == "state_uncertain":
            return True
        return getattr(exc, "status", None) in (409, 503, 504)

    @staticmethod
    def _supports_require_current(bl: IBrowserLauncher) -> bool:
        ctrl_params = inspect.signature(bl.control).parameters
        return "require_current" in ctrl_params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in ctrl_params.values()
        )

    @staticmethod
    def _supports_page_pin(bl: IBrowserLauncher) -> bool:
        ctrl_params = inspect.signature(bl.control).parameters
        return "target_page_id" in ctrl_params and "return_page_id" in ctrl_params

    def _run_script_steps(
        self,
        prof_name: str,
        steps: list[dict[str, Any]],
        cancel_ev: threading.Event,
        bl: IBrowserLauncher,
        log: Callable[[str], None],
        total_budget_seconds: float,
        step_dwell_seconds: float | None,
    ) -> str:
        deadline = time.monotonic() + total_budget_seconds if step_dwell_seconds is None else None
        total_steps = len(steps)
        acted_any = False
        has_target = False
        supports_pin = self._supports_require_current(bl)

        for step_idx, step in enumerate(steps, 1):
            if cancel_ev.is_set():
                return "stopped"
            if deadline is not None and time.monotonic() >= deadline:
                log(
                    f"[Warming] [{prof_name}] Script budget reached before step {step_idx}/{total_steps}"
                )
                return "timed_out"

            action = step["action"]
            if action not in WARMING_SCRIPT_ACTIONS:
                log(f"[Warming] [{prof_name}] Rejected unsupported step action '{action}'")
                return "failed"

            if action == "wait":
                if step_dwell_seconds is not None:
                    requested_wait = max(0.0, float(step_dwell_seconds))
                elif "seconds" in step:
                    requested_wait = float(step["seconds"])
                else:
                    requested_wait = random.uniform(float(step["min_s"]), float(step["max_s"]))
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    truncated = requested_wait > remaining
                    wait_s = min(requested_wait, remaining)
                else:
                    truncated = False
                    wait_s = requested_wait
                log(f"[Warming] [{prof_name}] Step {step_idx}/{total_steps}: wait ({wait_s:.1f}s)")
                if cancel_ev.wait(wait_s):
                    return "stopped"
                if truncated:
                    log(
                        f"[Warming] [{prof_name}] Script budget reached during wait step {step_idx}/{total_steps}"
                    )
                    return "timed_out"
                continue

            params = {k: v for k, v in step.items() if k not in ("action", "optional")}
            step_timeout_clamped = False
            try:
                if action in ("navigate", "tab_new") and params.get("url") is not None:
                    params["url"] = _validate_script_url(params["url"])
                if "timeout" in params:
                    base_ctrl_timeout = min(30.0, max(5.0, params["timeout"] / 1000.0 + 5.0))
                elif action in ("navigate", "tab_new"):
                    base_ctrl_timeout = 25.0
                else:
                    base_ctrl_timeout = 10.0

                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        log(
                            f"[Warming] [{prof_name}] Script budget reached before step {step_idx}/{total_steps}"
                        )
                        return "timed_out"
                    ctrl_timeout = min(base_ctrl_timeout, remaining)
                    if "timeout" in params:
                        clamped_ms = max(1, min(params["timeout"], int(remaining * 1000)))
                        step_timeout_clamped = clamped_ms < params["timeout"]
                        params["timeout"] = clamped_ms
                else:
                    ctrl_timeout = base_ctrl_timeout

                pin_target = has_target and action not in _TAB_SWITCH_ACTIONS
                if pin_target and supports_pin:
                    bl.control(
                        prof_name, action, params, timeout=ctrl_timeout, require_current=True
                    )
                else:
                    bl.control(prof_name, action, params, timeout=ctrl_timeout)
                acted_any = True
                has_target = True
                # Never log typed text or raw URLs/credentials in warming logs.
                log(f"[Warming] [{prof_name}] Step {step_idx}/{total_steps}: {action}")
            except Exception as step_err:
                if cancel_ev.is_set():
                    return "stopped"
                err_label = self._safe_step_error(step_err)
                if self._is_unknown_state_error(step_err):
                    log(
                        f"[Warming] [{prof_name}] Step {step_idx}/{total_steps} ({action}) interrupted: {err_label}"
                    )
                    if deadline is not None and time.monotonic() >= deadline:
                        return "timed_out"
                    return "interrupted"
                if step.get("optional"):
                    log(
                        f"[Warming] [{prof_name}] Optional step {step_idx}/{total_steps} ({action}) warning: {err_label}"
                    )
                    if (
                        step_timeout_clamped
                        and getattr(step_err, "error_code", None) == "action_timeout"
                    ):
                        log(
                            f"[Warming] [{prof_name}] Script budget reached on optional step {step_idx}/{total_steps}"
                        )
                        return "timed_out"
                    if getattr(step_err, "error_code", None) in ("action_timeout", "action_error"):
                        has_target = True
                    continue
                log(
                    f"[Warming] [{prof_name}] Step {step_idx}/{total_steps} ({action}) failed: {err_label}"
                )
                return "failed"

        if cancel_ev.is_set():
            return "stopped"
        if deadline is not None and time.monotonic() > deadline:
            log(f"[Warming] [{prof_name}] Script budget exceeded before completion")
            return "timed_out"
        return "ok" if acted_any else "failed"

    def _execute_task(
        self,
        task: WarmingTask,
        cancel_ev: threading.Event,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None,
        step_dwell_seconds: float | None,
        on_update: Callable[[], None] | None,
    ) -> bool:
        log = log_fn or (lambda m: logger.info(m))
        task_id = task.id
        is_script = task.preset == "script"
        steps = task.script_steps
        urls = [] if is_script else task.resolved_urls()
        invalid_urls = (not is_script) and (not urls)
        targets = task.profile_names
        dur_mins = task.duration_minutes
        task_name = task.name

        if on_update:
            with contextlib.suppress(Exception):
                on_update()

        kind_desc = f"{len(steps)} step(s)" if is_script else f"{len(urls)} sites"
        log(f"[Warming] Starting '{task_name}' for {len(targets)} profile(s) ({kind_desc})")

        total_budget_seconds = float(max(1, dur_mins) * 60)
        per_url_dwell = (
            float(step_dwell_seconds)
            if step_dwell_seconds is not None
            else min(20.0, max(2.0, total_budget_seconds / max(1, len(urls))))
        )

        succeeded = 0
        busy_count = 0
        timed_out_count = 0
        interrupted_count = 0
        if invalid_urls:
            log(
                f"[Warming] Task '{task_name}' has no valid HTTP/HTTPS URLs; update its custom URLs before running"
            )
        for prof_name in [] if invalid_urls else targets:
            if cancel_ev.is_set():
                break
            prof = pm.profiles.get(prof_name)
            if not prof:
                log(f"[Warming] Skipped missing profile '{prof_name}'")
                continue

            acquired_lease = False
            acquired_script_lock = False
            try:
                with self._lease_lock:
                    active = self._profile_active_users.get(prof_name, 0)
                    script_busy = prof_name in self._profile_script_owners
                    if (is_script and (active > 0 or script_busy)) or (
                        not is_script and script_busy
                    ):
                        busy_count += 1
                        log(
                            f"[Warming] [{prof_name}] Busy: profile is in use by another warming task"
                        )
                        continue
                    if is_script:
                        self._profile_script_owners[prof_name] = cancel_ev
                        acquired_script_lock = True
                    self._profile_active_users[prof_name] = active + 1
                    acquired_lease = True
                    need_launch = not bl.is_running(prof_name)
                    if need_launch:
                        started = bl.start_thread(prof, log)
                        if started:
                            self._profile_launched_by_warming.add(prof_name)
                    else:
                        started = False

                if need_launch and started:
                    ok, err = bl.wait_for_launch(prof_name, timeout=45.0)
                    if not ok:
                        log(f"[Warming] '{prof_name}' launch failed: {err}")
                        continue

                if is_script:
                    outcome = self._run_script_steps(
                        prof_name,
                        steps,
                        cancel_ev,
                        bl,
                        log,
                        total_budget_seconds,
                        step_dwell_seconds,
                    )
                    if outcome == "ok":
                        succeeded += 1
                    elif outcome == "timed_out":
                        timed_out_count += 1
                    elif outcome == "interrupted":
                        interrupted_count += 1
                    continue

                visited_any = False
                has_target = False
                pinned_page_id: str | None = None
                url_interrupted = False
                supports_pin = self._supports_require_current(bl)
                supports_page_pin = self._supports_page_pin(bl)
                single_pass = step_dwell_seconds is not None
                remaining_dwell = total_budget_seconds
                while not cancel_ev.is_set() and not url_interrupted:
                    visited_in_pass = False
                    for url in urls:
                        if cancel_ev.is_set() or (not single_pass and remaining_dwell <= 0):
                            break
                        safe_url = _redact_url_for_display(url)
                        try:
                            nav_params = {"url": url, "wait_until": "domcontentloaded"}
                            if supports_page_pin:
                                if not has_target:
                                    res = bl.control(
                                        prof_name,
                                        "navigate",
                                        nav_params,
                                        timeout=25.0,
                                        return_page_id=True,
                                    )
                                    pid = (
                                        res[1] if isinstance(res, tuple) and len(res) == 2 else None
                                    )
                                    if not isinstance(pid, str) or not pid:
                                        log(
                                            f"[Warming] [{prof_name}] Navigate interrupted on {safe_url}: missing page identity"
                                        )
                                        url_interrupted = True
                                        break
                                    pinned_page_id = pid
                                else:
                                    bl.control(
                                        prof_name,
                                        "navigate",
                                        nav_params,
                                        timeout=25.0,
                                        require_current=True,
                                        target_page_id=pinned_page_id,
                                    )
                            elif has_target and supports_pin:
                                bl.control(
                                    prof_name,
                                    "navigate",
                                    nav_params,
                                    timeout=25.0,
                                    require_current=True,
                                )
                            else:
                                bl.control(prof_name, "navigate", nav_params, timeout=25.0)
                            visited_any = True
                            visited_in_pass = True
                            has_target = True
                            log(f"[Warming] [{prof_name}] Visited {safe_url}")
                        except Exception as nav_err:
                            if cancel_ev.is_set():
                                break
                            err_label = self._safe_step_error(nav_err)
                            if self._is_unknown_state_error(nav_err):
                                log(
                                    f"[Warming] [{prof_name}] Navigate interrupted on {safe_url}: {err_label}"
                                )
                                url_interrupted = True
                                break
                            if supports_page_pin and not has_target:
                                err_pid = getattr(nav_err, "page_id", None)
                                if isinstance(err_pid, str) and err_pid:
                                    pinned_page_id = err_pid
                                    has_target = True
                                else:
                                    log(
                                        f"[Warming] [{prof_name}] Navigate interrupted on {safe_url}: missing page identity"
                                    )
                                    url_interrupted = True
                                    break
                            log(
                                f"[Warming] [{prof_name}] Navigate warning on {safe_url}: {err_label}"
                            )
                            continue

                        if cancel_ev.is_set():
                            break
                        dwell = (
                            per_url_dwell if single_pass else min(per_url_dwell, remaining_dwell)
                        )
                        if not single_pass:
                            remaining_dwell -= dwell
                        if dwell > 0:
                            t1 = dwell * 0.25
                            t2 = dwell * 0.40
                            scroll_phases: list[tuple[float, dict[str, Any]]] = [
                                (
                                    t1,
                                    {
                                        "delta_y": random.randint(320, 620),
                                        "steps": random.randint(5, 9),
                                        "humanize": True,
                                    },
                                ),
                                (
                                    t2,
                                    {
                                        "delta_y": random.randint(360, 680),
                                        "steps": random.randint(6, 10),
                                        "humanize": True,
                                    },
                                ),
                            ]
                            if dwell >= 6.0:
                                scroll_phases.append(
                                    (
                                        0.0,
                                        {
                                            "delta_y": -random.randint(140, 260),
                                            "steps": random.randint(4, 7),
                                            "humanize": True,
                                        },
                                    )
                                )
                            waited = 0.0
                            for wait_before, scroll_params in scroll_phases:
                                if cancel_ev.is_set():
                                    break
                                if wait_before > 0:
                                    if cancel_ev.wait(wait_before):
                                        break
                                    waited += wait_before
                                if cancel_ev.is_set():
                                    break
                                try:
                                    if supports_page_pin:
                                        bl.control(
                                            prof_name,
                                            "scroll",
                                            scroll_params,
                                            timeout=8.0,
                                            require_current=True,
                                            target_page_id=pinned_page_id,
                                        )
                                    elif supports_pin:
                                        bl.control(
                                            prof_name,
                                            "scroll",
                                            scroll_params,
                                            timeout=8.0,
                                            require_current=True,
                                        )
                                    else:
                                        bl.control(prof_name, "scroll", scroll_params, timeout=8.0)
                                except Exception as scroll_err:
                                    if cancel_ev.is_set():
                                        break
                                    if self._is_unknown_state_error(scroll_err):
                                        log(
                                            f"[Warming] [{prof_name}] Scroll interrupted on {safe_url}: {self._safe_step_error(scroll_err)}"
                                        )
                                        url_interrupted = True
                                    break
                                if cancel_ev.is_set():
                                    break
                            if url_interrupted or cancel_ev.is_set():
                                break
                            remaining_url_wait = max(0.0, dwell - waited)
                            if remaining_url_wait > 0:
                                cancel_ev.wait(remaining_url_wait)
                    if (
                        single_pass
                        or url_interrupted
                        or not visited_in_pass
                        or remaining_dwell <= 0
                    ):
                        break

                if url_interrupted:
                    interrupted_count += 1
                elif visited_any:
                    succeeded += 1
            except Exception as e:
                log(f"[Warming] Error warming '{prof_name}': {self._safe_step_error(e)}")
            finally:
                if acquired_lease:
                    with self._lease_lock:
                        if (
                            acquired_script_lock
                            and self._profile_script_owners.get(prof_name) is cancel_ev
                        ):
                            self._profile_script_owners.pop(prof_name, None)
                        rem = self._profile_active_users.get(prof_name, 1) - 1
                        if rem > 0:
                            self._profile_active_users[prof_name] = rem
                            candidate_stop = False
                        else:
                            self._profile_active_users.pop(prof_name, None)
                            candidate_stop = prof_name in self._profile_launched_by_warming
                    if candidate_stop:
                        is_live = False
                        try:
                            is_live = bl.is_running(prof_name)
                        finally:
                            with self._lease_lock:
                                if self._profile_active_users.get(prof_name, 0) == 0:
                                    self._profile_launched_by_warming.discard(prof_name)
                                    if is_live:
                                        with contextlib.suppress(Exception):
                                            bl.stop_profile(prof_name)

        with self._lock:
            is_current_owner = self._cancel_flags.get(task_id) is cancel_ev
            if is_current_owner:
                self._cancel_flags.pop(task_id, None)
                task = self._tasks.get(task_id)
                if task:
                    task.last_run = time.time()
                    if cancel_ev.is_set():
                        task.status = "idle"
                        task.last_result = f"Stopped ({succeeded}/{len(targets)})"
                    elif succeeded > 0:
                        task.status = "completed"
                        issues = []
                        if interrupted_count:
                            issues.append(f"{interrupted_count} interrupted")
                        if timed_out_count:
                            issues.append(f"{timed_out_count} timed out")
                        if busy_count:
                            issues.append(f"{busy_count} busy")
                        suffix = f"; {', '.join(issues)}" if issues else ""
                        task.last_result = f"OK ({succeeded}/{len(targets)} profiles){suffix}"
                    else:
                        task.status = "failed"
                        if interrupted_count > 0:
                            task.last_result = f"Interrupted (0/{len(targets)} profiles)"
                        elif timed_out_count > 0:
                            task.last_result = f"Timed out (0/{len(targets)} profiles)"
                        elif busy_count > 0:
                            task.last_result = f"Busy (0/{len(targets)} profiles)"
                        elif invalid_urls:
                            task.last_result = "Invalid URLs (update task URLs)"
                        else:
                            task.last_result = f"Failed (0/{len(targets)} profiles)"
                    if not task.repeat_daily and task.schedule_time:
                        task.enabled = False
                    with self._store_lease():
                        self._save()

        log(f"[Warming] Finished '{task_name}': {succeeded}/{len(targets)} profile(s) warmed")
        if on_update:
            with contextlib.suppress(Exception):
                on_update()
        return succeeded > 0

    def run_task_async(
        self,
        task_id: str,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
        *,
        require_exists: bool = False,
        scheduled_slot: str | None = None,
    ) -> bool:
        with self._store_lease():
            started = self._begin_task(
                task_id, require_exists=require_exists, scheduled_slot=scheduled_slot
            )
            if started is None:
                return False
            task, cancel_ev, previous_state, run_lease = started
            try:
                threading.Thread(
                    target=self._own_run,
                    args=(run_lease, task, cancel_ev, pm, bl, log_fn, None, on_update),
                    daemon=True,
                ).start()
            except RuntimeError:
                current = self._tasks[task_id]
                current.status, current.last_triggered_slot = previous_state
                self._cancel_flags.pop(task_id, None)
                run_lease.close()
                self._save()
                raise
        return True

    def check_due_tasks(
        self,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
        now: datetime.datetime | None = None,
    ) -> list[str]:
        """Trigger any enabled scheduled task matching current HH:MM that has not run this minute."""
        dt = now or datetime.datetime.now()
        hhmm = dt.strftime("%H:%M")
        slot = dt.strftime("%Y-%m-%d %H:%M")
        triggered: list[str] = []
        with self._store_lease():
            latest = self._read_tasks()
            for task in self._tasks.values():
                saved = latest.get(task.id)
                if (
                    saved is None
                    or not task.enabled
                    or not saved.enabled
                    or task.schedule_time != hhmm
                    or saved.schedule_time != hhmm
                    or task.last_triggered_slot == slot
                    or saved.last_triggered_slot == slot
                ):
                    continue
                if self.run_task_async(
                    task.id, pm, bl, log_fn=log_fn, on_update=on_update, scheduled_slot=slot
                ):
                    triggered.append(task.id)
        return triggered

    def start_daemon(
        self,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            if log_fn is not None:
                self._daemon_log_fn = log_fn
            if on_update is not None:
                self._daemon_on_update = on_update
            if (
                self._daemon_thread
                and self._daemon_thread.is_alive()
                and not self._daemon_stop.is_set()
            ):
                return
            if self._daemon_stop.is_set():
                self._daemon_stop = threading.Event()
            stop_ev = self._daemon_stop

            def _loop() -> None:
                while not stop_ev.wait(15.0):
                    with contextlib.suppress(Exception):
                        self.check_due_tasks(
                            pm,
                            bl,
                            log_fn=self._daemon_log_fn,
                            on_update=self._daemon_on_update,
                        )

            self._daemon_thread = threading.Thread(target=_loop, daemon=True)
            self._daemon_thread.start()

    def stop_daemon(self) -> None:
        with self._lock:
            self._daemon_stop.set()
