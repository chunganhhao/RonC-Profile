"""Launch a fingerprint-chromium profile as a Playwright BrowserContext.

    async with ChromeEngine("data/alice", proxy="socks5://u:p@host:1080") as context:
        page = context.pages[0]
        await page.goto("https://example.com")

What the engine adds on top of a bare `launch_persistent_context`:

- a persistent persona (seed, OS, brand, cores) per profile directory;
- proxies with credentials, through a loopback bridge (proxy_bridge.py);
- timezone / locale from the proxy's exit IP unless the persona pins them,
  with the locale kept per profile while the exit IP stays in one country;
- Playwright's automation switches removed where a page can observe them;
- native browser downloads restored to the profile/OS Downloads folder instead of Playwright's temporary artifacts directory;
- a refusal to present an OS the host cannot fake consistently.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.browser.timing import launch_stage

from . import fetch
from . import persona as personas
from .geo import ExitGeo, lookup, remembered_locale
from .native_bundle import bundle_sha256
from .proxy_bridge import ProxyBridge, Upstream
from .release import host_os
from .stealth import build_anti_cdp_init_script

# Playwright defaults a page can observe, or that make the browser behave
# unlike a person's Chrome. Everything else Playwright passes is kept, except
# its --disable-features list (see playwright_disabled_features_arg).
IGNORED_DEFAULT_ARGS = (
    "--enable-automation",
    "--disable-blink-features=AutomationControlled",
    "--disable-popup-blocking",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-back-forward-cache",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
    "--disable-sync",
    "--no-sandbox",
    "--enable-unsafe-swiftshader",
)
HEADLESS_SCREEN = (1920, 1080)

# Native realm capability only, not release/anonymity approval. Pin the complete
# Run #12, Run #14, Run #15, Run #17, Run #19, Run #21, and Run 45260495371 (029) payloads so existing profiles keep manifest parity.
NATIVE_REALM_BUNDLES = frozenset(
    {
        "800c76d9926cf68e1f6ad5c4e9f226a3f5cdba1a096c66fba38f5ec0761069b7",
        "8c77f73214266f9774b0e1c51a1c763b3714539d6e9e8c69b4140580304725e0",
        "0d7f8e262baa074bf7ded9cd8409995281ff29c5a2ae50804258e75a8d064f8b",
        "539e83e54a45d6607ca048f4d1b64591ead7af60a8be991542dc9eff4d276f6d",
        "2d74250932a67ea51341005ef78db8434e16376c2bd4e267eb2d29e11ddae8a7",
        "f3b0f3218788c444c0ce9665cbc438cce904a763211542b6eac3f534bac34862",
        "c3944e94fff0473c494c866b2295390c7521f114435b5a30cfd4a506f7fa469a",
    }
)
# Verified Chromium bundles containing 026 Apple Silicon coherence + WebGL extension gating.
APPLE_SILICON_COHERENCE_BUNDLES: frozenset[str] = frozenset(
    {
        "f3b0f3218788c444c0ce9665cbc438cce904a763211542b6eac3f534bac34862",
        "c3944e94fff0473c494c866b2295390c7521f114435b5a30cfd4a506f7fa469a",
    }
)
# Verified Chromium bundles containing 027 native locale, speech voices, and font-access filtering.
# shortcut: Run 45260495371 (c3944e94...) pinned under IPv4-only proxy egress + local dual-stack STUN scope; verify routable WAN IPv6 proxy egress before dual-stack WAN promotion.
LOCALE_SPEECH_FONT_027_BUNDLES: frozenset[str] = frozenset(
    {
        "c3944e94fff0473c494c866b2295390c7521f114435b5a30cfd4a506f7fa469a",
    }
)

_FEATURES_BLOCK = re.compile(r"disabledFeatures = \[(.*?)\]\.filter\(Boolean\)", re.S)


@functools.lru_cache(maxsize=1)
def playwright_disabled_features_arg() -> str | None:
    """Playwright's own --disable-features switch, exactly as it passes it.

    It turns off third-party storage partitioning, HTTPS upgrades, paint
    holding and more, and ignore_default_args only drops exact matches, so the
    string is read from the installed Playwright driver. None when it cannot be
    reproduced; test_browser.py::test_automation_switches_are_gone notices.
    """
    try:
        from playwright._impl._driver import compute_driver_executable

        driver = compute_driver_executable()
        cli = Path(driver[-1] if isinstance(driver, tuple) else driver)
        lib = cli.parent / "lib"
        for path in (lib / "coreBundle.js", lib / "server" / "chromium" / "chromiumSwitches.js"):
            if not path.is_file():
                continue
            match = _FEATURES_BLOCK.search(path.read_text(encoding="utf-8", errors="replace"))
            if not match:
                continue
            body = re.sub(r"//[^\n]*", "", match.group(1))
            if "?" in body:  # an entry depends on the environment
                return None
            return "--disable-features=" + ",".join(re.findall(r'"([^"]+)"', body))
    except Exception:
        return None
    return None


# fingerprint-chromium ignores Chrome's --force-webrtc-ip-handling-policy and it
# does not hide LAN addresses behind mDNS names, so the policy is set through
# the profile's preferences on every launch:
# - behind a proxy, no UDP at all: no candidate can reveal the real IP;
# - without one, only the public address (the one sites already see) and never
#   a LAN address. Fully blocked WebRTC is itself unusual.
WEBRTC_POLICY_PROXY = "disable_non_proxied_udp"
WEBRTC_POLICY_DIRECT = "default_public_interface_only"


class EngineError(RuntimeError):
    pass


class EngineNotInstalled(EngineError):
    pass


def _preserve_corrupt_preferences(path: Path, raw: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    for idx in range(1000):
        suffix = f".{idx}" if idx else ""
        backup = path.with_name(f"{path.name}.corrupt.{stamp}{suffix}.bak")
        try:
            fd = os.open(backup, flags, 0o600)
        except FileExistsError:
            continue
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)
            raise
        return backup
    raise EngineError(f"Could not allocate unique backup name for corrupt Preferences: {path}")


def _load_preferences(path: Path, *, recover_corrupt: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        if os.path.lexists(path):
            raise EngineError(f"Profile Preferences is an unreadable symlink: {path}") from exc
        return {}
    except OSError as exc:
        raise EngineError(f"Could not read profile Preferences ({path}): {exc}") from exc

    try:
        prefs = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        if not recover_corrupt:
            raise EngineError(
                f"Profile Preferences is corrupt ({path}); "
                "refusing to overwrite without explicit recovery"
            ) from exc
        _preserve_corrupt_preferences(path, raw)
        return {}

    if not isinstance(prefs, dict):
        if not recover_corrupt:
            raise EngineError(
                f"Profile Preferences root must be a JSON object ({path}); "
                "refusing to overwrite without explicit recovery"
            )
        _preserve_corrupt_preferences(path, raw)
        return {}

    return prefs


def _write_preferences(path: Path, prefs: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="Preferences.foxprofile-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(json.dumps(prefs))
            tmp.flush()
            os.fsync(tmp.fileno())
        tmp_path.replace(path)
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        raise


def set_webrtc_policy(profile_dir: Path, policy: str, *, recover_corrupt: bool = False) -> None:
    """Write webrtc.ip_handling_policy into the profile's Default/Preferences."""
    path = profile_dir / "Default" / "Preferences"
    prefs = _load_preferences(path, recover_corrupt=recover_corrupt)
    webrtc = prefs.get("webrtc")
    if not isinstance(webrtc, dict):
        webrtc = prefs["webrtc"] = {}
    if webrtc.get("ip_handling_policy") == policy:
        return
    webrtc["ip_handling_policy"] = policy
    _write_preferences(path, prefs)


def set_extension_pins(
    profile_dir: Path,
    pinned_ids: tuple[str, ...] | list[str] = (),
    unpinned_ids: tuple[str, ...] | list[str] = (),
    *,
    recover_corrupt: bool = False,
) -> None:
    """Apply toolbar pin/unpin rules in Default/Preferences without clobbering custom pins."""
    if not pinned_ids and not unpinned_ids:
        return
    path = profile_dir / "Default" / "Preferences"
    prefs = _load_preferences(path, recover_corrupt=recover_corrupt)
    ext_cfg = prefs.get("extensions")
    if not isinstance(ext_cfg, dict):
        ext_cfg = prefs["extensions"] = {}
    current_raw = ext_cfg.get("pinned_extensions")
    current: list[str] = (
        [str(x) for x in current_raw if isinstance(x, str)] if isinstance(current_raw, list) else []
    )
    unpin_set = {str(x) for x in unpinned_ids if x}
    updated = [ext_id for ext_id in current if ext_id not in unpin_set]
    for ext_id in pinned_ids:
        if ext_id and ext_id not in unpin_set and ext_id not in updated:
            updated.append(str(ext_id))
    if current_raw == updated:
        return
    ext_cfg["pinned_extensions"] = updated
    _write_preferences(path, prefs)


def set_default_download_preferences(profile_dir: Path, *, recover_corrupt: bool = False) -> None:
    """Seed download.prompt_for_download=False only when unset so native downloads save automatically."""
    path = profile_dir / "Default" / "Preferences"
    prefs = _load_preferences(path, recover_corrupt=recover_corrupt)
    download = prefs.get("download")
    if isinstance(download, dict) and isinstance(download.get("prompt_for_download"), bool):
        return
    if not isinstance(download, dict):
        download = prefs["download"] = {}
    download["prompt_for_download"] = False
    _write_preferences(path, prefs)


def default_sandbox() -> bool:
    # Chromium refuses to run sandboxed as root on Linux.
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        return False
    return True


def _read_verified_bundle_manifest(
    executable: Path,
    target_platform: str | None,
    *,
    require_bundle_field: bool = False,
    cache: dict[tuple[Path, str | None], tuple[dict[str, Any], str] | None] | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Return `(manifest, actual_bundle_sha256)` when `executable` matches its bundle manifest."""
    if target_platform != "macos":
        return None
    cache_key = (executable, target_platform)
    if cache is not None and cache_key in cache:
        cached = cache[cache_key]
    else:
        cached = None
        try:
            resolved = executable.resolve()
            if len(resolved.parents) > 3 and resolved.parts[-4:-1] == (
                "Chromium.app",
                "Contents",
                "MacOS",
            ):
                manifest_path = resolved.parents[3] / "native-acceptance.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    isinstance(manifest, dict)
                    and manifest.get("acceptance") == "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL"
                    and isinstance(manifest.get("cases"), dict)
                    and all(
                        isinstance(manifest["cases"].get(k), dict) for k in ("macos:2", "macos:3")
                    )
                ):
                    expected_sha = manifest.get("binary_sha256")
                    if isinstance(expected_sha, str) and len(expected_sha) == 64:
                        actual_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
                        if actual_sha == expected_sha.lower():
                            actual_bundle = bundle_sha256(resolved.parents[2])
                            declared_bundle = manifest.get("bundle_sha256", actual_bundle)
                            if (
                                isinstance(declared_bundle, str)
                                and declared_bundle == actual_bundle
                            ):
                                cached = (manifest, actual_bundle)
        except (OSError, ValueError, TypeError, RuntimeError):
            cached = None
        if cache is not None:
            cache[cache_key] = cached
    if cached is None:
        return None
    if require_bundle_field and not isinstance(cached[0].get("bundle_sha256"), str):
        return None
    return cached


def _has_verified_native_realms(
    executable: Path,
    persona: personas.Persona,
    *,
    cache: dict[tuple[Path, str | None], tuple[dict[str, Any], str] | None] | None = None,
) -> bool:
    """True when `executable` sits inside a verified native-realms Chromium.app bundle."""
    verified = _read_verified_bundle_manifest(
        executable, getattr(persona, "platform", None), cache=cache
    )
    return verified is not None and verified[1] in NATIVE_REALM_BUNDLES


def _has_verified_apple_silicon_026(
    executable: Path,
    target: personas.Persona | str,
    *,
    cache: dict[tuple[Path, str | None], tuple[dict[str, Any], str] | None] | None = None,
) -> bool:
    """True when `executable` sits inside a pinned Chromium.app bundle verified for 026."""
    platform_name = target if isinstance(target, str) else getattr(target, "platform", None)
    verified = _read_verified_bundle_manifest(
        executable, platform_name, require_bundle_field=True, cache=cache
    )
    if verified is None:
        return False
    manifest, actual_bundle = verified
    cases = manifest["cases"]
    return actual_bundle in APPLE_SILICON_COHERENCE_BUNDLES and all(
        isinstance(cases.get(k), dict)
        for k in ("apple_silicon_m3_laptop", "apple_silicon_m2_max_desktop")
    )


_PIXELSCAN_RED_BOX_DIGEST = "bf9da7959d914298f9ce9e41a480fd66f76fac5c6f5e0a9b5a99b18cfc6fd997"
_EXPECTED_027_LANGS = ["de-DE", "de", "en-US", "en"]
_EXPECTED_027_REALM = {
    "dtf": "de-DE",
    "nf": "de-DE",
    "num": "123.456,789",
    "langs": _EXPECTED_027_LANGS,
}
_EXPECTED_027_POPUP_FIRST = {
    "lang": "de-DE",
    "langs": _EXPECTED_027_LANGS,
    "dtf": "de-DE",
    "nf": "de-DE",
    "num": "123.456,789",
}


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_zero_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _has_verified_locale_speech_font_027(
    executable: Path,
    target: personas.Persona | str,
    *,
    cache: dict[tuple[Path, str | None], tuple[dict[str, Any], str] | None] | None = None,
) -> bool:
    """True when `executable` sits inside a pinned bundle with verified 027+028 acceptance."""
    platform_name = target if isinstance(target, str) else getattr(target, "platform", None)
    verified = _read_verified_bundle_manifest(
        executable, platform_name, require_bundle_field=True, cache=cache
    )
    if verified is None:
        return False
    manifest, actual_bundle = verified
    if actual_bundle not in LOCALE_SPEECH_FONT_027_BUNDLES:
        return False
    cases = manifest["cases"]
    for seed_key in ("macos:2", "macos:3"):
        seed_case = cases.get(seed_key)
        if (
            not isinstance(seed_case, dict)
            or not _is_zero_int(seed_case.get("flatMonochromeChanged"))
            or seed_case.get("webglRedBoxDigest") != _PIXELSCAN_RED_BOX_DIGEST
        ):
            return False
    locale_case = cases.get("locale_speech_font_de")
    if not isinstance(locale_case, dict):
        return False
    if "host_local_voice_count" not in locale_case:
        return False
    host_local_voice_count = locale_case["host_local_voice_count"]
    if host_local_voice_count is not None and not _is_positive_int(host_local_voice_count):
        return False
    if (
        locale_case.get("speech_voice_gate") != "enforced"
        or not _is_zero_int(locale_case.get("google_voice_count"))
        or not _is_positive_int(locale_case.get("local_voice_count"))
        or not _is_positive_int(locale_case.get("local_voice_count_3006"))
        or not _is_zero_int(locale_case.get("core_font_missing_count"))
        or not _is_positive_int(locale_case.get("installed_protected_font_count"))
        or not _is_positive_int(locale_case.get("seed_3003_font_count"))
        or not _is_positive_int(locale_case.get("seed_3006_font_count"))
        or not _is_positive_int(locale_case.get("hidden_case_checked"))
    ):
        return False
    for realm in ("window", "worker", "shared", "service"):
        row = locale_case.get(realm)
        if not isinstance(row, dict) or any(
            row.get(key) != expected for key, expected in _EXPECTED_027_REALM.items()
        ):
            return False
    for popup_key, require_same_origin_opener, require_opener_closed in (
        ("noopener_popup", False, False),
        ("same_origin_cross_nav_popup", True, False),
        ("same_origin_close_popup", True, True),
    ):
        popup = locale_case.get(popup_key)
        if not isinstance(popup, dict) or popup.get("seen") != ["de-DE"]:
            return False
        first = popup.get("first")
        if not isinstance(first, dict) or any(
            first.get(key) != expected for key, expected in _EXPECTED_027_POPUP_FIRST.items()
        ):
            return False
        if require_same_origin_opener and popup.get("hadSameOriginOpener") is not True:
            return False
        if require_opener_closed and popup.get("openerClosed") is not True:
            return False
    return True


def build_args(
    persona: personas.Persona,
    proxy_url: str | None = None,
    headless: bool | str = False,
    extra_args: tuple[str, ...] | list[str] = (),
    extensions: tuple[str | Path, ...] | list[str | Path] = (),
    *,
    include_display_profile: bool = True,
) -> list[str]:
    """Command-line switches for a launch (Playwright adds its own before these).

    Note: `--enable-automation` is already stripped in `IGNORED_DEFAULT_ARGS`, so
    Blink's `AutomationControlled` feature is never activated (`navigator.webdriver`
    remains `false`). Do NOT pass `--disable-blink-features=AutomationControlled`,
    as Chromium's `bad_flags_prompt.cc` shows an unsupported-flag infobar that
    shrinks `window.innerHeight` and exposes the flag in `chrome://version`.
    """
    args = persona.to_args(include_display_profile=include_display_profile)
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")
    args += [
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
    ]
    use_preset_screen = bool(
        include_display_profile and persona.screen_width and persona.screen_height
    )
    screen_w = (persona.screen_width if use_preset_screen else None) or HEADLESS_SCREEN[0]
    screen_h = (persona.screen_height if use_preset_screen else None) or HEADLESS_SCREEN[1]
    avail_h = (persona.avail_height if use_preset_screen else None) or screen_h
    win_w = max(800, min(screen_w, screen_w - 4)) if use_preset_screen else screen_w
    win_h = max(600, min(avail_h - 90, screen_h - 90)) if use_preset_screen else screen_h
    if headless is True:
        # Headless otherwise reports an 800x600 screen.
        args += [f"--window-size={win_w},{win_h}", f"--screen-info={{0,0 {screen_w}x{screen_h}}}"]
    elif headless == "offscreen":
        ow, oh = (win_w, win_h) if use_preset_screen else (1600, 900)
        args += ["--window-position=-32000,-32000", f"--window-size={ow},{oh}"]
    elif use_preset_screen:
        args.append(f"--window-size={win_w},{win_h}")
    else:
        args.append("--start-maximized")
    ext_paths = [str(Path(p).resolve()) for p in extensions if p]
    if ext_paths:
        joined = ",".join(ext_paths)
        args += [
            f"--disable-extensions-except={joined}",
            f"--load-extension={joined}",
        ]
    args += list(extra_args)
    return args


class ChromeEngine:
    """One running fingerprint-chromium profile. Use as an async context manager."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        platform: str | None = None,
        persona: personas.Persona | None = None,
        proxy: str | None = None,
        headless: bool | str = False,
        geoip: bool = True,
        timezone: str | None = None,
        locale: str | None = None,
        executable: str | Path | None = None,
        version: str | None = None,
        extra_args: tuple[str, ...] | list[str] = (),
        allow_unsupported: bool = False,
        sandbox: bool | None = None,
        block_webrtc: bool = False,
        stealth_js: bool = True,
        extensions: tuple[str | Path, ...] | list[str | Path] = (),
        pinned_extension_ids: tuple[str, ...] | list[str] = (),
        unpinned_extension_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        if headless not in (True, False, "offscreen"):
            raise ValueError("headless must be True, False or 'offscreen'")
        self.profile_dir = Path(profile_dir)
        self._platform = platform
        self._persona = persona
        self._proxy = proxy
        self.headless = headless
        self.geoip = geoip
        self._timezone = timezone
        self._locale = locale
        self._executable = executable
        self._version = version
        self._extra_args = tuple(extra_args)
        self._allow_unsupported = allow_unsupported
        self.sandbox = default_sandbox() if sandbox is None else sandbox
        self.block_webrtc = bool(block_webrtc)
        self.stealth_js = bool(stealth_js)
        self._extensions = tuple(extensions)
        self._pinned_extension_ids = tuple(pinned_extension_ids)
        self._unpinned_extension_ids = tuple(unpinned_extension_ids)

        self.persona: personas.Persona | None = None
        self.geo: ExitGeo | None = None
        self.bridge: ProxyBridge | None = None
        self.args: list[str] = []
        self.warnings: list[str] = []
        self.crashes: list[str] = []
        self.context: Any = None
        self._playwright: Any = None
        self._selected_executable: Path | None = None
        self._verified_bundle_cache: dict[
            tuple[Path, str | None], tuple[dict[str, Any], str] | None
        ] = {}
        self._cdp_locale_override: str | None = None
        self._locale_sessions: dict[Any, Any] = {}
        self._locale_pending: set[Any] = set()
        self._locale_reschedule: set[Any] = set()
        self._locale_tasks: set[asyncio.Task[Any]] = set()

    # --- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> Any:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> Any:
        if self.context is not None or self.bridge is not None:
            raise EngineError("The engine is already running; stop() it first")
        try:
            return await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> Any:
        self._verified_bundle_cache.clear()
        search_pending = self.profile_dir / ".search-bootstrap-pending"
        initialize_profile_search = (
            search_pending.exists() or not (self.profile_dir / "Default" / "Web Data").exists()
        )
        with launch_stage("chrome_core_resolve"):
            executable = self._resolve_executable()
            self._selected_executable = executable
        _load_preferences(self.profile_dir / "Default" / "Preferences")
        with launch_stage("chrome_persona"):
            persona = self._resolve_persona()
        with launch_stage("chrome_native_integrity"):
            native_realms = _has_verified_native_realms(
                executable, persona, cache=self._verified_bundle_cache
            )
            native_026 = (
                _has_verified_apple_silicon_026(
                    executable, persona, cache=self._verified_bundle_cache
                )
                if getattr(persona, "platform", None) == "macos"
                else True
            )
            native_027 = (
                _has_verified_locale_speech_font_027(
                    executable, persona, cache=self._verified_bundle_cache
                )
                if getattr(persona, "platform", None) == "macos"
                else False
            )
            if getattr(persona, "requires_apple_silicon_026", False) and not native_026:
                msg = (
                    "macOS Apple Silicon device/display persona requires a verified 026+ "
                    "Chromium bundle (active binary lacks 026 capability)"
                )
                if not self._allow_unsupported:
                    raise EngineError(msg)
                self.warnings.append(msg)

        with launch_stage("chrome_proxy_bridge"):
            upstream = Upstream.parse(self._proxy) if self._proxy else None
            if upstream:
                self.bridge = await ProxyBridge(upstream).start()

        with launch_stage("chrome_geo_locale"):
            if upstream and self.geoip and not (persona.timezone and persona.locale):
                self.geo = await self._lookup_geo()
                if not persona.locale:
                    locale = remembered_locale(self.profile_dir, self.geo.country, self.geo.locale)
                    self.geo = replace(self.geo, locale=locale)
                persona = persona.with_geo(self.geo.timezone, self.geo.locale)
        self.persona = persona
        self._cdp_locale_override = (
            persona.locale
            if (
                getattr(persona, "platform", None) == "macos"
                and getattr(persona, "locale", None)
                and not native_027
            )
            else None
        )

        self.args = build_args(
            persona,
            proxy_url=self.bridge.url if self.bridge else None,
            headless=self.headless,
            extra_args=self._extra_args,
            extensions=self._extensions,
            include_display_profile=native_026,
        )

        from playwright.async_api import async_playwright

        with launch_stage("chrome_driver"):
            self._playwright = await async_playwright().start()
        with launch_stage("chrome_preferences"):
            set_webrtc_policy(
                self.profile_dir,
                WEBRTC_POLICY_PROXY if (self.bridge or self.block_webrtc) else WEBRTC_POLICY_DIRECT,
            )
            set_extension_pins(
                self.profile_dir,
                pinned_ids=self._pinned_extension_ids,
                unpinned_ids=self._unpinned_extension_ids,
            )
            set_default_download_preferences(self.profile_dir)
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            if initialize_profile_search:
                # Browser startup creates Web Data even if the native editor later fails.
                search_pending.touch(exist_ok=True)
        with launch_stage("chrome_context_start"):
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                executable_path=str(executable),
                headless=self.headless is True,
                args=self.args,
                ignore_default_args=self._ignored_default_args(),
                no_viewport=True,
                color_scheme="null",
                chromium_sandbox=self.sandbox,
                accept_downloads=True,
            )
            await self._restore_default_downloads()
        # Neither a version/asset flag nor a sibling manifest can grant native capability.
        if self.stealth_js and not native_realms and hasattr(self.context, "add_init_script"):
            await self.context.add_init_script(build_anti_cdp_init_script(persona))
        if initialize_profile_search:
            from .search import initialize_search

            with launch_stage("chrome_search"):
                await initialize_search(self.context)
                search_pending.unlink()
        if self.geo and self.geo.latitude is not None and self.geo.longitude is not None:
            # Only the position is emulated; sites still have to ask for permission.
            await self.context.set_geolocation(
                {"latitude": self.geo.latitude, "longitude": self.geo.longitude, "accuracy": 50}
            )
        for page in self.context.pages:
            self._watch(page, apply_locale=False)
            if self._cdp_locale_override:
                await self._apply_page_locale(page)
        self.context.on("page", self._watch)
        return self.context

    async def stop(self) -> None:
        self._cdp_locale_override = None
        self._locale_pending.clear()
        self._locale_reschedule.clear()
        for task in list(self._locale_tasks):
            task.cancel()
        for task in list(self._locale_tasks):
            with contextlib.suppress(BaseException):
                await task
        self._locale_tasks.clear()
        for cdp in list(self._locale_sessions.values()):
            with contextlib.suppress(Exception):
                await cdp.detach()
        self._locale_sessions.clear()
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.close()
            self.context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None

    # --- helpers ----------------------------------------------------------

    def _resolve_executable(self) -> Path:
        if self._executable:
            path = Path(self._executable)
            if not path.is_file():
                raise EngineNotInstalled(f"Browser executable not found: {path}")
            return path
        installed = fetch.installed_executable(self._version)
        if not installed:
            raise EngineNotInstalled(
                "fingerprint-chromium is not installed; run: python -m chrome_engine fetch"
            )
        return installed

    def _resolve_persona(self) -> personas.Persona:
        target_platform = (
            self._persona.platform if self._persona is not None else self._platform or host_os()
        )
        # shortcut: fail closed until native WebGPU covers service/cross-realm workers.
        if target_platform != host_os() and not self._allow_unsupported:
            raise EngineError(
                f"A {target_platform} persona cannot be presented consistently on a "
                f"{host_os()} host: native worker WebGPU can reveal the host GPU; "
                "use a same-OS profile until a verified native core is available"
            )
        if self._persona is not None:
            pinned = {
                k: v or None
                for k, v in (("timezone", self._timezone), ("locale", self._locale))
                if v is not None
            }
            persona = replace(self._persona, **pinned)
        else:
            has_026 = (
                _has_verified_apple_silicon_026(
                    self._selected_executable,
                    target_platform,
                    cache=self._verified_bundle_cache,
                )
                if (target_platform == "macos" and self._selected_executable is not None)
                else True
            )
            if self._selected_executable is not None and target_platform == "macos":
                existing = personas.load(self.profile_dir)
                if (
                    existing is not None
                    and existing.platform == target_platform
                    and existing.requires_apple_silicon_026
                    and not has_026
                    and not self._allow_unsupported
                ):
                    raise EngineError(
                        "macOS Apple Silicon device/display persona requires a verified 026+ "
                        "Chromium bundle (active binary lacks 026 capability)"
                    )
            persona = personas.load_or_create(
                self.profile_dir,
                self._platform or host_os(),
                coherence_026=has_026,
                timezone=self._timezone,
                locale=self._locale,
            )
        level, reason = personas.compatibility(host_os(), persona.platform)
        if level == personas.UNSUPPORTED and not self._allow_unsupported:
            raise EngineError(
                f"A {persona.platform} persona cannot be presented consistently on a "
                f"{host_os()} host: {reason}"
            )
        if level != personas.OK:
            self.warnings.append(f"{persona.platform} persona on {host_os()}: {reason}")
        return persona

    async def _lookup_geo(self) -> ExitGeo:
        assert self.bridge is not None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lookup, self.bridge.url)
        except Exception as e:
            # Launching anyway would show the host's timezone behind the proxy.
            raise EngineError(
                f"Could not place the proxy's exit IP ({type(e).__name__}: {e}); "
                "pin a timezone and locale or pass geoip=False"
            ) from e

    def _ignored_default_args(self) -> list[str]:
        ignored = list(IGNORED_DEFAULT_ARGS)
        features = playwright_disabled_features_arg()
        if features:
            ignored.append(features)
        else:
            self.warnings.append(
                "Playwright's --disable-features list could not be removed: third-party "
                "storage partitioning and HTTPS upgrades stay off"
            )
        return ignored

    async def _restore_default_downloads(self) -> None:
        """Reset Playwright's temp-UUID download override back to native Chrome downloads."""
        browser = getattr(self.context, "browser", None)
        new_cdp = getattr(browser, "new_browser_cdp_session", None)
        if not callable(new_cdp):
            return
        cdp = None
        try:
            cdp = await new_cdp()
            await cdp.send(
                "Browser.setDownloadBehavior",
                {"behavior": "default", "eventsEnabled": False},
            )
        except Exception as exc:
            self.warnings.append(
                f"Could not restore browser default download behavior ({type(exc).__name__}: {exc})"
            )
        finally:
            if cdp is not None:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    def _spawn_locale_task(self, coro: Any) -> None:
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._locale_tasks.add(task)
        task.add_done_callback(self._locale_tasks.discard)

    def _schedule_page_locale(self, page: Any) -> None:
        if (
            not self._cdp_locale_override
            or page in self._locale_sessions
            or (hasattr(page, "is_closed") and page.is_closed())
        ):
            return
        if page in self._locale_pending:
            self._locale_reschedule.add(page)
            return
        self._spawn_locale_task(self._apply_page_locale(page))

    async def _refresh_locale_session(self, page: Any, cdp: Any) -> None:
        locale = self._cdp_locale_override
        if not locale or (hasattr(page, "is_closed") and page.is_closed()):
            return
        with contextlib.suppress(Exception):
            await cdp.send("Emulation.setLocaleOverride", {"locale": locale})

    def _reconcile_page_locales(self) -> None:
        if not self._cdp_locale_override or self.context is None:
            return
        for p in list(getattr(self.context, "pages", ())):
            self._schedule_page_locale(p)
        for p, cdp in list(self._locale_sessions.items()):
            self._spawn_locale_task(self._refresh_locale_session(p, cdp))

    def _on_page_closed(self, page: Any) -> None:
        self._locale_sessions.pop(page, None)
        self._locale_pending.discard(page)
        self._locale_reschedule.discard(page)
        self._reconcile_page_locales()

    def _on_frame_navigated(self, page: Any, frame: Any) -> None:
        if not hasattr(page, "main_frame") or frame == page.main_frame:
            self._reconcile_page_locales()

    async def _apply_page_locale(self, page: Any) -> None:
        # shortcut: pre-027 CDP Emulation.setLocaleOverride gives eventual recovery (~5-20ms en-US window on renderer-singleton owner swap or cross-origin noopener boot); native 027 LocaleController::Init() gives continuous zero-gap locale and retires this path via LOCALE_SPEECH_FONT_027_BUNDLES.
        locale = self._cdp_locale_override
        if (
            not locale
            or page in self._locale_sessions
            or page in self._locale_pending
            or self.context is None
            or not hasattr(self.context, "new_cdp_session")
            or (hasattr(page, "is_closed") and page.is_closed())
        ):
            return
        self._locale_pending.add(page)
        self._locale_reschedule.discard(page)
        try:
            with contextlib.suppress(Exception):
                cdp = await self.context.new_cdp_session(page)
                retry_delays = (0.01, 0.025, 0.05, 0.1)
                for attempt in range(len(retry_delays) + 1):
                    try:
                        await cdp.send("Emulation.setLocaleOverride", {"locale": locale})
                        break
                    except Exception as exc:
                        # Sibling close/navigation may still be tearing down Mojo ownership in the same renderer.
                        if attempt < len(retry_delays) and "Another locale override" in str(exc):
                            await asyncio.sleep(retry_delays[attempt])
                            if self._cdp_locale_override and not (
                                hasattr(page, "is_closed") and page.is_closed()
                            ):
                                continue
                        with contextlib.suppress(Exception):
                            await cdp.detach()
                        return
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await cdp.detach()
                        raise
                if not self._cdp_locale_override or (
                    hasattr(page, "is_closed") and page.is_closed()
                ):
                    with contextlib.suppress(Exception):
                        await cdp.detach()
                    return
                self._locale_sessions[page] = cdp
                if page in self._locale_reschedule:
                    self._locale_reschedule.discard(page)
                    self._spawn_locale_task(self._refresh_locale_session(page, cdp))
        finally:
            self._locale_pending.discard(page)
            if (
                page in self._locale_reschedule
                and page not in self._locale_sessions
                and self._cdp_locale_override
                and not (hasattr(page, "is_closed") and page.is_closed())
            ):
                self._locale_reschedule.discard(page)
                self._schedule_page_locale(page)

    def _watch(self, page: Any, *, apply_locale: bool = True) -> None:
        page.on("crash", lambda p: self.crashes.append(p.url))
        if self._cdp_locale_override and hasattr(page, "on"):
            page.on("close", lambda *_: self._on_page_closed(page))
            page.on("framenavigated", lambda frame: self._on_frame_navigated(page, frame))
        if apply_locale:
            self._schedule_page_locale(page)
