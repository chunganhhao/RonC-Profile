"""A profile's persistent device identity for the Chrome engine.

fingerprint-chromium derives most of the device from one 32-bit seed (GPU,
canvas/audio noise, fonts, client rects); the persona adds what the seed does
not choose: OS, OS version, browser brand, CPU cores, timezone and language.
It is generated once and stored in the profile directory, so a profile shows
the same device on every launch, like Camoufox's fingerprint.json.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from random import Random
from typing import Any

PERSONA_FILE = "chrome_persona.json"
PLATFORMS = ("windows", "macos", "linux")
BRANDS = ("Chrome", "Edge")
SEED_MAX = 2**31 - 1
# Chromium's ApproximatedDeviceMemory implementation caps exposed deviceMemory at 8 GB.
# fingerprint-chromium 148's C++ patch (021-device-memory-cap.patch) indexes {8, 16, 32}
# by `fingerprint % 3`, so seeds divisible by 3 natively report 8 GB in C++.
# ponytail: temporary bypass pending upstream https://github.com/adryfish/fingerprint-chromium/issues/100
VALID_DEVICE_MEMORIES = (0.25, 0.5, 1, 2, 4, 8)

# Values Chrome reports in navigator.userAgentData platformVersion, weighted
# roughly by usage. Windows: 10.0.0 = Windows 10, 15.0.0 = 11 23H2, 19.0.0 = 11 24H2.
_PLATFORM_VERSIONS: dict[str, list[tuple[str, float]]] = {
    "windows": [("10.0.0", 0.35), ("15.0.0", 0.25), ("19.0.0", 0.40)],
    "macos": [("14.7.6", 0.25), ("15.6.1", 0.45), ("26.0.1", 0.30)],
    "linux": [("6.8.0", 0.5), ("6.11.0", 0.3), ("6.14.0", 0.2)],
}
_CORES: dict[str, list[tuple[int, float]]] = {
    "windows": [(4, 0.15), (8, 0.35), (12, 0.20), (16, 0.25), (20, 0.05)],
    "macos": [(8, 0.45), (10, 0.25), (12, 0.20), (14, 0.10)],
    "linux": [(4, 0.20), (8, 0.35), (12, 0.20), (16, 0.25)],
}

_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# How well a persona OS can be presented on a host OS. fingerprint-chromium
# spoofs navigator, client hints and the WebGL vendor/renderer everywhere, but
# fonts come from the host. A Linux persona keeps the host's real WebGL renderer:
# Direct3D 11 on Windows, the Apple GPU (e.g. "ANGLE Metal Renderer: Apple M1 Pro")
# on macOS. macOS overlay scrollbars are 0px wide, which a Windows persona cannot
# have. Measured with 148.0.7778.215 on Windows 11 and macOS 15 (Apple Silicon).
OK, WARN, UNSUPPORTED = "ok", "warn", "unsupported"
_COMPATIBILITY: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "macos"): (WARN, "the host's Windows fonts are visible to font probes"),
    ("windows", "linux"): (UNSUPPORTED, "WebGL still reports a Direct3D 11 renderer"),
    ("linux", "windows"): (WARN, "the host's Linux fonts are visible; install Windows fonts"),
    ("linux", "macos"): (WARN, "the host's Linux fonts are visible to font probes"),
    ("macos", "windows"): (
        WARN,
        "the host's macOS fonts are visible to font probes and overlay scrollbars are 0px wide",
    ),
    ("macos", "linux"): (UNSUPPORTED, "WebGL still reports the host's Apple GPU renderer"),
}


class PersonaError(ValueError):
    pass


@dataclass(frozen=True)
class Persona:
    seed: int
    platform: str
    platform_version: str
    brand: str = "Chrome"
    hardware_concurrency: int = 8
    # None: follow the proxy's exit IP (or the host when there is no proxy).
    timezone: str | None = None
    locale: str | None = None

    def __post_init__(self) -> None:
        validate(self)

    def with_geo(self, timezone: str | None, locale: str | None) -> Persona:
        """Fill in timezone / locale only where the persona leaves them open."""
        return replace(self, timezone=self.timezone or timezone, locale=self.locale or locale)

    @property
    def has_valid_device_memory_seed(self) -> bool:
        """True when C++ seed % 3 == 0 (reports 8 GB instead of non-standard 16/32 GB)."""
        return seed_gives_valid_device_memory(self.seed)

    def with_aligned_seed(self) -> Persona:
        """Return a copy whose seed is aligned to a multiple of 3 for W3C deviceMemory=8."""
        if self.has_valid_device_memory_seed:
            return self
        return replace(self, seed=align_seed_for_device_memory(self.seed))

    def to_args(self) -> list[str]:
        """fingerprint-chromium switches for this persona."""
        args = [
            f"--fingerprint={align_seed_for_device_memory(self.seed)}",
            f"--fingerprint-platform={self.platform}",
            f"--fingerprint-platform-version={self.platform_version}",
            f"--fingerprint-brand={self.brand}",
            f"--fingerprint-hardware-concurrency={self.hardware_concurrency}",
        ]
        if self.timezone:
            args.append(f"--timezone={self.timezone}")
        if self.locale:
            args.append(f"--lang={self.locale}")
            args.append(f"--accept-lang={accept_languages(self.locale)}")
        return args

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Persona:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def validate(p: Persona) -> None:
    if not isinstance(p.seed, int) or not 1 <= p.seed <= SEED_MAX:
        raise PersonaError(f"seed must be an integer in 1..{SEED_MAX}")
    if p.platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    if not _VERSION.match(p.platform_version or ""):
        raise PersonaError("platform_version must look like 15.0.0")
    if p.brand not in BRANDS:
        raise PersonaError(f"brand must be one of {', '.join(BRANDS)}")
    if p.brand == "Edge" and p.platform == "linux":
        raise PersonaError("Edge personas are Windows or macOS only")
    if not isinstance(p.hardware_concurrency, int) or not 1 <= p.hardware_concurrency <= 64:
        raise PersonaError("hardware_concurrency must be 1..64")
    if p.timezone is not None:
        _check_timezone(p.timezone)
    if p.locale is not None and not _LOCALE.match(p.locale):
        raise PersonaError("locale must look like en-US or vi-VN")


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise PersonaError(f"Unknown timezone {name!r}") from e


def accept_languages(locale: str) -> str:
    """The language list a browser set to `locale` typically sends: vi-VN,vi,en-US,en."""
    langs = [locale]
    base = locale.split("-")[0]
    if base != locale:
        langs.append(base)
    if base != "en":
        langs += ["en-US", "en"]
    return ",".join(langs)


def _pick(rng: Random, weighted: list[tuple[Any, float]]) -> Any:
    values, weights = zip(*weighted, strict=True)
    return rng.choices(values, weights=weights, k=1)[0]


def seed_gives_valid_device_memory(seed: int) -> bool:
    """True when `seed` makes fingerprint-chromium's C++ patch report 8 GB (seed % 3 == 0)."""
    return (
        isinstance(seed, int)
        and not isinstance(seed, bool)
        and 1 <= seed <= SEED_MAX
        and seed % 3 == 0
    )


def align_seed_for_device_memory(seed: int) -> int:
    """Map a 1..SEED_MAX seed to a multiple of 3 so C++ reports navigator.deviceMemory == 8."""
    # ponytail: temporary bypass pending upstream https://github.com/adryfish/fingerprint-chromium/issues/100
    clamped = max(1, min(int(seed), SEED_MAX))
    if clamped % 3 == 0:
        return clamped
    if clamped <= SEED_MAX // 3:
        return clamped * 3
    aligned = clamped - (clamped % 3)
    return aligned if aligned >= 3 else 3


def generate(
    platform: str,
    seed: int | None = None,
    brand: str = "Chrome",
    timezone: str | None = None,
    locale: str | None = None,
) -> Persona:
    """A new persona. The same seed and platform always give the same persona."""
    if platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    if seed is None:
        seed = align_seed_for_device_memory(1 + secrets.randbelow(SEED_MAX))
    rng = Random(f"{seed}:{platform}")
    return Persona(
        seed=seed,
        platform=platform,
        platform_version=_pick(rng, _PLATFORM_VERSIONS[platform]),
        brand=brand,
        hardware_concurrency=_pick(rng, _CORES[platform]),
        timezone=timezone,
        locale=locale,
    )


def persona_path(profile_dir: str | Path) -> Path:
    return Path(profile_dir) / PERSONA_FILE


def load(profile_dir: str | Path) -> Persona | None:
    try:
        return Persona.from_json(json.loads(persona_path(profile_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(profile_dir: str | Path, persona: Persona) -> None:
    path = persona_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(persona.to_json(), indent=2), encoding="utf-8")
    tmp.replace(path)


def load_or_create(profile_dir: str | Path, platform: str, **overrides: Any) -> Persona:
    """The profile's saved persona, generated on first use or when the OS changes.

    `overrides` (timezone, locale, brand, ...) are applied to the saved persona
    without regenerating it, so pinning a timezone keeps the same device. None
    leaves a field as saved; "" clears it (timezone / locale follow the IP again).
    """
    persona = load(profile_dir)
    if persona is None or persona.platform != platform:
        persona = generate(platform)
    elif not persona.has_valid_device_memory_seed:
        persona = persona.with_aligned_seed()
    overrides = {k: (v or None) for k, v in overrides.items() if v is not None}
    if overrides:
        persona = replace(persona, **overrides)
    save(profile_dir, persona)
    return persona


def compatibility(host: str, platform: str) -> tuple[str, str]:
    """(OK | WARN | UNSUPPORTED, reason) for presenting `platform` on a `host` OS."""
    if host == platform:
        return OK, ""
    return _COMPATIBILITY.get((host, platform), (WARN, "untested combination"))
