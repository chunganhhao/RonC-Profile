"""Desktop choices that survive a restart. The profile filter is deliberately not
one of them: every launch starts on All."""

import contextlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from ..core.logging import get_logger
from ..core.strings import STRINGS
from .state import PAGE_SIZE_OPTIONS
from .theme.colors import ACCENT_PRESETS

logger = get_logger(__name__)

Bounds = tuple[float, float, float, float]
_BOUNDS_KEYS = ("left", "top", "width", "height")
THEMES = ("light", "dark")


@dataclass(frozen=True)
class Preferences:
    language: str | None = None
    theme: str | None = None
    accent: str | None = None
    page_size: int | None = None
    window: Bounds | None = None


def _choice(value: object, options) -> str | None:
    return value if isinstance(value, str) and value in options else None


def _page_size(value: object) -> int | None:
    return value if type(value) is int and value in PAGE_SIZE_OPTIONS else None


def _bounds(value: object) -> Bounds | None:
    if not isinstance(value, dict):
        return None
    numbers = [value.get(key) for key in _BOUNDS_KEYS]
    if not all(
        isinstance(n, int | float) and not isinstance(n, bool) and math.isfinite(n) for n in numbers
    ):
        return None
    left, top, width, height = map(float, numbers)
    return (left, top, width, height) if width > 0 and height > 0 else None


def load(path: str | os.PathLike) -> Preferences:
    """Saved choices; anything missing, unreadable or out of range is left unset."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Preferences()
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable %s: %s", path, e)
        return Preferences()
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: expected a JSON object", path)
        return Preferences()
    return Preferences(
        language=_choice(data.get("language"), STRINGS),
        theme=_choice(data.get("theme"), THEMES),
        accent=_choice(data.get("accent"), ACCENT_PRESETS),
        page_size=_page_size(data.get("page_size")),
        window=_bounds(data.get("window")),
    )


def save(path: str | os.PathLike, prefs: Preferences) -> bool:
    """Write atomically, so a crash mid-write never leaves a truncated file."""
    path = Path(path)
    data: dict[str, object] = {
        "language": prefs.language,
        "theme": prefs.theme,
        "accent": prefs.accent,
        "page_size": prefs.page_size,
    }
    if prefs.window is not None:
        data["window"] = dict(zip(_BOUNDS_KEYS, prefs.window, strict=True))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Could not save preferences to %s: %s", path, e)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True
