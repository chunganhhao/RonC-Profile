"""Engine behaviour that needs no browser."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from chrome_engine import engine as engine_mod
from chrome_engine.engine import (
    IGNORED_DEFAULT_ARGS,
    ChromeEngine,
    EngineError,
    EngineNotInstalled,
    build_args,
)
from chrome_engine.persona import Persona

P = Persona(seed=5, platform="windows", platform_version="15.0.0", hardware_concurrency=8)


def test_build_args_headed():
    args = build_args(P)
    assert args[:5] == P.to_args()
    assert "--start-maximized" in args
    assert "--disable-blink-features=AutomationControlled" not in args
    assert not any(a.startswith("--disable-blink-features") for a in args)
    assert not any(a.startswith("--disable-features") for a in args)
    assert not any(a.startswith("--proxy-server") for a in args)


def test_build_args_with_extensions_and_pins(tmp_path):
    ext = tmp_path / "ext1"
    ext.mkdir()
    args = build_args(P, extensions=[ext])
    resolved = str(ext.resolve())
    assert f"--disable-extensions-except={resolved}" in args
    assert f"--load-extension={resolved}" in args

    engine_mod.set_extension_pins(
        tmp_path,
        pinned_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        unpinned_ids=["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
    )
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text("utf-8"))
    assert prefs["extensions"]["pinned_extensions"] == ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    # Custom pinned extension cccc stays intact while aaaa is unpinned
    prefs["extensions"]["pinned_extensions"].append("cccccccccccccccccccccccccccccccc")
    (tmp_path / "Default" / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    engine_mod.set_extension_pins(
        tmp_path,
        pinned_ids=[],
        unpinned_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
    )
    prefs2 = json.loads((tmp_path / "Default" / "Preferences").read_text("utf-8"))
    assert prefs2["extensions"]["pinned_extensions"] == ["cccccccccccccccccccccccccccccccc"]


def test_build_args_headless_sets_a_real_screen():
    args = build_args(P, headless=True)
    assert "--window-size=1920,1080" in args
    assert "--screen-info={0,0 1920x1080}" in args
    assert "--start-maximized" not in args


def test_build_args_offscreen():
    args = build_args(P, headless="offscreen")
    assert "--window-position=-32000,-32000" in args


def test_build_args_proxy_and_extra_come_last():
    args = build_args(P, proxy_url="http://127.0.0.1:5555", extra_args=["--foo"])
    assert "--proxy-server=http://127.0.0.1:5555" in args
    assert args[-1] == "--foo"


def test_build_args_respects_macos_display_preset_bounds():
    from chrome_engine import persona as personas

    mac = personas.generate("macos", seed=3)
    headed_args = build_args(mac, headless=False)
    assert "--start-maximized" not in headed_args
    ws_arg = next(a for a in headed_args if a.startswith("--window-size="))
    ww, wh = (int(x) for x in ws_arg.split("=", 1)[1].split(","))
    assert 800 <= ww <= mac.screen_width
    assert 600 <= wh <= mac.avail_height

    headless_args = build_args(mac, headless=True)
    assert f"--screen-info={{0,0 {mac.screen_width}x{mac.screen_height}}}" in headless_args


def test_playwright_feature_switch_is_found():
    switch = engine_mod.playwright_disabled_features_arg()
    assert switch is not None, "update the parser for this Playwright version"
    assert switch.startswith("--disable-features=")
    assert "ThirdPartyStoragePartitioning" in switch
    assert " " not in switch and '"' not in switch


def test_ignored_args_include_playwright_features(tmp_path):
    ignored = ChromeEngine(tmp_path)._ignored_default_args()
    assert engine_mod.playwright_disabled_features_arg() in ignored


def test_unparseable_playwright_features_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "playwright_disabled_features_arg", lambda: None)
    eng = ChromeEngine(tmp_path)
    assert eng._ignored_default_args() == list(IGNORED_DEFAULT_ARGS)
    assert "storage partitioning" in eng.warnings[0]


def test_ignored_defaults_cover_page_visible_switches():
    for switch in (
        "--enable-automation",
        "--disable-popup-blocking",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        "--disable-back-forward-cache",
        "--no-sandbox",
        "--enable-unsafe-swiftshader",
    ):
        assert switch in IGNORED_DEFAULT_ARGS


def test_headless_value_is_validated(tmp_path):
    with pytest.raises(ValueError):
        ChromeEngine(tmp_path, headless="virtual")


def test_missing_executable(tmp_path):
    eng = ChromeEngine(tmp_path, executable=tmp_path / "nope.exe")
    with pytest.raises(EngineNotInstalled):
        asyncio.run(eng.start())


def test_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.fetch, "installed_executable", lambda v=None: None)
    with pytest.raises(EngineNotInstalled, match="python -m chrome_engine fetch"):
        asyncio.run(ChromeEngine(tmp_path).start())


def _resolve(tmp_path, monkeypatch, host, **kwargs):
    monkeypatch.setattr(engine_mod, "host_os", lambda: host)
    eng = ChromeEngine(tmp_path, **kwargs)
    return eng, eng._resolve_persona()


def test_unsupported_persona_is_refused(tmp_path, monkeypatch):
    with pytest.raises(EngineError, match="cannot be presented consistently"):
        _resolve(tmp_path, monkeypatch, "windows", platform="linux")


def test_unsupported_persona_can_be_forced(tmp_path, monkeypatch):
    eng, persona = _resolve(
        tmp_path, monkeypatch, "windows", platform="linux", allow_unsupported=True
    )
    assert persona.platform == "linux"
    assert eng.warnings


def test_cross_os_persona_refused_before_identity_write(tmp_path, monkeypatch):
    path = tmp_path / "chrome_persona.json"
    path.write_text("do not rewrite an existing identity")
    with pytest.raises(EngineError, match="native worker WebGPU"):
        _resolve(tmp_path, monkeypatch, "windows", platform="macos")
    assert path.read_text() == "do not rewrite an existing identity"


def test_explicit_cross_os_persona_is_also_refused(tmp_path, monkeypatch):
    with pytest.raises(EngineError, match="native worker WebGPU"):
        _resolve(tmp_path, monkeypatch, "macos", persona=P)


def test_platform_defaults_to_host(tmp_path, monkeypatch):
    eng, persona = _resolve(tmp_path, monkeypatch, "linux")
    assert persona.platform == "linux"
    assert eng.warnings == []


def test_explicit_persona_with_pins(tmp_path, monkeypatch):
    eng, persona = _resolve(
        tmp_path, monkeypatch, "windows", persona=P, timezone="Asia/Tokyo", locale="ja-JP"
    )
    assert (persona.seed, persona.timezone, persona.locale) == (5, "Asia/Tokyo", "ja-JP")


def test_saved_persona_is_reused(tmp_path, monkeypatch):
    _, first = _resolve(tmp_path, monkeypatch, "windows")
    _, second = _resolve(tmp_path, monkeypatch, "windows")
    assert first == second


def test_geo_failure_behind_a_proxy_refuses_to_launch(tmp_path, monkeypatch):
    """Launching anyway would show the host's timezone through the proxy."""

    def boom(url):
        raise OSError("no route")

    monkeypatch.setattr(engine_mod, "lookup", boom)
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    eng = ChromeEngine(tmp_path / "p", executable=exe, proxy="http://u:p@127.0.0.1:9")
    with pytest.raises(EngineError, match="exit IP"):
        asyncio.run(eng.start())
    # start() cleaned up the bridge it had opened.
    assert eng.bridge is None


def test_geo_is_skipped_when_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "lookup", lambda url: pytest.fail("looked up"))
    eng = ChromeEngine(
        tmp_path / "p",
        executable=__file__,
        proxy="http://127.0.0.1:9",
        timezone="Europe/Paris",
        locale="fr-FR",
    )

    import playwright.async_api as pw

    class Fake:
        async def start(self):
            raise RuntimeError("stop before the browser")

    monkeypatch.setattr(pw, "async_playwright", lambda: Fake())
    with pytest.raises(RuntimeError, match="stop before the browser"):
        asyncio.run(eng.start())
    assert eng.persona.timezone == "Europe/Paris"
    assert eng.geo is None
    assert "--timezone=Europe/Paris" in eng.args
    assert any(a.startswith("--proxy-server=http://127.0.0.1:") for a in eng.args)


def test_webrtc_policy_on_a_fresh_profile(tmp_path):
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text("utf-8"))
    assert prefs == {"webrtc": {"ip_handling_policy": "disable_non_proxied_udp"}}


def test_webrtc_policy_keeps_other_preferences(tmp_path):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"profile": {"name": "x"}, "webrtc": {"multiple_routes_enabled": False}})
    )
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_DIRECT)
    prefs = json.loads(path.read_text("utf-8"))
    assert prefs["profile"] == {"name": "x"}
    assert prefs["webrtc"] == {
        "multiple_routes_enabled": False,
        "ip_handling_policy": "default_public_interface_only",
    }
    assert not list(path.parent.glob("*tmp*"))


def test_webrtc_policy_aborts_on_unreadable_preferences(tmp_path, monkeypatch):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    original = '{"other_setting":true}'
    path.write_text(original, encoding="utf-8")

    orig_read_bytes = Path.read_bytes

    def deny_read(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_read)
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY, recover_corrupt=True)
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob("*.bak"))
    assert not list(path.parent.glob("*tmp*"))

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        path.chmod(0o000)
        try:
            with pytest.raises(EngineError, match="Could not read profile Preferences"):
                engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
        finally:
            path.chmod(0o600)
        assert path.read_text(encoding="utf-8") == original
        assert not list(path.parent.glob("*.bak"))
        assert not list(path.parent.glob("*tmp*"))


@pytest.mark.parametrize("raw_bytes", [b"{broken", b"[1, 2]", b"\xff\xfe"])
def test_webrtc_policy_preserves_corrupt_preferences_and_backs_up_on_recovery(tmp_path, raw_bytes):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    path.write_bytes(raw_bytes)

    with pytest.raises(EngineError, match="refusing to overwrite without explicit recovery"):
        engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    assert path.read_bytes() == raw_bytes
    assert not list(path.parent.glob("*.bak"))

    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY, recover_corrupt=True)
    backups = sorted(path.parent.glob("Preferences.corrupt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == raw_bytes
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert (
        json.loads(path.read_text("utf-8"))["webrtc"]["ip_handling_policy"]
        == "disable_non_proxied_udp"
    )
    assert not list(path.parent.glob("*tmp*"))

    # Second explicit recovery must create an additional backup without clobbering the first.
    second_corrupt = b"{corrupt-again"
    path.write_bytes(second_corrupt)
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY, recover_corrupt=True)
    backups_after = sorted(path.parent.glob("Preferences.corrupt.*.bak"))
    assert len(backups_after) == 2
    assert sorted(b.read_bytes() for b in backups_after) == sorted([raw_bytes, second_corrupt])
    if os.name == "posix":
        assert all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups_after)


def test_webrtc_policy_refuses_broken_symlink(tmp_path):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    missing_target = tmp_path / "missing-prefs.json"
    path.symlink_to(missing_target)

    with pytest.raises(EngineError, match="unreadable symlink"):
        engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    assert path.is_symlink()
    assert not missing_target.exists()


def test_extension_pins_aborts_on_unreadable_and_preserves_corrupt_preferences(
    tmp_path, monkeypatch
):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    original = '{"other_setting":true}'
    path.write_text(original, encoding="utf-8")

    orig_read_bytes = Path.read_bytes

    def deny_read(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_read)
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        engine_mod.set_extension_pins(tmp_path, pinned_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == original

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(EngineError, match="refusing to overwrite without explicit recovery"):
        engine_mod.set_extension_pins(tmp_path, pinned_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
    assert path.read_text(encoding="utf-8") == "{broken"
    assert not list(path.parent.glob("*.bak"))

    engine_mod.set_extension_pins(
        tmp_path,
        pinned_ids=["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        recover_corrupt=True,
    )
    backups = list(path.parent.glob("Preferences.corrupt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text("utf-8") == "{broken"
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert json.loads(path.read_text("utf-8"))["extensions"]["pinned_extensions"] == [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]


def test_start_aborts_without_writing_on_unreadable_preferences(tmp_path, monkeypatch):
    import playwright.async_api as pw

    from chrome_engine import persona as personas

    monkeypatch.setattr(engine_mod, "host_os", lambda: "windows")
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")

    def make_mocks():
        state = {"launched": False, "bridge_started": False, "bridge_stopped": False}

        class FakeChromium:
            async def launch_persistent_context(self, *args, **kwargs):
                state["launched"] = True
                raise AssertionError("browser must not launch on unreadable Preferences")

        class FakePW:
            chromium = FakeChromium()

            async def stop(self):
                pass

        class FakeStarter:
            async def start(self):
                return FakePW()

        class FakeBridge:
            url = "http://127.0.0.1:9999"

            def __init__(self, *_a):
                pass

            async def start(self):
                state["bridge_started"] = True
                return self

            async def stop(self):
                state["bridge_stopped"] = True

        monkeypatch.setattr(pw, "async_playwright", lambda: FakeStarter())
        monkeypatch.setattr(engine_mod, "ProxyBridge", FakeBridge)
        return state

    orig_read_bytes = Path.read_bytes

    # 1. Pre-existing unreadable Preferences with NO chrome_persona.json:
    # must abort before writing chrome_persona.json, geo_locale.json, or starting ProxyBridge.
    prof_fresh = tmp_path / "prof_fresh"
    prefs_fresh = prof_fresh / "Default" / "Preferences"
    prefs_fresh.parent.mkdir(parents=True)
    original = '{"other_setting":true}'
    prefs_fresh.write_text(original, encoding="utf-8")
    state1 = make_mocks()

    def deny_fresh(self):
        if self == prefs_fresh:
            raise PermissionError(13, "Permission denied", str(self))
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_fresh)
    eng1 = ChromeEngine(
        prof_fresh,
        platform="windows",
        executable=exe,
        proxy="http://127.0.0.1:8888",
        timezone="America/New_York",
        locale="en-US",
    )
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        asyncio.run(eng1.start())
    monkeypatch.setattr(Path, "read_bytes", orig_read_bytes)

    assert not personas.persona_path(prof_fresh).exists()
    assert not (prof_fresh / "geo_locale.json").exists()
    assert not (prof_fresh / ".search-bootstrap-pending").exists()
    assert state1["bridge_started"] is False
    assert state1["launched"] is False
    assert eng1.bridge is None
    assert prefs_fresh.read_text(encoding="utf-8") == original

    # 2. Pre-existing unreadable Preferences WITH saved chrome_persona.json and new overrides:
    # must not overwrite chrome_persona.json with the new timezone/locale.
    prof_saved = tmp_path / "prof_saved"
    prefs_saved = prof_saved / "Default" / "Preferences"
    prefs_saved.parent.mkdir(parents=True)
    prefs_saved.write_text(original, encoding="utf-8")
    personas.save(prof_saved, P)
    persona_before = personas.persona_path(prof_saved).read_bytes()
    state2 = make_mocks()

    def deny_saved(self):
        if self == prefs_saved:
            raise PermissionError(13, "Permission denied", str(self))
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_saved)
    eng2 = ChromeEngine(
        prof_saved,
        platform="windows",
        executable=exe,
        proxy="http://127.0.0.1:8888",
        timezone="America/New_York",
        locale="en-US",
    )
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        asyncio.run(eng2.start())
    monkeypatch.setattr(Path, "read_bytes", orig_read_bytes)

    assert personas.persona_path(prof_saved).read_bytes() == persona_before
    assert not (prof_saved / ".search-bootstrap-pending").exists()
    assert state2["bridge_started"] is False
    assert state2["launched"] is False
    assert eng2.bridge is None
    assert prefs_saved.read_text(encoding="utf-8") == original

    # 3. Mid-launch TOCTOU: readable on preflight, unreadable at chrome_preferences.
    prof_toctou = tmp_path / "prof_toctou"
    prefs_toctou = prof_toctou / "Default" / "Preferences"
    prefs_toctou.parent.mkdir(parents=True)
    prefs_toctou.write_text(original, encoding="utf-8")
    state3 = make_mocks()
    reads = [0]

    def deny_second_read(self):
        if self == prefs_toctou:
            reads[0] += 1
            if reads[0] >= 2:
                raise PermissionError(13, "Permission denied", str(self))
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_second_read)
    eng3 = ChromeEngine(
        prof_toctou,
        platform="windows",
        executable=exe,
        proxy="http://127.0.0.1:8888",
        timezone="America/New_York",
        locale="en-US",
    )
    with pytest.raises(EngineError, match="Could not read profile Preferences"):
        asyncio.run(eng3.start())
    monkeypatch.undo()

    assert state3["bridge_started"] is True
    assert state3["bridge_stopped"] is True
    assert state3["launched"] is False
    assert eng3.bridge is None
    assert prefs_toctou.read_text(encoding="utf-8") == original
    assert not (prof_toctou / ".search-bootstrap-pending").exists()
    assert not list(prefs_toctou.parent.glob("*.bak"))
    assert not list(prefs_toctou.parent.glob("*tmp*"))


def test_webrtc_policy_switches_with_the_proxy(tmp_path):
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_DIRECT)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text("utf-8"))
    assert prefs["webrtc"]["ip_handling_policy"] == "default_public_interface_only"


def test_start_twice_is_refused(tmp_path):
    eng = ChromeEngine(tmp_path)
    eng.context = object()
    with pytest.raises(EngineError, match="already running"):
        asyncio.run(eng.start())


def test_runner_installs_the_engine_on_first_launch(monkeypatch, capsys):
    import asyncio

    from chrome_engine import fetch, runner

    installed = []
    monkeypatch.setattr(
        fetch, "installed_executable", lambda *a, **k: installed[0] if installed else None
    )

    def fake_install(progress=None):
        for done in (0, 50, 100, 100, 100):
            progress(done, 100)
        installed.append("chrome")
        return "chrome"

    monkeypatch.setattr(fetch, "install", fake_install)
    asyncio.run(runner.ensure_installed())
    out = capsys.readouterr().out
    assert "downloading it once" in out
    assert out.count("Downloading Chrome engine: 100%") == 1
    assert "Chrome engine installed" in out

    # Already installed: nothing is printed or downloaded.
    asyncio.run(runner.ensure_installed())
    assert capsys.readouterr().out == ""


def _start_until_browser(eng, monkeypatch):
    import playwright.async_api as pw

    class Fake:
        async def start(self):
            raise RuntimeError("stop before the browser")

    monkeypatch.setattr(pw, "async_playwright", lambda: Fake())
    with pytest.raises(RuntimeError, match="stop before the browser"):
        asyncio.run(eng.start())


def test_geo_locale_is_kept_across_launches(tmp_path, monkeypatch):
    from chrome_engine.geo import ExitGeo

    picks = iter(["es-US", "en-US"])
    monkeypatch.setattr(
        engine_mod,
        "lookup",
        lambda url: ExitGeo("203.0.113.5", "America/Chicago", next(picks), country="US"),
    )
    locales = []
    for _ in range(2):
        eng = ChromeEngine(tmp_path / "p", executable=__file__, proxy="http://127.0.0.1:9")
        _start_until_browser(eng, monkeypatch)
        locales.append(eng.persona.locale)
    assert locales == ["es-US", "es-US"]
    assert "--lang=es-US" in eng.args


def test_macos_026_capability_gate_respects_binary_and_preserves_identity(tmp_path, monkeypatch):
    import hashlib

    from chrome_engine import persona as personas
    from chrome_engine.native_bundle import bundle_sha256

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    app = tmp_path / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"run19-binary")
    bin_sha = hashlib.sha256(b"run19-binary").hexdigest()
    b_sha = bundle_sha256(app)

    # Pin ONLY as legacy native-realms (Run #19), NOT in APPLE_SILICON_COHERENCE_BUNDLES.
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", {b_sha})
    manifest = {
        "binary_sha256": bin_sha,
        "bundle_sha256": b_sha,
        "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
        "cases": {
            "macos:2": {},
            "macos:3": {},
            "apple_silicon_m3_laptop": {},
            "apple_silicon_m2_max_desktop": {},
        },
    }
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Even if sibling manifest claims 026 cases, Run #19 pin MUST fail closed for 026.
    p026 = personas.generate("macos", seed=3)
    assert engine_mod._has_verified_native_realms(exe, p026) is True
    assert engine_mod._has_verified_apple_silicon_026(exe, p026) is False

    prof = tmp_path / "prof026"
    personas.save(prof, p026)
    saved_bytes_before = personas.persona_path(prof).read_bytes()

    eng_refuse = ChromeEngine(prof, platform="macos", executable=exe)
    with pytest.raises(EngineError, match="requires a verified 026\\+"):
        asyncio.run(eng_refuse.start())
    # Persisted chrome_persona.json must NOT be silently overwritten on refusal.
    assert personas.persona_path(prof).read_bytes() == saved_bytes_before

    # Baseline profile on Run #19 stays on compatible pre-026 baseline without 026 display flags.
    prof_base = tmp_path / "prof_base"
    eng_base = ChromeEngine(prof_base, platform="macos", executable=exe)
    _start_until_browser(eng_base, monkeypatch)
    assert eng_base.persona.device_model is None
    assert not eng_base.persona.requires_apple_silicon_026
    assert not any(a.startswith("--fingerprint-display-profile") for a in eng_base.args)

    # Verified 026 bundle pinned + manifest cases -> accepted with 026 args.
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", {b_sha})
    assert engine_mod._has_verified_apple_silicon_026(exe, p026) is True
    eng_ok = ChromeEngine(prof, platform="macos", executable=exe)
    _start_until_browser(eng_ok, monkeypatch)
    assert eng_ok.persona == p026
    assert any(a.startswith("--fingerprint-display-profile=") for a in eng_ok.args)


def test_chrome_engine_hashes_verified_bundle_once_per_launch(tmp_path, monkeypatch):
    import hashlib

    from chrome_engine import persona as personas
    from chrome_engine.native_bundle import bundle_sha256

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    app = tmp_path / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"026-binary")
    bin_sha = hashlib.sha256(b"026-binary").hexdigest()
    b_sha = bundle_sha256(app)

    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", {b_sha})
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", {b_sha})
    manifest = {
        "binary_sha256": bin_sha,
        "bundle_sha256": b_sha,
        "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
        "cases": {
            "macos:2": {},
            "macos:3": {},
            "apple_silicon_m3_laptop": {},
            "apple_silicon_m2_max_desktop": {},
        },
    }
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest), encoding="utf-8")

    hash_calls = 0
    orig_bundle_sha256 = engine_mod.bundle_sha256

    def counting_bundle_sha256(bundle_path):
        nonlocal hash_calls
        hash_calls += 1
        return orig_bundle_sha256(bundle_path)

    monkeypatch.setattr(engine_mod, "bundle_sha256", counting_bundle_sha256)

    prof = tmp_path / "prof_cached"
    personas.save(prof, personas.generate("macos", seed=3))

    eng1 = ChromeEngine(prof, platform="macos", executable=exe)
    _start_until_browser(eng1, monkeypatch)
    assert hash_calls == 1

    eng2 = ChromeEngine(prof, platform="macos", executable=exe)
    _start_until_browser(eng2, monkeypatch)
    assert hash_calls == 2


def _valid_027_manifest_cases() -> dict:
    langs = ["de-DE", "de", "en-US", "en"]
    realm = {"dtf": "de-DE", "nf": "de-DE", "num": "123.456,789", "langs": list(langs)}
    first = {
        "lang": "de-DE",
        "langs": list(langs),
        "dtf": "de-DE",
        "nf": "de-DE",
        "num": "123.456,789",
    }
    return {
        "macos:2": {
            "flatMonochromeChanged": 0,
            "webglRedBoxDigest": engine_mod._PIXELSCAN_RED_BOX_DIGEST,
        },
        "macos:3": {
            "flatMonochromeChanged": 0,
            "webglRedBoxDigest": engine_mod._PIXELSCAN_RED_BOX_DIGEST,
        },
        "apple_silicon_m3_laptop": {},
        "apple_silicon_m2_max_desktop": {},
        "locale_speech_font_de": {
            "seed_3003_font_count": 177,
            "seed_3006_font_count": 178,
            "speech_voice_gate": "enforced",
            "google_voice_count": 0,
            "local_voice_count": 180,
            "local_voice_count_3006": 180,
            "host_local_voice_count": 180,
            "core_font_missing_count": 0,
            "installed_protected_font_count": 169,
            "hidden_case_checked": 3,
            "window": dict(realm),
            "worker": dict(realm),
            "shared": dict(realm),
            "service": dict(realm),
            "noopener_popup": {"first": dict(first), "seen": ["de-DE"]},
            "same_origin_cross_nav_popup": {
                "hadSameOriginOpener": True,
                "first": dict(first),
                "seen": ["de-DE"],
            },
            "same_origin_close_popup": {
                "hadSameOriginOpener": True,
                "openerClosed": True,
                "first": dict(first),
                "seen": ["de-DE"],
            },
        },
    }


def test_runtime_cdp_locale_override_lifecycle_and_027_retirement(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    import playwright.async_api as pw

    from chrome_engine import persona as personas
    from chrome_engine import search
    from chrome_engine.native_bundle import bundle_sha256

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    app = tmp_path / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"026-binary")
    bin_sha = hashlib.sha256(b"026-binary").hexdigest()
    b_sha = bundle_sha256(app)
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", {b_sha})
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", {b_sha})
    manifest = {
        "binary_sha256": bin_sha,
        "bundle_sha256": b_sha,
        "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
        "cases": _valid_027_manifest_cases(),
    }
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest), encoding="utf-8")

    cdp_calls = []
    detached = []
    page_listeners = {}
    owner_page = [None]
    retry_remaining = [0]
    on_popup_terminal_detach = [None]

    class FakePage:
        def __init__(self, name):
            self.name = name
            self.closed = False
            self.main_frame = f"{name}-main"

        def is_closed(self):
            return self.closed

        def on(self, event, cb):
            page_listeners.setdefault(self.name, {}).setdefault(event, []).append(cb)

    class FakeCdp:
        def __init__(self, page):
            self.page = page

        async def send(self, method, params=None):
            if owner_page[0] is not None and owner_page[0] is not self.page:
                raise RuntimeError(
                    "CDPSession.send: Protocol error (Emulation.setLocaleOverride): "
                    "Another locale override is already in effect"
                )
            if retry_remaining[0] > 0:
                retry_remaining[0] -= 1
                raise RuntimeError(
                    "CDPSession.send: Protocol error (Emulation.setLocaleOverride): "
                    "Another locale override is already in effect"
                )
            if owner_page[0] is None:
                owner_page[0] = self.page
            cdp_calls.append((self.page.name, method, params))

        async def detach(self):
            detached.append(self.page.name)
            if owner_page[0] is self.page:
                owner_page[0] = None
            if self.page is popup_tab and on_popup_terminal_detach[0] is not None:
                cb = on_popup_terminal_detach[0]
                on_popup_terminal_detach[0] = None
                cb()

    initial_page = FakePage("p1")
    new_tab = FakePage("p2")
    popup_tab = FakePage("popup")
    ctx_listeners = {}

    class FakeContext:
        pages = [initial_page]

        def on(self, event, cb):
            ctx_listeners.setdefault(event, []).append(cb)

        async def new_cdp_session(self, page):
            return FakeCdp(page)

        async def close(self):
            pass

    class FakePlaywright:
        chromium = SimpleNamespace(launch_persistent_context=lambda *a, **kw: _ret_ctx())

        async def stop(self):
            pass

    async def _ret_ctx():
        return FakeContext()

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(pw, "async_playwright", lambda: FakeStarter())

    async def no_search(_ctx):
        pass

    monkeypatch.setattr(search, "initialize_search", no_search)
    prof = tmp_path / "prof"
    personas.save(prof, personas.generate("macos", seed=3, locale="de-DE"))

    async def run_pre_027():
        eng = ChromeEngine(prof, platform="macos", executable=exe, geoip=False)
        await eng.start()
        assert cdp_calls == [("p1", "Emulation.setLocaleOverride", {"locale": "de-DE"})]

        # Same-origin popup is rejected while opener p1 holds the renderer lock.
        # Even if p1 closes DURING popup's terminal detach() while popup is still
        # marked pending, _locale_reschedule must preserve the handoff and recover
        # across 2 consecutive transient lock rejections.
        p1_cdp = eng._locale_sessions[initial_page]

        def close_opener_during_terminal_detach():
            assert popup_tab in eng._locale_pending
            FakeContext.pages.remove(initial_page)
            initial_page.closed = True
            owner_page[0] = None
            retry_remaining[0] = 2
            page_listeners["p1"]["close"][0]()
            assert popup_tab in eng._locale_reschedule

        on_popup_terminal_detach[0] = close_opener_during_terminal_detach
        FakeContext.pages.append(popup_tab)
        ctx_listeners["page"][0](popup_tab)
        while eng._locale_tasks:
            await asyncio.gather(*list(eng._locale_tasks))
        assert "popup" in detached
        assert popup_tab in eng._locale_sessions
        assert initial_page not in eng._locale_sessions
        assert eng._locale_pending == set()
        assert eng._locale_reschedule == set()
        await p1_cdp.detach()

        # Cross-origin new tab succeeds directly; stop detaches surviving sessions cleanly.
        owner_page[0] = None
        FakeContext.pages.append(new_tab)
        ctx_listeners["page"][0](new_tab)
        await asyncio.sleep(0)
        assert new_tab in eng._locale_sessions
        owner_page[0] = popup_tab
        await eng.stop()
        assert eng._locale_sessions == {}
        assert eng._locale_pending == set()
        assert eng._locale_reschedule == set()

        # Events firing after stop clears _cdp_locale_override must not queue tasks.
        before = len(cdp_calls)
        ctx_listeners["page"][0](FakePage("late"))
        assert eng._locale_tasks == set()
        assert len(cdp_calls) == before

    asyncio.run(run_pre_027())

    # GeoIP-resolved locale (when persona.locale is initially None) is also picked up.
    cdp_calls.clear()
    owner_page[0] = None
    FakeContext.pages = [initial_page]
    initial_page.closed = False
    geo_prof = tmp_path / "geo_prof"
    personas.save(geo_prof, personas.generate("macos", seed=7))
    eng_geo = ChromeEngine(geo_prof, platform="macos", executable=exe, geoip=True)

    async def fake_lookup():
        return engine_mod.ExitGeo(
            ip="198.51.100.9", country="FR", timezone="Europe/Paris", locale="fr-FR"
        )

    monkeypatch.setattr(eng_geo, "_lookup_geo", fake_lookup)

    class NoOpBridge:
        url = "http://127.0.0.1:9999"

        def __init__(self, *_a):
            pass

        async def start(self):
            return self

        async def stop(self):
            pass

    monkeypatch.setattr(engine_mod, "ProxyBridge", NoOpBridge)
    eng_geo._proxy = "http://127.0.0.1:8888"
    asyncio.run(eng_geo.start())
    assert cdp_calls == [("p1", "Emulation.setLocaleOverride", {"locale": "fr-FR"})]
    asyncio.run(eng_geo.stop())

    # Once a verified 027 bundle is pinned, CDP locale override automatically retires.
    cdp_calls.clear()
    monkeypatch.setattr(engine_mod, "LOCALE_SPEECH_FONT_027_BUNDLES", {b_sha})
    eng_027 = ChromeEngine(prof, platform="macos", executable=exe, geoip=False)
    asyncio.run(eng_027.start())
    assert eng_027._cdp_locale_override is None
    assert cdp_calls == []
    asyncio.run(eng_027.stop())


def test_027_bundle_gate_requires_semantic_non_waived_evidence(tmp_path, monkeypatch):
    import copy
    import hashlib

    from chrome_engine.native_bundle import bundle_sha256

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    app = tmp_path / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"029-binary")
    bin_sha = hashlib.sha256(b"029-binary").hexdigest()
    b_sha = bundle_sha256(app)
    monkeypatch.setattr(engine_mod, "LOCALE_SPEECH_FONT_027_BUNDLES", {b_sha})

    def check_cases(cases: dict) -> bool:
        manifest = {
            "binary_sha256": bin_sha,
            "bundle_sha256": b_sha,
            "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
            "cases": cases,
        }
        (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest), encoding="utf-8")
        return engine_mod._has_verified_locale_speech_font_027(exe, "macos")

    base_cases = _valid_027_manifest_cases()
    assert check_cases(base_cases) is True

    unavailable_host_cases = copy.deepcopy(base_cases)
    unavailable_host_cases["locale_speech_font_de"]["host_local_voice_count"] = None
    assert check_cases(unavailable_host_cases) is True

    mutations = [
        lambda c: c.__setitem__("locale_speech_font_de", {}),
        lambda c: c["locale_speech_font_de"].pop("host_local_voice_count"),
        lambda c: c["locale_speech_font_de"].__setitem__("host_local_voice_count", 0),
        lambda c: c["locale_speech_font_de"].__setitem__("host_local_voice_count", -1),
        lambda c: c["locale_speech_font_de"].__setitem__("host_local_voice_count", True),
        lambda c: c["locale_speech_font_de"].__setitem__("host_local_voice_count", False),
        lambda c: c["locale_speech_font_de"].__setitem__("host_local_voice_count", "180"),
        lambda c: c["locale_speech_font_de"].update(
            {"host_local_voice_count": None, "local_voice_count": 0}
        ),
        lambda c: c["locale_speech_font_de"].update(
            {"host_local_voice_count": None, "local_voice_count_3006": 0}
        ),
        lambda c: c["locale_speech_font_de"].__setitem__(
            "speech_voice_gate", "host_unavailable_on_runner"
        ),
        lambda c: c["locale_speech_font_de"].__setitem__("google_voice_count", 1),
        lambda c: c["locale_speech_font_de"].__setitem__("google_voice_count", False),
        lambda c: c["locale_speech_font_de"].__setitem__("local_voice_count", 0),
        lambda c: c["locale_speech_font_de"].__setitem__("local_voice_count", True),
        lambda c: c["locale_speech_font_de"].__setitem__("core_font_missing_count", 1),
        lambda c: c["locale_speech_font_de"].__setitem__("installed_protected_font_count", 0),
        lambda c: c["locale_speech_font_de"].__setitem__("seed_3003_font_count", 0),
        lambda c: c["locale_speech_font_de"].__setitem__("hidden_case_checked", 0),
        lambda c: c["locale_speech_font_de"]["window"].__setitem__("dtf", "en-US"),
        lambda c: c["locale_speech_font_de"]["worker"].__setitem__("nf", "en-US"),
        lambda c: c["locale_speech_font_de"]["worker"].__setitem__("num", "123,456.789"),
        lambda c: c["locale_speech_font_de"]["worker"].__setitem__("langs", ["en-US"]),
        lambda c: c["locale_speech_font_de"]["noopener_popup"].pop("first"),
        lambda c: c["locale_speech_font_de"]["noopener_popup"]["first"].__setitem__("nf", "en-US"),
        lambda c: c["locale_speech_font_de"]["noopener_popup"]["first"].__setitem__(
            "lang", "en-US"
        ),
        lambda c: c["locale_speech_font_de"]["same_origin_cross_nav_popup"].__setitem__(
            "hadSameOriginOpener", False
        ),
        lambda c: c["locale_speech_font_de"]["same_origin_close_popup"].__setitem__(
            "openerClosed", False
        ),
        lambda c: c["locale_speech_font_de"]["same_origin_close_popup"].__setitem__(
            "seen", ["de-DE", "en-US"]
        ),
        lambda c: c["macos:2"].__setitem__("flatMonochromeChanged", 1),
        lambda c: c["macos:2"].__setitem__("flatMonochromeChanged", False),
        lambda c: c["macos:3"].__setitem__("webglRedBoxDigest", "0" * 64),
    ]
    for mutate in mutations:
        mutated = copy.deepcopy(base_cases)
        mutate(mutated)
        assert check_cases(mutated) is False


def test_set_default_download_preferences(tmp_path, monkeypatch):
    # 1. Fresh profile seeds prompt_for_download=False without setting default_directory.
    fresh = tmp_path / "fresh"
    engine_mod.set_default_download_preferences(fresh)
    fresh_prefs = json.loads((fresh / "Default" / "Preferences").read_text("utf-8"))
    assert fresh_prefs["download"] == {"prompt_for_download": False}
    assert "default_directory" not in fresh_prefs["download"]

    # 2. Existing custom default_directory is preserved while seeding prompt_for_download=False.
    custom = tmp_path / "custom"
    (custom / "Default").mkdir(parents=True)
    (custom / "Default" / "Preferences").write_text(
        json.dumps({"download": {"default_directory": "/tmp/custom-dl"}}),
        encoding="utf-8",
    )
    engine_mod.set_default_download_preferences(custom)
    custom_prefs = json.loads((custom / "Default" / "Preferences").read_text("utf-8"))
    assert custom_prefs["download"] == {
        "default_directory": "/tmp/custom-dl",
        "prompt_for_download": False,
    }

    # 3. Explicit boolean prompt_for_download (True or False) is never overwritten or rewritten.
    writes: list[Path] = []
    orig_write = engine_mod._write_preferences

    def spy_write(path: Path, prefs: dict) -> None:
        writes.append(path)
        orig_write(path, prefs)

    monkeypatch.setattr(engine_mod, "_write_preferences", spy_write)
    prompt_true = tmp_path / "prompt_true"
    (prompt_true / "Default").mkdir(parents=True)
    (prompt_true / "Default" / "Preferences").write_text(
        json.dumps({"download": {"prompt_for_download": True}}),
        encoding="utf-8",
    )
    engine_mod.set_default_download_preferences(prompt_true)
    assert (
        json.loads((prompt_true / "Default" / "Preferences").read_text("utf-8"))["download"][
            "prompt_for_download"
        ]
        is True
    )
    engine_mod.set_default_download_preferences(fresh)
    assert writes == []
    monkeypatch.undo()

    # 4. Non-dict download or non-bool prompt_for_download is normalized to False.
    bad_type = tmp_path / "bad_type"
    (bad_type / "Default").mkdir(parents=True)
    (bad_type / "Default" / "Preferences").write_text(
        json.dumps({"download": "not-a-dict"}), encoding="utf-8"
    )
    engine_mod.set_default_download_preferences(bad_type)
    assert json.loads((bad_type / "Default" / "Preferences").read_text("utf-8"))["download"] == {
        "prompt_for_download": False
    }

    bad_flag = tmp_path / "bad_flag"
    (bad_flag / "Default").mkdir(parents=True)
    (bad_flag / "Default" / "Preferences").write_text(
        json.dumps({"download": {"prompt_for_download": 1}}), encoding="utf-8"
    )
    engine_mod.set_default_download_preferences(bad_flag)
    assert json.loads((bad_flag / "Default" / "Preferences").read_text("utf-8"))["download"] == {
        "prompt_for_download": False
    }

    # 5. Corrupt Preferences fails closed unless recover_corrupt=True.
    corrupt = tmp_path / "corrupt"
    corrupt_path = corrupt / "Default" / "Preferences"
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(EngineError, match="refusing to overwrite without explicit recovery"):
        engine_mod.set_default_download_preferences(corrupt)
    assert corrupt_path.read_text("utf-8") == "{broken"
    assert not list(corrupt_path.parent.glob("*.bak"))

    engine_mod.set_default_download_preferences(corrupt, recover_corrupt=True)
    backups = list(corrupt_path.parent.glob("Preferences.corrupt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text("utf-8") == "{broken"
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert json.loads(corrupt_path.read_text("utf-8"))["download"] == {"prompt_for_download": False}


def test_restore_default_downloads_sends_cdp_and_detaches(tmp_path, monkeypatch):
    import playwright.async_api as pw

    from chrome_engine import persona as personas
    from chrome_engine import search

    monkeypatch.setattr(engine_mod, "host_os", lambda: "windows")
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")

    async def no_search(_ctx):
        pass

    monkeypatch.setattr(search, "initialize_search", no_search)

    def make_env(browser_factory):
        class FakeContext:
            pages = []
            browser = browser_factory()

            def on(self, *_args):
                pass

            async def close(self):
                pass

        class FakePW:
            class chromium:
                @staticmethod
                async def launch_persistent_context(*_args, **_kwargs):
                    return FakeContext()

            async def stop(self):
                pass

        class FakeStarter:
            async def start(self):
                return FakePW()

        monkeypatch.setattr(pw, "async_playwright", lambda: FakeStarter())

    def setup_prof(name: str) -> Path:
        prof = tmp_path / name
        personas.save(prof, personas.generate("windows", seed=11))
        return prof

    # 1. Normal CDP reset sends behavior=default, eventsEnabled=False, and detaches.
    events: list[tuple] = []

    class NormalCDP:
        async def send(self, method, params):
            events.append(("send", method, params))

        async def detach(self):
            events.append(("detach",))

    class NormalBrowser:
        async def new_browser_cdp_session(self):
            return NormalCDP()

    make_env(NormalBrowser)
    eng1 = ChromeEngine(
        setup_prof("p1"), platform="windows", executable=exe, geoip=False, stealth_js=False
    )
    asyncio.run(eng1.start())
    assert events == [
        ("send", "Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False}),
        ("detach",),
    ]
    assert eng1.warnings == []
    asyncio.run(eng1.stop())

    # 2. RuntimeError during CDP send warns cleanly and still detaches.
    events.clear()

    class FailingCDP:
        async def send(self, _method, _params):
            raise RuntimeError("cdp failed")

        async def detach(self):
            events.append(("detach",))

    class FailingBrowser:
        async def new_browser_cdp_session(self):
            return FailingCDP()

    make_env(FailingBrowser)
    eng2 = ChromeEngine(
        setup_prof("p2"), platform="windows", executable=exe, geoip=False, stealth_js=False
    )
    asyncio.run(eng2.start())
    assert events == [("detach",)]
    assert any("Could not restore browser default download behavior" in w for w in eng2.warnings)
    asyncio.run(eng2.stop())

    # 3. CancelledError propagates instead of being swallowed, while still detaching.
    events.clear()

    class CancelledCDP:
        async def send(self, _method, _params):
            raise asyncio.CancelledError()

        async def detach(self):
            events.append(("detach",))

    class CancelledBrowser:
        async def new_browser_cdp_session(self):
            return CancelledCDP()

    make_env(CancelledBrowser)
    eng3 = ChromeEngine(
        setup_prof("p3"), platform="windows", executable=exe, geoip=False, stealth_js=False
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(eng3.start())
    assert events == [("detach",)]

    # 4. context.browser is None -> skips cleanly with no warnings.
    make_env(lambda: None)
    eng4 = ChromeEngine(
        setup_prof("p4"), platform="windows", executable=exe, geoip=False, stealth_js=False
    )
    asyncio.run(eng4.start())
    assert eng4.warnings == []
    asyncio.run(eng4.stop())
