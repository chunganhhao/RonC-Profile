"""Per-profile fingerprint persistence & coherence-validated hardware customization.

Camoufox generates a fresh fingerprint on every launch unless one is passed
in. A profile that keeps its cookies but changes screen, GPU and fonts on each
launch looks like a spoofed browser, so the first launch generates a
fingerprint and every later launch reuses it from the profile's data dir.
"""

from __future__ import annotations

import contextlib
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
    if (
        os_type == "macos"
        and (w_cur, h_cur) in coherence.APPLE_SILICON_PANELS
        and "Apple M" not in renderer
    ):
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
                cand_renderer,
                target_os,
                nav.get("hardwareConcurrency"),
                scr.get("width"),
                scr.get("height"),
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
        for win_key in (
            "outerWidth",
            "outerHeight",
            "innerWidth",
            "innerHeight",
            "screenX",
            "screenY",
        ):
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


_RUNTIME_FF_VERSION = "158.0"


def _normalize_runtime_ua(raw_ua: str) -> str:
    """Align the preview User-Agent version with the Camoufox C++ runtime rewrite."""
    if not raw_ua:
        return ""
    import re

    ua = re.sub(r"\brv:\d+(?:\.\d+)?", f"rv:{_RUNTIME_FF_VERSION}", raw_ua)
    return re.sub(r"\bFirefox/\d+(?:\.\d+)?", f"Firefox/{_RUNTIME_FF_VERSION}", ua)


def extract_details(fp: dict[str, Any], os_type: str = "windows") -> dict[str, Any]:
    """Extract rich hardware details from an in-memory Camoufox fingerprint dict."""
    import hashlib

    navigator = fp.get("navigator", {}) or {}
    screen = fp.get("screen", {}) or {}
    window = fp.get("window", {}) or {}
    webgl = fp.get("webgl", {}) or {}
    audio_vals = (fp.get("audio", {}) or {}).get("values", {}) or {}
    fonts = fp.get("allFonts") or fp.get("baseFonts") or []
    w, h = screen.get("width"), screen.get("height")
    iw, ih = window.get("innerWidth"), window.get("innerHeight")
    sx, sy = window.get("screenX"), window.get("screenY")
    font_list = fonts if isinstance(fonts, list) else []
    sig_payload = json.dumps(
        [
            os_type,
            w,
            h,
            iw,
            ih,
            sx,
            sy,
            webgl.get("vendor"),
            webgl.get("renderer"),
            navigator.get("hardwareConcurrency"),
            audio_vals.get("AnalyserNode.context.sampleRate"),
            font_list[:48],
            len(font_list),
        ],
        sort_keys=True,
        default=str,
    )
    trait_hash = "#" + hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()[:8].upper()
    return {
        "os": os_type,
        "platform": navigator.get("platform") or _NAVIGATOR_PLATFORM.get(os_type, ""),
        "oscpu": navigator.get("oscpu") or "",
        "user_agent": _normalize_runtime_ua(navigator.get("userAgent") or ""),
        "screen": f"{w}x{h}" if w and h else None,
        "window_inner": f"{iw}x{ih}" if iw and ih else None,
        "color_depth": screen.get("colorDepth") or 24,
        "dpr": window.get("devicePixelRatio") or 1,
        "hardware_concurrency": navigator.get("hardwareConcurrency") or 8,
        "gpu_vendor": webgl.get("vendor") or "",
        "gpu_renderer": webgl.get("renderer") or "",
        "audio_sample_rate": audio_vals.get("AnalyserNode.context.sampleRate") or 44100,
        "fonts_count": len(font_list),
        "trait_hash": trait_hash,
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


def load_saved_chrome_persona(profile_dir: str) -> dict[str, Any] | None:
    """Return the saved chrome_persona.json dictionary if valid."""
    from chrome_engine import persona as chrome_persona

    p = chrome_persona.load(profile_dir)
    if p is None:
        return None
    return p.to_json()


_026_PERSONA_KEYS = (
    "device_model",
    "screen_width",
    "screen_height",
    "avail_top",
    "avail_height",
    "device_scale_factor",
    "color_depth",
    "color_gamut",
    "is_internal_display",
    "color_scheme",
)


def _has_026_fields(persona_data: dict[str, Any] | None) -> bool:
    return isinstance(persona_data, dict) and any(
        persona_data.get(key) is not None for key in _026_PERSONA_KEYS
    )


def _selected_chrome_supports_026(
    core_version: str | None = None,
    executable: str | pathlib.Path | None = None,
) -> bool:
    """True when the selected or installed Chromium bundle verifies for patch 026."""
    from chrome_engine import engine as engine_mod
    from chrome_engine import fetch

    from .cores import bound_executable

    exe: pathlib.Path | None = None
    if executable is not None:
        exe = pathlib.Path(executable)
    elif core_version:
        with contextlib.suppress(Exception):
            exe = bound_executable("chrome", core_version)
    else:
        env_exe = os.environ.get("FOXPROFILE_CHROME_EXECUTABLE")
        exe = pathlib.Path(env_exe) if env_exe else fetch.installed_executable()
    return bool(
        exe is not None
        and exe.is_file()
        and engine_mod._has_verified_apple_silicon_026(exe, "macos")
    )


def _resolve_use_026(
    target_os: str,
    *,
    coherence_026: bool | None = None,
    persona_data: dict[str, Any] | None = None,
    base_persona: dict[str, Any] | None = None,
    core_version: str | None = None,
    executable: str | pathlib.Path | None = None,
) -> bool:
    if target_os != "macos":
        return False
    if coherence_026 is not None:
        return bool(coherence_026)
    if base_persona is not None and not _has_026_fields(persona_data):
        return _has_026_fields(base_persona)
    return _selected_chrome_supports_026(core_version=core_version, executable=executable)


def generate_chrome_persona(
    os_type: str = "windows",
    *,
    cpu_cores: int | None = None,
    hardware_concurrency: int | None = None,
    seed: int | None = None,
    base_persona: dict[str, Any] | None = None,
    roll_new: bool = False,
    coherence_026: bool | None = None,
    core_version: str | None = None,
    executable: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """Generate or customize a coherent ChromeEngine Persona dictionary."""
    from dataclasses import replace as dc_replace

    from chrome_engine import persona as chrome_persona

    target_os = os_type if os_type in chrome_persona.PLATFORMS else "windows"
    aligned_seed = (
        chrome_persona.align_seed_for_device_memory(int(seed)) if seed is not None else None
    )
    candidate_base = (
        base_persona
        if (
            not roll_new
            and isinstance(base_persona, dict)
            and base_persona.get("platform") == target_os
        )
        else None
    )
    p = None
    if candidate_base is not None:
        with contextlib.suppress(Exception):
            loaded = chrome_persona.Persona.from_json(candidate_base).with_aligned_seed()
            if aligned_seed is None or loaded.seed == aligned_seed:
                p = loaded
    use_026 = _resolve_use_026(
        target_os,
        coherence_026=coherence_026,
        base_persona=p.to_json() if p is not None else None,
        core_version=core_version,
        executable=executable,
    )
    if (
        p is not None
        and target_os == "macos"
        and ((use_026 and p.device_model is None) or (not use_026 and p.requires_apple_silicon_026))
    ):
        p = None
    if p is None:
        p = chrome_persona.generate(target_os, seed=aligned_seed, coherence_026=use_026)
    cores = cpu_cores if cpu_cores is not None else hardware_concurrency
    if cores is not None and 1 <= int(cores) <= 64:
        with contextlib.suppress(Exception):
            p = dc_replace(p, hardware_concurrency=int(cores))
    return p.to_json()


def _build_chrome_persona(
    profile_dir: str,
    persona_data: dict[str, Any],
    *,
    os_type: str | None = None,
    coherence_026: bool | None = None,
    core_version: str | None = None,
    executable: str | pathlib.Path | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Validate and resolve the target ChromeEngine Persona before writing."""
    from dataclasses import fields as dc_fields
    from dataclasses import replace as dc_replace

    from chrome_engine import persona as chrome_persona

    allowed_keys = {f.name for f in dc_fields(chrome_persona.Persona)}
    if (
        not isinstance(persona_data, dict)
        or not persona_data
        or not (persona_data.keys() <= allowed_keys)
    ):
        raise chrome_persona.PersonaError("Invalid Chrome persona payload")

    data = dict(persona_data)
    supplied_platform = data.get("platform")
    if "platform" in data:
        if (
            not isinstance(supplied_platform, str)
            or supplied_platform not in chrome_persona.PLATFORMS
        ):
            raise chrome_persona.PersonaError(
                f"platform must be one of {', '.join(chrome_persona.PLATFORMS)}"
            )
        if os_type is not None and supplied_platform != os_type:
            raise chrome_persona.PersonaError(
                f"persona platform {supplied_platform!r} does not match profile os_type {os_type!r}"
            )

    has_cpu_key = "hardware_concurrency" in data
    reset_cpu = has_cpu_key and data["hardware_concurrency"] is None
    if has_cpu_key and not reset_cpu:
        raw_cpu = data["hardware_concurrency"]
        if not chrome_persona._is_int(raw_cpu) or not 1 <= raw_cpu <= 64:
            raise chrome_persona.PersonaError("hardware_concurrency must be 1..64")

    raw_seed = data.get("seed")
    if (
        "seed" in data
        and raw_seed is not None
        and (not chrome_persona._is_int(raw_seed) or not 1 <= raw_seed <= chrome_persona.SEED_MAX)
    ):
        raise chrome_persona.PersonaError(
            f"seed must be an integer in 1..{chrome_persona.SEED_MAX}"
        )

    existing = load_saved_chrome_persona(profile_dir)
    target_os = str(
        supplied_platform or os_type or (existing.get("platform") if existing else "windows")
    )
    if target_os not in chrome_persona.PLATFORMS:
        raise chrome_persona.PersonaError(
            f"platform must be one of {', '.join(chrome_persona.PLATFORMS)}"
        )
    data["platform"] = target_os

    aligned_raw_seed = (
        chrome_persona.align_seed_for_device_memory(int(raw_seed)) if raw_seed is not None else None
    )
    same_seed = (
        existing is not None
        and existing.get("platform") == target_os
        and (aligned_raw_seed is None or aligned_raw_seed == existing.get("seed"))
    )

    use_026 = _resolve_use_026(
        target_os,
        coherence_026=coherence_026,
        persona_data=data,
        base_persona=existing if same_seed else None,
        core_version=core_version,
        executable=executable,
    )
    if _has_026_fields(data) and (target_os != "macos" or not use_026):
        raise chrome_persona.PersonaError(
            "macOS Apple Silicon 026 persona fields require a verified 026+ Chromium core"
        )
    if ("screen_width" in data or "screen_height" in data) and (
        (data.get("screen_width") is None) != (data.get("screen_height") is None)
    ):
        raise chrome_persona.PersonaError(
            "screen_width and screen_height must both be set or both omitted"
        )

    has_seed = bool(
        aligned_raw_seed is not None or (same_seed and existing and existing.get("seed"))
    )
    has_version = bool(
        "platform_version" in data or (same_seed and existing and existing.get("platform_version"))
    )
    has_all_026 = all(
        data.get(key) is not None or (same_seed and existing and existing.get(key) is not None)
        for key in _026_PERSONA_KEYS
    )
    needs_generation = (
        not has_seed or not has_version or (target_os == "macos" and use_026 and not has_all_026)
    )

    if needs_generation:
        eff_seed = (
            aligned_raw_seed
            if aligned_raw_seed is not None
            else (existing.get("seed") if same_seed and existing else None)
        )
        effective = chrome_persona.generate(
            target_os, seed=eff_seed, coherence_026=use_026
        ).to_json()
        if same_seed and existing is not None:
            preserve_version = not use_026 or _has_026_fields(existing)
            for key in (
                "brand",
                "platform_version",
                "hardware_concurrency",
                "timezone",
                "locale",
            ):
                if key == "platform_version" and not preserve_version:
                    continue
                if existing.get(key) is not None:
                    effective[key] = existing[key]
            if use_026:
                for key in _026_PERSONA_KEYS:
                    if existing.get(key) is not None:
                        effective[key] = existing[key]
        if use_026 and isinstance(data.get("device_model"), str):
            matched = next(
                (
                    d
                    for d in chrome_persona.apple_silicon_slot_for_seed(
                        int(effective["seed"])
                    ).displays
                    if d.model == data["device_model"]
                ),
                None,
            )
            if matched is not None:
                dock = max(
                    0,
                    int(effective["screen_height"])
                    - int(effective["avail_top"] or 0)
                    - int(effective["avail_height"]),
                )
                effective.update(
                    {
                        "device_model": matched.model,
                        "screen_width": matched.width,
                        "screen_height": matched.height,
                        "avail_top": matched.avail_top,
                        "avail_height": matched.height - matched.avail_top - dock,
                        "device_scale_factor": matched.dpr,
                        "color_depth": matched.color_depth,
                        "color_gamut": matched.gamut,
                        "is_internal_display": matched.internal,
                    }
                )
    else:
        effective = dict(existing) if (same_seed and existing is not None) else {}
        if not use_026:
            for key in _026_PERSONA_KEYS:
                effective.pop(key, None)

    base_h = effective.get("screen_height")
    base_top = effective.get("avail_top")
    base_avail_h = effective.get("avail_height")

    effective["platform"] = target_os
    if aligned_raw_seed is not None:
        effective["seed"] = aligned_raw_seed
    for key, value in data.items():
        if key in ("platform", "seed"):
            continue
        if key == "hardware_concurrency":
            if not reset_cpu:
                effective[key] = value
            continue
        if key in _026_PERSONA_KEYS:
            if use_026:
                if value is None:
                    if same_seed and existing and _has_026_fields(existing):
                        raise chrome_persona.PersonaError(
                            f"{key} cannot be null on a 026 macOS persona"
                        )
                else:
                    effective[key] = value
            continue
        effective[key] = value

    if (
        use_026
        and data.get("screen_height") is not None
        and chrome_persona._is_int(data["screen_height"])
        and data.get("avail_height") is None
        and chrome_persona._is_int(base_h)
        and chrome_persona._is_int(base_avail_h)
    ):
        eff_top = effective.get("avail_top") or 0
        if chrome_persona._is_int(eff_top) and eff_top >= 0:
            base_dock = max(0, int(base_h) - int(base_top or 0) - int(base_avail_h))
            eff_h = int(data["screen_height"])
            effective["avail_height"] = min(eff_h - eff_top, max(240, eff_h - eff_top - base_dock))

    if reset_cpu and effective.get("seed") is not None:
        effective["hardware_concurrency"] = chrome_persona.default_cpu_for_persona(
            target_os,
            int(effective["seed"]),
            device_model=effective.get("device_model") if use_026 else None,
            coherence_026=use_026,
        )

    persona = chrome_persona.Persona.from_json(effective).with_aligned_seed()
    if target_os == "macos" and use_026:
        slot = chrome_persona.apple_silicon_slot_for_seed(persona.seed)
        if (
            slot.chip in ("Apple M4", "Apple M4 Pro", "Apple M4 Max")
            and int(persona.platform_version.split(".")[0]) < 15
        ):
            persona = dc_replace(persona, platform_version=slot.os_versions[0][0])
    return persona, existing


def _write_chrome_persona(
    profile_dir: str,
    persona: Any,
    existing: dict[str, Any] | None,
) -> str:
    from chrome_engine import persona as chrome_persona

    path = pathlib.Path(profile_dir) / CHROME_PERSONA_FILE
    if existing == persona.to_json():
        return str(path)
    chrome_persona.save(profile_dir, persona)
    _summary_cache.pop(str(path), None)
    return str(path)


def save_chrome_persona(
    profile_dir: str,
    persona_data: dict[str, Any],
    *,
    os_type: str | None = None,
    coherence_026: bool | None = None,
    core_version: str | None = None,
    executable: str | pathlib.Path | None = None,
) -> str:
    """Persist a ChromeEngine Persona dictionary to chrome_persona.json."""
    persona, existing = _build_chrome_persona(
        profile_dir,
        persona_data,
        os_type=os_type,
        coherence_026=coherence_026,
        core_version=core_version,
        executable=executable,
    )
    return _write_chrome_persona(profile_dir, persona, existing)


def extract_chrome_details(
    persona_data: dict[str, Any], os_type: str = "windows"
) -> dict[str, Any]:
    """Extract display details from an in-memory ChromeEngine Persona dict."""
    from chrome_engine.persona import align_seed_for_device_memory
    from chrome_engine.stealth import webgpu_profile_for

    target_os = str(persona_data.get("platform") or os_type or "windows")
    seed = align_seed_for_device_memory(int(persona_data.get("seed") or 4242))
    plat_ver = str(persona_data.get("platform_version") or "15.0.0")
    cores = int(persona_data.get("hardware_concurrency") or 8)
    brand = str(persona_data.get("brand") or "Chrome")
    gpu_info = webgpu_profile_for(target_os, seed)
    vendor_map = {
        "apple": "Apple Inc. (Metal 3)",
        "intel": "Intel Inc.",
        "nvidia": "NVIDIA Corporation",
        "amd": "AMD",
    }
    gpu_vendor = vendor_map.get(
        gpu_info.get("vendor", ""), str(gpu_info.get("vendor") or "Auto").upper()
    )
    device_model = persona_data.get("device_model")
    gpu_desc = f"{gpu_info.get('description') or 'C++ Seed GPU'} · Seed #{seed}"
    if device_model:
        gpu_desc = f"{device_model} · Seed #{seed}"
    w = persona_data.get("screen_width")
    h = persona_data.get("screen_height")
    dpr_val = persona_data.get("device_scale_factor")
    dpr = dpr_val if dpr_val is not None else (2 if target_os == "macos" else 1)
    screen_label = f"{w}x{h} @ {dpr:g}x" if (w and h) else "C++ Seed / Viewport"
    color_depth = int(persona_data.get("color_depth") or 24)
    ua_os_token = {
        "macos": "Macintosh; Intel Mac OS X 10_15_7",
        "windows": "Windows NT 10.0; Win64; x64",
        "linux": "X11; Linux x86_64",
    }.get(target_os, "Windows NT 10.0; Win64; x64")
    ua = f"Mozilla/5.0 ({ua_os_token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    return {
        "os": target_os,
        "platform": f"{_NAVIGATOR_PLATFORM.get(target_os, 'Win32')} · v{plat_ver}",
        "platform_version": plat_ver,
        "brand": brand,
        "device_model": device_model,
        "seed": seed,
        "user_agent": ua,
        "screen": screen_label,
        "window_inner": f"{int(w) - 4}x..." if w else "Maximized",
        "color_depth": color_depth,
        "dpr": dpr,
        "hardware_concurrency": cores,
        "device_memory": 8,
        "gpu_vendor": gpu_vendor,
        "gpu_renderer": gpu_desc,
        "audio_sample_rate": 44100,
        "fonts_count": "C++ Seed Noise",
        "trait_hash": f"#SEED-{seed}",
    }


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
    w = data.get("screen_width")
    h = data.get("screen_height")
    return {
        "os": platform,
        "platform": _NAVIGATOR_PLATFORM.get(platform),
        "screen": f"{w}x{h}" if (w and h) else None,
        "hardware_concurrency": data.get("hardware_concurrency"),
        "seed": data.get("seed"),
    }
