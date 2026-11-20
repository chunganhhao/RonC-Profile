"""A profile's persistent device identity for the Chrome engine.

fingerprint-chromium derives most of the device from one 32-bit seed (GPU,
canvas/audio noise, fonts, client rects); the persona adds what the seed does
not choose: OS, OS version, browser brand, CPU cores, timezone and language.
It is generated once and stored in the profile directory, so a profile shows
the same device on every launch, like Camoufox's fingerprint.json.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
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
    "macos": [(8, 0.35), (10, 0.25), (11, 0.05), (12, 0.20), (14, 0.10), (16, 0.05)],
    "linux": [(4, 0.20), (8, 0.35), (12, 0.20), (16, 0.25)],
}

# Coherent Apple Silicon 13-slot catalog aligned 1:1 with Chromium
# kMacOSWebGLConfigs[0..12] (gcd(3, 13) == 1 so every aligned seed * 3 hits all slots).
_MAC_OS_SONOMA_UP = [
    ("14.7.6", 0.12),
    ("14.8.4", 0.10),
    ("15.5.0", 0.14),
    ("15.6.1", 0.24),
    ("15.7.4", 0.14),
    ("26.0.1", 0.12),
    ("26.2.0", 0.08),
    ("26.3.1", 0.06),
]
_MAC_OS_SEQUOIA_UP = [
    ("15.4.1", 0.12),
    ("15.5.0", 0.18),
    ("15.6.1", 0.28),
    ("15.7.4", 0.18),
    ("26.0.1", 0.10),
    ("26.2.0", 0.08),
    ("26.3.1", 0.06),
]


@dataclass(frozen=True)
class _MacDisplayArchetype:
    model: str
    width: int
    height: int
    avail_top: int
    dpr: float
    color_depth: int
    gamut: str
    internal: bool
    weight: float
    cores: tuple[tuple[int, float], ...] | None = None


@dataclass(frozen=True)
class AppleSiliconSlot:
    chip: str
    cores: list[tuple[int, float]]
    os_versions: list[tuple[str, float]]
    displays: list[_MacDisplayArchetype]


_APPLE_SILICON_SLOTS: tuple[AppleSiliconSlot, ...] = (
    # [0] Apple M4 (launched May 2024 on 14.5/iPadOS; Mac M4 Nov 2024 requires macOS 15.0+)
    AppleSiliconSlot(
        chip="Apple M4",
        cores=[(10, 0.85), (8, 0.15)],
        os_versions=_MAC_OS_SEQUOIA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 14 (M4)", 1512, 982, 38, 2.0, 30, "p3", True, 0.35, ((10, 1.0),)
            ),
            _MacDisplayArchetype(
                "Mac mini (M4) Studio Display",
                2560,
                1440,
                25,
                2.0,
                30,
                "p3",
                False,
                0.25,
                ((10, 1.0),),
            ),
            _MacDisplayArchetype(
                "Mac mini (M4) 1080p",
                1920,
                1080,
                25,
                1.0,
                24,
                "srgb",
                False,
                0.20,
                ((10, 1.0),),
            ),
            _MacDisplayArchetype(
                "iMac 24 (M4)",
                2240,
                1260,
                25,
                2.0,
                30,
                "p3",
                True,
                0.20,
                ((10, 0.65), (8, 0.35)),
            ),
        ],
    ),
    # [1] Apple M2
    AppleSiliconSlot(
        chip="Apple M2",
        cores=[(8, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype("MacBook Air 13 (M2)", 1470, 956, 37, 2.0, 30, "p3", True, 0.45),
            _MacDisplayArchetype("MacBook Air 15 (M2)", 1710, 1107, 37, 2.0, 30, "p3", True, 0.30),
            _MacDisplayArchetype(
                "Mac mini (M2) 1080p", 1920, 1080, 25, 1.0, 24, "srgb", False, 0.25
            ),
        ],
    ),
    # [2] Apple M1
    AppleSiliconSlot(
        chip="Apple M1",
        cores=[(8, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype("MacBook Air 13 (M1)", 1440, 900, 25, 2.0, 30, "p3", True, 0.50),
            _MacDisplayArchetype("MacBook Pro 13 (M1)", 1440, 900, 25, 2.0, 30, "p3", True, 0.25),
            _MacDisplayArchetype("iMac 24 (M1)", 2240, 1260, 25, 2.0, 30, "p3", True, 0.25),
        ],
    ),
    # [3] Apple M3
    AppleSiliconSlot(
        chip="Apple M3",
        cores=[(8, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype("MacBook Air 13 (M3)", 1470, 956, 37, 2.0, 30, "p3", True, 0.45),
            _MacDisplayArchetype("MacBook Air 15 (M3)", 1710, 1107, 37, 2.0, 30, "p3", True, 0.30),
            _MacDisplayArchetype("MacBook Pro 14 (M3)", 1512, 982, 38, 2.0, 30, "p3", True, 0.25),
        ],
    ),
    # [4] Apple M1 Pro
    AppleSiliconSlot(
        chip="Apple M1 Pro",
        cores=[(10, 0.70), (8, 0.30)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 14 (M1 Pro)",
                1512,
                982,
                38,
                2.0,
                30,
                "p3",
                True,
                0.60,
                ((10, 0.70), (8, 0.30)),
            ),
            _MacDisplayArchetype(
                "MacBook Pro 16 (M1 Pro)",
                1728,
                1117,
                38,
                2.0,
                30,
                "p3",
                True,
                0.40,
                ((10, 1.0),),
            ),
        ],
    ),
    # [5] Apple M2 Pro
    AppleSiliconSlot(
        chip="Apple M2 Pro",
        cores=[(12, 0.65), (10, 0.35)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 14 (M2 Pro)",
                1512,
                982,
                38,
                2.0,
                30,
                "p3",
                True,
                0.50,
                ((12, 0.60), (10, 0.40)),
            ),
            _MacDisplayArchetype(
                "MacBook Pro 16 (M2 Pro)",
                1728,
                1117,
                38,
                2.0,
                30,
                "p3",
                True,
                0.30,
                ((12, 1.0),),
            ),
            _MacDisplayArchetype(
                "Mac mini (M2 Pro) 1440p",
                2560,
                1440,
                25,
                2.0,
                30,
                "p3",
                False,
                0.20,
                ((12, 0.55), (10, 0.45)),
            ),
        ],
    ),
    # [6] Apple M3 Pro
    AppleSiliconSlot(
        chip="Apple M3 Pro",
        cores=[(12, 0.65), (11, 0.35)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 14 (M3 Pro)",
                1512,
                982,
                38,
                2.0,
                30,
                "p3",
                True,
                0.55,
                ((12, 0.60), (11, 0.40)),
            ),
            _MacDisplayArchetype(
                "MacBook Pro 16 (M3 Pro)",
                1728,
                1117,
                38,
                2.0,
                30,
                "p3",
                True,
                0.45,
                ((12, 1.0),),
            ),
        ],
    ),
    # [7] Apple M4 Pro
    AppleSiliconSlot(
        chip="Apple M4 Pro",
        cores=[(14, 0.70), (12, 0.30)],
        os_versions=_MAC_OS_SEQUOIA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 14 (M4 Pro)",
                1512,
                982,
                38,
                2.0,
                30,
                "p3",
                True,
                0.45,
                ((14, 0.60), (12, 0.40)),
            ),
            _MacDisplayArchetype(
                "MacBook Pro 16 (M4 Pro)",
                1728,
                1117,
                38,
                2.0,
                30,
                "p3",
                True,
                0.35,
                ((14, 1.0),),
            ),
            _MacDisplayArchetype(
                "Mac mini (M4 Pro) 1440p",
                2560,
                1440,
                25,
                2.0,
                30,
                "p3",
                False,
                0.20,
                ((14, 0.65), (12, 0.35)),
            ),
        ],
    ),
    # [8] Apple M1 Max
    AppleSiliconSlot(
        chip="Apple M1 Max",
        cores=[(10, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 16 (M1 Max)", 1728, 1117, 38, 2.0, 30, "p3", True, 0.50
            ),
            _MacDisplayArchetype(
                "MacBook Pro 14 (M1 Max)", 1512, 982, 38, 2.0, 30, "p3", True, 0.25
            ),
            _MacDisplayArchetype("Mac Studio (M1 Max)", 2560, 1440, 25, 2.0, 30, "p3", False, 0.25),
        ],
    ),
    # [9] Apple M2 Max
    AppleSiliconSlot(
        chip="Apple M2 Max",
        cores=[(12, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 16 (M2 Max)", 1728, 1117, 38, 2.0, 30, "p3", True, 0.50
            ),
            _MacDisplayArchetype(
                "MacBook Pro 14 (M2 Max)", 1512, 982, 38, 2.0, 30, "p3", True, 0.25
            ),
            _MacDisplayArchetype("Mac Studio (M2 Max)", 2560, 1440, 25, 2.0, 30, "p3", False, 0.25),
        ],
    ),
    # [10] Apple M3 Max
    AppleSiliconSlot(
        chip="Apple M3 Max",
        cores=[(16, 0.65), (14, 0.35)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 16 (M3 Max)", 1728, 1117, 38, 2.0, 30, "p3", True, 0.60
            ),
            _MacDisplayArchetype(
                "MacBook Pro 14 (M3 Max)", 1512, 982, 38, 2.0, 30, "p3", True, 0.40
            ),
        ],
    ),
    # [11] Apple M4 Max
    AppleSiliconSlot(
        chip="Apple M4 Max",
        cores=[(16, 0.70), (14, 0.30)],
        os_versions=_MAC_OS_SEQUOIA_UP,
        displays=[
            _MacDisplayArchetype(
                "MacBook Pro 16 (M4 Max)", 1728, 1117, 38, 2.0, 30, "p3", True, 0.50
            ),
            _MacDisplayArchetype(
                "MacBook Pro 14 (M4 Max)", 1512, 982, 38, 2.0, 30, "p3", True, 0.25
            ),
            _MacDisplayArchetype("Mac Studio (M4 Max)", 2560, 1440, 25, 2.0, 30, "p3", False, 0.25),
        ],
    ),
    # [12] Apple M2 (MacBook Air 15 / 13.6 / Mac mini alias matching kMacOSWebGLConfigs[12])
    AppleSiliconSlot(
        chip="Apple M2",
        cores=[(8, 1.0)],
        os_versions=_MAC_OS_SONOMA_UP,
        displays=[
            _MacDisplayArchetype("MacBook Air 15 (M2)", 1710, 1107, 37, 2.0, 30, "p3", True, 0.45),
            _MacDisplayArchetype("MacBook Air 13 (M2)", 1470, 956, 37, 2.0, 30, "p3", True, 0.35),
            _MacDisplayArchetype("Mac mini (M2) 1440p", 2560, 1440, 25, 2.0, 30, "p3", False, 0.20),
        ],
    ),
)


def apple_silicon_slot_for_seed(seed: int) -> AppleSiliconSlot:
    """Return the C++-matched Apple Silicon chip slot for an aligned or raw seed."""
    aligned = align_seed_for_device_memory(seed)
    return _APPLE_SILICON_SLOTS[aligned % len(_APPLE_SILICON_SLOTS)]


def allowed_macos_cores_for_model(
    slot: AppleSiliconSlot, device_model: str | None
) -> list[tuple[int, float]]:
    """Return weighted CPU core choices valid for `device_model` within `slot`."""
    if device_model:
        for display in slot.displays:
            if display.model == device_model and display.cores is not None:
                return list(display.cores)
    return list(slot.cores)


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
    # Optional coherent Mac/display profile attributes (filled by generate("macos")).
    device_model: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    avail_top: int | None = None
    avail_height: int | None = None
    device_scale_factor: float | None = None
    color_depth: int | None = None
    color_gamut: str | None = None
    is_internal_display: bool | None = None
    color_scheme: str | None = None

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

    @property
    def requires_apple_silicon_026(self) -> bool:
        """True when this macOS persona declares 026+ chip/display attributes."""
        return self.platform == "macos" and any(
            v is not None
            for v in (
                self.device_model,
                self.screen_width,
                self.screen_height,
                self.avail_top,
                self.avail_height,
                self.device_scale_factor,
                self.color_depth,
                self.color_gamut,
                self.is_internal_display,
                self.color_scheme,
            )
        )

    def to_args(self, *, include_display_profile: bool = True) -> list[str]:
        """fingerprint-chromium switches for this persona."""
        args = [
            f"--fingerprint={align_seed_for_device_memory(self.seed)}",
            f"--fingerprint-platform={self.platform}",
            f"--fingerprint-platform-version={self.platform_version}",
            f"--fingerprint-brand={self.brand}",
            f"--fingerprint-hardware-concurrency={self.hardware_concurrency}",
        ]
        if include_display_profile:
            if self.screen_width is not None and self.screen_height is not None:
                args.append(f"--fingerprint-screen-width={self.screen_width}")
                args.append(f"--fingerprint-screen-height={self.screen_height}")
            if self.device_scale_factor is not None:
                args.append(f"--fingerprint-device-scale-factor={self.device_scale_factor:g}")
            profile_parts: list[str] = []
            if self.avail_top is not None:
                profile_parts.append(f"avail_top={self.avail_top}")
            if self.avail_height is not None:
                profile_parts.append(f"avail_height={self.avail_height}")
            if self.color_depth is not None:
                profile_parts.append(f"color_depth={self.color_depth}")
            if self.color_gamut is not None:
                profile_parts.append(f"gamut={self.color_gamut}")
            if self.is_internal_display is not None:
                profile_parts.append(f"internal={1 if self.is_internal_display else 0}")
            if self.color_scheme is not None:
                profile_parts.append(f"scheme={self.color_scheme}")
            if profile_parts:
                args.append(f"--fingerprint-display-profile={','.join(profile_parts)}")
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


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(p: Persona) -> None:
    if not _is_int(p.seed) or not 1 <= p.seed <= SEED_MAX:
        raise PersonaError(f"seed must be an integer in 1..{SEED_MAX}")
    if not isinstance(p.platform, str) or p.platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    if not isinstance(p.platform_version, str) or not _VERSION.match(p.platform_version):
        raise PersonaError("platform_version must look like 15.0.0")
    if not isinstance(p.brand, str) or p.brand not in BRANDS:
        raise PersonaError(f"brand must be one of {', '.join(BRANDS)}")
    if p.brand == "Edge" and p.platform == "linux":
        raise PersonaError("Edge personas are Windows or macOS only")
    if not _is_int(p.hardware_concurrency) or not 1 <= p.hardware_concurrency <= 64:
        raise PersonaError("hardware_concurrency must be 1..64")
    if (p.screen_width is None) != (p.screen_height is None):
        raise PersonaError("screen_width and screen_height must both be set or both omitted")
    if p.screen_width is not None and (
        not _is_int(p.screen_width) or not 320 <= p.screen_width <= 8192
    ):
        raise PersonaError("screen_width must be in 320..8192")
    if p.screen_height is not None and (
        not _is_int(p.screen_height) or not 240 <= p.screen_height <= 8192
    ):
        raise PersonaError("screen_height must be in 240..8192")
    if p.avail_top is not None and (not _is_int(p.avail_top) or p.avail_top < 0):
        raise PersonaError("avail_top must be a non-negative integer")
    if p.avail_height is not None and (not _is_int(p.avail_height) or p.avail_height < 240):
        raise PersonaError("avail_height must be >= 240")
    if p.screen_height is not None and p.avail_height is not None:
        top = p.avail_top or 0
        if top + p.avail_height > p.screen_height:
            raise PersonaError("avail_top + avail_height must not exceed screen_height")
    if p.device_scale_factor is not None and (
        isinstance(p.device_scale_factor, bool)
        or not isinstance(p.device_scale_factor, (int, float))
        or not 0.5 <= float(p.device_scale_factor) <= 4.0
    ):
        raise PersonaError("device_scale_factor must be in 0.5..4.0")
    if p.color_depth is not None and (not _is_int(p.color_depth) or p.color_depth not in (24, 30)):
        raise PersonaError("color_depth must be 24 or 30")
    if p.color_gamut is not None and p.color_gamut not in ("srgb", "p3"):
        raise PersonaError("color_gamut must be 'srgb' or 'p3'")
    if p.is_internal_display is not None and not isinstance(p.is_internal_display, bool):
        raise PersonaError("is_internal_display must be a bool")
    if p.color_scheme is not None and p.color_scheme not in ("light", "dark"):
        raise PersonaError("color_scheme must be 'light' or 'dark'")
    if p.device_model is not None and (
        not isinstance(p.device_model, str) or not p.device_model.strip()
    ):
        raise PersonaError("device_model must be a non-empty string")
    if p.timezone is not None:
        _check_timezone(p.timezone)
    if p.locale is not None and (not isinstance(p.locale, str) or not _LOCALE.match(p.locale)):
        raise PersonaError("locale must look like en-US or vi-VN")


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(name, str) or not name.strip():
        raise PersonaError(f"Unknown timezone {name!r}")
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
    *,
    coherence_026: bool = True,
) -> Persona:
    """A new persona. The same seed and platform always give the same persona."""
    if platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    if seed is None:
        seed = align_seed_for_device_memory(1 + secrets.randbelow(SEED_MAX))
    rng = Random(f"{seed}:{platform}")
    if platform == "macos" and coherence_026:
        slot = apple_silicon_slot_for_seed(seed)
        display = rng.choices(slot.displays, weights=[d.weight for d in slot.displays], k=1)[0]
        dock_height = rng.choice((0, 64, 68, 72, 76))
        avail_height = display.height - display.avail_top - dock_height
        color_scheme = rng.choices(("dark", "light"), weights=(0.65, 0.35), k=1)[0]
        return Persona(
            seed=seed,
            platform=platform,
            platform_version=_pick(rng, slot.os_versions),
            brand=brand,
            hardware_concurrency=_pick(rng, allowed_macos_cores_for_model(slot, display.model)),
            timezone=timezone,
            locale=locale,
            device_model=display.model,
            screen_width=display.width,
            screen_height=display.height,
            avail_top=display.avail_top,
            avail_height=avail_height,
            device_scale_factor=display.dpr,
            color_depth=display.color_depth,
            color_gamut=display.gamut,
            is_internal_display=display.internal,
            color_scheme=color_scheme,
        )
    return Persona(
        seed=seed,
        platform=platform,
        platform_version=_pick(rng, _PLATFORM_VERSIONS[platform]),
        brand=brand,
        hardware_concurrency=_pick(rng, _CORES[platform]),
        timezone=timezone,
        locale=locale,
    )


def default_cpu_for_persona(
    platform: str,
    seed: int,
    device_model: str | None = None,
    *,
    coherence_026: bool = True,
) -> int:
    """Deterministic Auto CPU count matching `(platform, seed, device_model)`."""
    generated = generate(platform, seed=seed, coherence_026=coherence_026)
    if (
        platform != "macos"
        or not coherence_026
        or not device_model
        or device_model == generated.device_model
    ):
        return generated.hardware_concurrency
    slot = apple_silicon_slot_for_seed(generated.seed)
    return _pick(
        Random(f"{generated.seed}:{platform}:cpu:{device_model}"),
        allowed_macos_cores_for_model(slot, device_model),
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
    payload = json.dumps(persona.to_json(), indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".chrome_persona_", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def load_or_create(
    profile_dir: str | Path,
    platform: str,
    *,
    coherence_026: bool = True,
    **overrides: Any,
) -> Persona:
    """The profile's saved persona, generated on first use or when the OS changes.

    `overrides` (timezone, locale, brand, ...) are applied to the saved persona
    without regenerating it, so pinning a timezone keeps the same device. None
    leaves a field as saved; "" clears it (timezone / locale follow the IP again).
    """
    loaded = load(profile_dir)
    if loaded is None or loaded.platform != platform:
        persona = generate(platform, coherence_026=coherence_026)
    elif not loaded.has_valid_device_memory_seed:
        persona = loaded.with_aligned_seed()
    else:
        persona = loaded
    overrides = {k: (v or None) for k, v in overrides.items() if v is not None}
    if overrides:
        persona = replace(persona, **overrides)
    if loaded is None or persona != loaded:
        save(profile_dir, persona)
    return persona


def compatibility(host: str, platform: str) -> tuple[str, str]:
    """(OK | WARN | UNSUPPORTED, reason) for presenting `platform` on a `host` OS."""
    if host == platform:
        return OK, ""
    return _COMPATIBILITY.get((host, platform), (WARN, "untested combination"))
