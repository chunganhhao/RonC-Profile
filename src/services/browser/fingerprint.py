"""Per-profile fingerprint persistence & coherence-validated hardware customization.

Camoufox generates a fresh fingerprint on every launch unless one is passed
in. A profile that keeps its cookies but changes screen, GPU and fonts on each
launch looks like a spoofed browser, so the first launch generates a
fingerprint and every later launch reuses it from the profile's data dir.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
from typing import Any

from ...core.config import FINGERPRINT_FILE
from ...core.logging import get_logger

logger = get_logger("browser.fingerprint")

# Common desktop resolutions per OS that pass Camoufox's coherence checks
COMMON_SCREENS: dict[str, list[str]] = {
    "windows": ["auto", "1920x1080", "2560x1440", "1536x864", "1600x900", "1366x768"],
    "macos": ["auto", "1512x982", "1440x900", "1728x1117", "1920x1080", "2560x1440"],
    "linux": ["auto", "1920x1080", "2560x1440", "1600x900", "1366x768"],
}

_OS_KEY_MAP = {"windows": "win", "macos": "mac", "linux": "lin"}


def fingerprint_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, FINGERPRINT_FILE)


def available_gpus(os_type: str) -> list[tuple[str, str]]:
    """Return sorted, non-software, OS-coherent (vendor, renderer) GPU pairs for `os_type`."""
    try:
        from camoufox import coherence
        from camoufox.fingerprints import is_software_renderer
        from camoufox.webgl import _RFP_RENDERER, firefox_gpus

        target_os = _OS_KEY_MAP.get(os_type, "win")
        filtered = [
            (vendor, renderer)
            for vendor, renderer in firefox_gpus(os_type)
            if renderer
            and renderer != _RFP_RENDERER
            and not is_software_renderer(renderer)
            and coherence.gpu_fits_os(renderer, target_os)
        ]
        if os_type == "macos":
            return sorted(
                filtered,
                key=lambda pair: (0 if "Apple M" in pair[1] else 1, pair[0], pair[1]),
            )
        return sorted(filtered)
    except Exception:
        return []


def available_cores(os_type: str, gpu_renderer: str = "") -> list[int]:
    """Return valid CPU core counts for `os_type` and `gpu_renderer` per camoufox.coherence."""
    try:
        from camoufox import coherence
        from camoufox.fingerprints import PLAUSIBLE_CORE_COUNTS

        if os_type == "macos":
            if "Apple" in gpu_renderer or not gpu_renderer:
                return sorted(coherence.APPLE_SILICON_CORES)
            if "Intel" in gpu_renderer:
                return sorted(coherence.INTEL_MAC_IGP_CORES)
            return sorted(coherence.INTEL_MAC_DGPU_CORES)
        return list(PLAUSIBLE_CORE_COUNTS)
    except Exception:
        return [4, 6, 8, 10, 12, 16]


def generate_coherent_fingerprint(
    os_type: str,
    screen_spec: str = "auto",
    cpu_cores: int | None = None,
    gpu_pair: tuple[str, str] | None = None,
    base_fp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate or customize a fingerprint and validate it through Camoufox's coherence rules."""
    from camoufox import coherence
    from camoufox.fingerprints import (
        Screen,
        clamp_window_dimensions,
        clamp_window_position,
        fix_navigator_arch,
        follow_baseline_protection,
        from_fpgen,
        generate_fingerprint,
        raise_screen_to_modern_floor,
    )

    target_os = _OS_KEY_MAP.get(os_type, "win")

    if base_fp is not None:
        fp = copy.deepcopy(base_fp)
    else:
        screen_arg = None
        if screen_spec and screen_spec != "auto" and "x" in screen_spec:
            try:
                w_str, h_str = screen_spec.lower().split("x", 1)
                w, h = int(w_str), int(h_str)
                screen_arg = Screen(min_width=w, max_width=w, min_height=h, max_height=h)
            except ValueError:
                screen_arg = None
        fp = generate_fingerprint(os=os_type, screen=screen_arg)

    scr = fp.setdefault("screen", {})
    win = fp.setdefault("window", {})
    nav = fp.setdefault("navigator", {})
    webgl = fp.setdefault("webgl", {})

    # Apply screen override if specified and ensure window geometry is coherent
    if screen_spec and screen_spec != "auto" and "x" in screen_spec:
        try:
            w_str, h_str = screen_spec.lower().split("x", 1)
            w, h = int(w_str), int(h_str)
            scr["width"] = w
            scr["height"] = h
            scr["availWidth"] = w
            scr["availHeight"] = max(600, h - 48)
            win["outerWidth"] = min(w, win.get("outerWidth") or w)
            win["outerHeight"] = min(scr["availHeight"], max(600, h - 48))
            win["innerWidth"] = min(win["outerWidth"], win.get("innerWidth") or win["outerWidth"])
            win["innerHeight"] = max(480, min(win["outerHeight"], win["outerHeight"] - 86))
            fp["_custom_screen"] = True
        except ValueError:
            pass
    elif (not screen_spec or screen_spec == "auto") and fp.pop("_custom_screen", False):
        fresh = generate_fingerprint(os=os_type)
        scr.clear()
        scr.update(fresh.get("screen") or {})
        win.clear()
        win.update(fresh.get("window") or {})

    # Apply GPU pair if specified or make sure it fits the OS & machine
    valid_gpus = available_gpus(os_type)
    if gpu_pair and gpu_pair in valid_gpus:
        webgl["vendor"], webgl["renderer"] = gpu_pair
        fp["_custom_gpu"] = True
    elif valid_gpus and (webgl.get("vendor"), webgl.get("renderer")) not in valid_gpus:
        webgl["vendor"], webgl["renderer"] = valid_gpus[0]

    renderer = str(webgl.get("renderer") or "")
    w_cur = int(scr.get("width") or 1920)
    h_cur = int(scr.get("height") or 1080)

    # Resolve macOS notched Apple Silicon panel vs Intel/AMD GPU conflict
    if os_type == "macos" and (w_cur, h_cur) in coherence.APPLE_SILICON_PANELS and "Apple M" not in renderer:
        if gpu_pair and gpu_pair in valid_gpus:
            # User explicitly chose an Intel/AMD Mac GPU -> move screen off the notched Apple Silicon panel
            scr["width"], scr["height"] = 1440, 900
            scr["availWidth"], scr["availHeight"] = 1440, 875
            w_cur, h_cur = 1440, 900
        else:
            # Prefer an Apple M-series GPU for the notched panel
            apple_gpus = [g for g in valid_gpus if "Apple M" in g[1]]
            if apple_gpus:
                webgl["vendor"], webgl["renderer"] = apple_gpus[0]
                renderer = str(webgl["renderer"])

    # Apply CPU cores, ensuring coherence with OS and GPU (e.g. Apple Silicon >= 8 cores)
    valid_cores = available_cores(os_type, renderer)
    if cpu_cores and cpu_cores in valid_cores:
        nav["hardwareConcurrency"] = cpu_cores
        fp["_custom_cores"] = True
    elif valid_cores and nav.get("hardwareConcurrency") not in valid_cores:
        current = int(nav.get("hardwareConcurrency") or 8)
        nav["hardwareConcurrency"] = min(valid_cores, key=lambda c: abs(c - current))

    # Ensure GPU also passes gpu_fits_machine (e.g. discrete GPU on netbook screen)
    if valid_gpus and not coherence.gpu_fits_machine(
        renderer, target_os, nav.get("hardwareConcurrency"), scr.get("width"), scr.get("height")
    ):
        for cand_vendor, cand_renderer in valid_gpus:
            if coherence.gpu_fits_machine(
                cand_renderer, target_os, nav.get("hardwareConcurrency"), scr.get("width"), scr.get("height")
            ):
                webgl["vendor"], webgl["renderer"] = cand_vendor, cand_renderer
                renderer = cand_renderer
                break

    # Run Camoufox's whole-identity coherence repair on a temporary config projection
    try:
        cfg = from_fpgen(fp, "158")
        if webgl.get("vendor"):
            cfg["webGl:vendor"] = webgl["vendor"]
        if webgl.get("renderer"):
            cfg["webGl:renderer"] = webgl["renderer"]
        if nav.get("hardwareConcurrency"):
            cfg["navigator.hardwareConcurrency"] = int(nav["hardwareConcurrency"])

        fix_navigator_arch(cfg, target_os)
        if coherence.screen_is_implausible(cfg):
            coherence.repair_screen_orientation(cfg)
        raise_screen_to_modern_floor(cfg)
        follow_baseline_protection(cfg, target_os)
        clamp_window_dimensions(cfg)
        clamp_window_position(cfg)
        coherence.apply(cfg, target_os)

        if cfg.get("navigator.hardwareConcurrency"):
            nav["hardwareConcurrency"] = int(cfg["navigator.hardwareConcurrency"])
        if cfg.get("navigator.platform"):
            nav["platform"] = cfg["navigator.platform"]
        if cfg.get("navigator.oscpu"):
            nav["oscpu"] = cfg["navigator.oscpu"]
        if "navigator.maxTouchPoints" in cfg:
            nav["maxTouchPoints"] = int(cfg["navigator.maxTouchPoints"])
        if cfg.get("screen.width") and cfg.get("screen.height"):
            scr["width"] = int(cfg["screen.width"])
            scr["height"] = int(cfg["screen.height"])
        if cfg.get("screen.availWidth") and cfg.get("screen.availHeight"):
            scr["availWidth"] = int(cfg["screen.availWidth"])
            scr["availHeight"] = int(cfg["screen.availHeight"])
        if cfg.get("screen.colorDepth"):
            scr["colorDepth"] = int(cfg["screen.colorDepth"])
            scr["pixelDepth"] = int(cfg.get("screen.pixelDepth") or cfg["screen.colorDepth"])
        for win_key in ("outerWidth", "outerHeight", "innerWidth", "innerHeight", "screenX", "screenY"):
            cfg_key = f"window.{win_key}"
            if cfg.get(cfg_key) is not None:
                win[win_key] = int(cfg[cfg_key])
        if cfg.get("window.devicePixelRatio"):
            win["devicePixelRatio"] = cfg["window.devicePixelRatio"]
    except Exception as e:
        logger.debug("Coherence projection check skipped: %s", e)

    return fp


def save_fingerprint(profile_dir: str, os_type: str, fp: dict[str, Any]) -> None:
    """Persist a fingerprint dictionary to the profile's directory."""
    path = pathlib.Path(fingerprint_path(profile_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"os": os_type, "fingerprint": fp}),
        encoding="utf-8",
    )
    _summary_cache.pop(str(path), None)


def load_or_create(profile_dir: str, os_type: str) -> dict[str, Any]:
    """Return the profile's saved fingerprint, generating it on first use."""
    path = pathlib.Path(fingerprint_path(profile_dir))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("os") == os_type and isinstance(data.get("fingerprint"), dict):
                return data["fingerprint"]
            logger.info("Fingerprint OS changed for %s, regenerating", profile_dir)
        except (OSError, ValueError) as e:
            logger.warning("Unreadable fingerprint in %s (%s), regenerating", path, e)

    from camoufox.fingerprints import generate_fingerprint

    fingerprint = generate_fingerprint(os=os_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"os": os_type, "fingerprint": fingerprint}),
        encoding="utf-8",
    )
    logger.info("Generated fingerprint for %s (%s)", profile_dir, os_type)
    return fingerprint


# The Chrome engine's equivalent of fingerprint.json (chrome_engine/persona.py).
CHROME_PERSONA_FILE = "chrome_persona.json"
# The locale kept for the exit IP's country, both engines (chrome_engine/geo.py).
GEO_LOCALE_FILE = "geo_locale.json"
# Files that make up a profile's identity rather than its browser data.
IDENTITY_FILES = (FINGERPRINT_FILE, CHROME_PERSONA_FILE, GEO_LOCALE_FILE)
_NAVIGATOR_PLATFORM = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}


def reset(profile_dir: str) -> bool:
    """Delete the saved fingerprint so the next launch generates a new one."""
    removed = False
    for name in IDENTITY_FILES:
        path = pathlib.Path(profile_dir) / name
        if path.exists():
            path.unlink()
            removed = True
    return removed


_summary_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def summary(profile_dir: str) -> dict[str, Any] | None:
    """Return the headline values of the saved fingerprint, if any.

    Cached by file mtime: the UI asks for every visible card on each refresh
    and the file is a few hundred KB.
    """
    path = pathlib.Path(fingerprint_path(profile_dir))
    reader = _read_summary
    if not path.exists():
        path = pathlib.Path(profile_dir) / CHROME_PERSONA_FILE
        reader = _read_chrome_summary
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _summary_cache.pop(str(path), None)
        return None
    cached = _summary_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    result = reader(path)
    _summary_cache[str(path)] = (mtime, result)
    return result


def extract_details(fp: dict[str, Any], os_type: str = "windows") -> dict[str, Any]:
    """Extract rich hardware details from an in-memory Camoufox fingerprint dict."""
    navigator = fp.get("navigator", {}) or {}
    screen = fp.get("screen", {}) or {}
    window = fp.get("window", {}) or {}
    webgl = fp.get("webgl", {}) or {}
    audio_vals = (fp.get("audio", {}) or {}).get("values", {}) or {}
    fonts = fp.get("allFonts") or fp.get("baseFonts") or []
    w, h = screen.get("width"), screen.get("height")
    return {
        "os": os_type,
        "platform": navigator.get("platform") or _NAVIGATOR_PLATFORM.get(os_type, ""),
        "oscpu": navigator.get("oscpu") or "",
        "user_agent": navigator.get("userAgent") or "",
        "screen": f"{w}x{h}" if w and h else None,
        "color_depth": screen.get("colorDepth") or 24,
        "dpr": window.get("devicePixelRatio") or 1,
        "hardware_concurrency": navigator.get("hardwareConcurrency") or 8,
        "gpu_vendor": webgl.get("vendor") or "",
        "gpu_renderer": webgl.get("renderer") or "",
        "audio_sample_rate": audio_vals.get("AnalyserNode.context.sampleRate") or 44100,
        "fonts_count": len(fonts) if isinstance(fonts, list) else 0,
    }


def load_saved_fingerprint(profile_dir: str) -> tuple[str, dict[str, Any]] | None:
    """Return (os_type, fingerprint_dict) from fingerprint.json if present."""
    path = pathlib.Path(fingerprint_path(profile_dir))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("fingerprint"), dict):
            return str(data.get("os") or "windows"), data["fingerprint"]
    except (OSError, ValueError):
        pass
    return None


def _read_summary(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fp = data.get("fingerprint", {})
    navigator = fp.get("navigator", {}) or {}
    screen = fp.get("screen", {}) or {}
    # The stored userAgent carries the generator's Firefox version; Camoufox
    # rewrites it to the installed version at launch, so it is not reported.
    return {
        "os": data.get("os"),
        "platform": navigator.get("platform"),
        "screen": f"{screen.get('width')}x{screen.get('height')}" if screen.get("width") else None,
        "hardware_concurrency": navigator.get("hardwareConcurrency"),
    }


def _read_chrome_summary(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    platform = data.get("platform")
    return {
        "os": platform,
        "platform": _NAVIGATOR_PLATFORM.get(platform),
        # The Chrome engine sets the screen only when headless; headed it is the
        # real monitor, so there is no stored value to report.
        "screen": None,
        "hardware_concurrency": data.get("hardware_concurrency"),
    }
