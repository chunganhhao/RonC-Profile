"""Engine behaviour that needs no browser."""

import asyncio
import json

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


def test_webrtc_policy_replaces_a_corrupt_file(tmp_path):
    path = tmp_path / "Default" / "Preferences"
    path.parent.mkdir()
    path.write_text("{broken")
    engine_mod.set_webrtc_policy(tmp_path, engine_mod.WEBRTC_POLICY_PROXY)
    assert (
        json.loads(path.read_text("utf-8"))["webrtc"]["ip_handling_policy"]
        == "disable_non_proxied_udp"
    )


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
        "cases": {
            "macos:2": {},
            "macos:3": {},
            "apple_silicon_m3_laptop": {},
            "apple_silicon_m2_max_desktop": {},
            "locale_speech_font_de": {},
        },
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
