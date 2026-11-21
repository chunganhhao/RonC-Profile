"""Integration tests against the real fingerprint-chromium build.

Skipped unless the pinned build is installed (python -m chrome_engine fetch).
Nothing here touches the internet: proxies, origins and the probe page are
local servers, and geo lookups are stubbed.
"""

from __future__ import annotations

import asyncio

import pytest

from chrome_engine import engine as engine_mod
from chrome_engine import probe
from chrome_engine.engine import ChromeEngine
from chrome_engine.geo import ExitGeo
from chrome_engine.persona import Persona
from chrome_engine.release import host_os

from .servers import HttpProxy, Origin, Socks5Proxy

pytestmark = pytest.mark.browser

HOST = host_os()
# Proxies would skip loopback targets without this, and every target here is local.
THROUGH_PROXY = ("--proxy-bypass-list=<-loopback>",)


def run(coro, timeout=120):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def persona(seed=4242, platform=HOST, **kw):
    versions = {"windows": "19.0.0", "macos": "15.6.1", "linux": "6.8.0"}
    base = dict(
        seed=seed,
        platform=platform,
        platform_version=versions[platform],
        hardware_concurrency=8,
        timezone="Asia/Ho_Chi_Minh",
        locale="vi-VN",
    )
    return Persona(**{**base, **kw})


async def _probe(chrome_exe, profile_dir, **kwargs):
    eng = ChromeEngine(profile_dir, executable=chrome_exe, **kwargs)
    context = await eng.start()
    try:
        fp = await probe.collect(context)
        return eng, fp
    finally:
        await eng.stop()


@pytest.mark.parametrize("headless", [True, False, "offscreen"])
def test_fingerprint_is_coherent(chrome_exe, tmp_path, headless):
    p = persona()
    eng, fp = run(_probe(chrome_exe, tmp_path, persona=p, headless=headless))
    issues = probe.check(eng.persona, fp)
    assert issues == [], "\n".join(map(str, issues))
    assert probe.chrome_major(fp) == 148
    assert eng.crashes == []


@pytest.mark.parametrize("brand", ["Chrome", "Edge"])
def test_brands(chrome_exe, tmp_path, brand):
    p = persona(brand=brand, platform="windows" if HOST == "linux" else HOST)
    if HOST == "linux":
        pytest.skip("Edge personas are not offered on Linux")
    eng, fp = run(_probe(chrome_exe, tmp_path, persona=p, headless=True))
    assert probe.check(eng.persona, fp) == []


@pytest.mark.skipif(HOST != "windows", reason="cross-OS persona measured on Windows")
def test_macos_persona_on_windows_host(chrome_exe, tmp_path):
    p = persona(platform="macos")
    eng, fp = run(_probe(chrome_exe, tmp_path, persona=p, headless=True, allow_unsupported=True))
    assert probe.check(eng.persona, fp) == []
    assert "Apple" in fp["webgl"]["renderer"]
    assert eng.warnings  # fonts are still the host's


def test_emoji_canvas_does_not_crash(chrome_exe, tmp_path):
    """150.0.7871.186 crashed here in ~50% of launches; the pin must not."""

    async def go():
        for seed in range(1000, 1006):
            eng = ChromeEngine(
                tmp_path / str(seed),
                executable=chrome_exe,
                persona=persona(seed=seed),
                headless=True,
            )
            context = await eng.start()
            try:
                async with probe.ProbeServer() as server:
                    page = context.pages[0]
                    await page.goto(server.url + "/")
                    assert await page.evaluate(probe.EMOJI_CANVAS_JS) > 0
                    assert await page.evaluate(probe.EMOJI_CANVAS_JS) > 0
                assert eng.crashes == []
            finally:
                await eng.stop()

    run(go(), timeout=300)


def test_same_seed_same_device_across_launches(chrome_exe, tmp_path):
    p = persona(seed=777)
    _, a = run(_probe(chrome_exe, tmp_path / "a", persona=p, headless=True))
    _, b = run(_probe(chrome_exe, tmp_path / "b", persona=p, headless=True))
    assert a["canvasHash"] == b["canvasHash"]
    assert a["audioHash"] == b["audioHash"]
    assert a["webgl"] == b["webgl"]


def test_different_seeds_are_different_devices(chrome_exe, tmp_path):
    _, a = run(_probe(chrome_exe, tmp_path / "a", persona=persona(seed=101), headless=True))
    _, b = run(_probe(chrome_exe, tmp_path / "b", persona=persona(seed=202), headless=True))
    assert a["canvasHash"] != b["canvasHash"]
    assert a["audioHash"] != b["audioHash"]


def test_profile_persists_cookies_storage_and_persona(chrome_exe, tmp_path):
    async def go():
        origin = await Origin("localhost").start()
        try:
            eng = ChromeEngine(tmp_path, executable=chrome_exe, headless=True)
            context = await eng.start()
            first = eng.persona
            page = context.pages[0]
            await page.goto(origin.url + "/")
            await page.evaluate(
                "() => { document.cookie = 'sid=abc; max-age=86400'; localStorage.setItem('k', 'v'); }"
            )
            await eng.stop()

            eng = ChromeEngine(tmp_path, executable=chrome_exe, headless=True)
            context = await eng.start()
            try:
                page = context.pages[0]
                await page.goto(origin.url + "/")
                cookie, stored = await page.evaluate(
                    "() => [document.cookie, localStorage.getItem('k')]"
                )
                assert "sid=abc" in cookie
                assert stored == "v"
                assert eng.persona == first
            finally:
                await eng.stop()
        finally:
            await origin.stop()

    run(go())


@pytest.mark.parametrize("proxy_cls", [HttpProxy, Socks5Proxy])
def test_authenticated_proxy(chrome_exe, tmp_path, proxy_cls):
    async def go():
        origin = await Origin("localhost").start()
        upstream = await proxy_cls().start()
        try:
            eng = ChromeEngine(
                tmp_path,
                executable=chrome_exe,
                persona=persona(),
                proxy=upstream.url,
                headless=True,
                extra_args=THROUGH_PROXY,
            )
            context = await eng.start()
            try:
                page = context.pages[0]
                await page.goto(origin.url + "/")
                assert "origin-ok" in await page.content()
                fp = await probe.collect(context)
            finally:
                await eng.stop()
            issues = probe.check(eng.persona, fp)
            assert issues == [], "\n".join(map(str, issues))
            assert fp["webrtcCandidates"] == []
            if isinstance(upstream, Socks5Proxy):
                # Names reach the proxy unresolved: no DNS leak.
                assert ("domain", "localhost", origin.port) in upstream.targets
            else:
                assert any(target.startswith("http://localhost") for _, target in upstream.requests)
            assert origin.hits
        finally:
            await upstream.stop()
            await origin.stop()

    run(go())


def test_wrong_proxy_credentials_never_fall_back_to_direct(chrome_exe, tmp_path):
    async def go():
        origin = await Origin("localhost").start()
        upstream = await HttpProxy(password="right").start()
        try:
            bad = upstream.url.replace("right", "wrong")
            eng = ChromeEngine(
                tmp_path,
                executable=chrome_exe,
                persona=persona(),
                proxy=bad,
                headless=True,
                extra_args=THROUGH_PROXY,
            )
            context = await eng.start()
            try:
                with pytest.raises(Exception):  # noqa: B017 - any navigation error
                    await context.pages[0].goto(origin.url + "/", timeout=15_000)
            finally:
                await eng.stop()
            assert origin.hits == []
            assert upstream.rejected >= 1
        finally:
            await upstream.stop()
            await origin.stop()

    run(go())


def test_geoip_fills_timezone_locale_and_position(chrome_exe, tmp_path, monkeypatch):
    seen = []

    def fake_lookup(url):
        seen.append(url)
        return ExitGeo(
            ip="203.0.113.7",
            timezone="Asia/Tokyo",
            locale="ja-JP",
            latitude=35.68,
            longitude=139.69,
        )

    monkeypatch.setattr(engine_mod, "lookup", fake_lookup)

    async def go():
        upstream = await HttpProxy().start()
        try:
            p = persona(timezone=None, locale=None)
            eng = ChromeEngine(
                tmp_path, executable=chrome_exe, persona=p, proxy=upstream.url, headless=True
            )
            context = await eng.start()
            try:
                await context.grant_permissions(["geolocation"])
                fp = await probe.collect(context)
                async with probe.ProbeServer() as server:
                    page = context.pages[0]
                    await page.goto(server.url + "/")
                    coords = await page.evaluate(
                        """() => new Promise((ok, fail) => navigator.geolocation.getCurrentPosition(
                            p => ok([p.coords.latitude, p.coords.longitude]), fail))"""
                    )
            finally:
                await eng.stop()
            assert seen and seen[0].startswith("http://127.0.0.1:")  # through the bridge
            assert (eng.persona.timezone, eng.persona.locale) == ("Asia/Tokyo", "ja-JP")
            assert probe.check(eng.persona, fp) == []
            assert coords == pytest.approx([35.68, 139.69])
        finally:
            await upstream.stop()

    run(go())


def test_saved_persona_does_not_store_geo(chrome_exe, tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_mod,
        "lookup",
        lambda url: ExitGeo(ip="203.0.113.7", timezone="Asia/Tokyo", locale="ja-JP"),
    )

    async def go():
        upstream = await HttpProxy().start()
        try:
            eng = ChromeEngine(tmp_path, executable=chrome_exe, proxy=upstream.url, headless=True)
            await eng.start()
            await eng.stop()
        finally:
            await upstream.stop()

    run(go())
    from chrome_engine.persona import load

    saved = load(tmp_path)
    # The next launch may use another exit IP: geo must not be frozen into the persona.
    assert (saved.timezone, saved.locale) == (None, None)


def test_automation_switches_are_gone(chrome_exe, tmp_path):
    async def go():
        eng = ChromeEngine(tmp_path, executable=chrome_exe, persona=persona(), headless=True)
        context = await eng.start()
        try:
            page = context.pages[0]
            await page.goto("chrome://version")
            return await page.inner_text("#command_line")
        finally:
            await eng.stop()

    cmdline = run(go())
    for switch in engine_mod.IGNORED_DEFAULT_ARGS:
        assert f"{switch} " not in cmdline + " ", switch
    assert "swiftshader" not in cmdline.lower()
    # Playwright's feature list (storage partitioning off, HTTPS upgrades off...) is gone.
    assert "--disable-features=" not in cmdline
    assert "ThirdPartyStoragePartitioning" not in cmdline


def test_native_sharedworker_preserves_blob_url(chrome_exe, tmp_path):
    if not engine_mod._has_verified_native_realms(chrome_exe, persona()):
        pytest.skip("requires verified native-realms Chromium bundle")

    async def go():
        origin = await Origin("localhost").start()
        try:
            eng = ChromeEngine(tmp_path, executable=chrome_exe, persona=persona(), headless=True)
            context = await eng.start()
            try:
                page = context.pages[0]
                await page.goto(origin.url + "/")
                return await page.evaluate(
                    """async () => {
                      const u = URL.createObjectURL(new Blob(
                        ['onconnect = (e) => { e.ports[0].postMessage(self.location.href); };'],
                        {type: 'text/javascript'}
                      ));
                      const sw = new SharedWorker(u);
                      const href = await new Promise((r) => {
                        sw.port.onmessage = (e) => r(e.data);
                        sw.port.start();
                        setTimeout(() => r('timeout'), 3000);
                      });
                      URL.revokeObjectURL(u);
                      return {u, href, matches: href === u};
                    }"""
                )
            finally:
                await eng.stop()
        finally:
            await origin.stop()

    res = run(go())
    assert res["matches"] is True, res


def test_popups_are_blocked_like_a_normal_browser(chrome_exe, tmp_path):
    async def go():
        eng = ChromeEngine(tmp_path, executable=chrome_exe, persona=persona(), headless=True)
        context = await eng.start()
        try:
            page = context.pages[0]
            # An inline script has no user gesture (page.evaluate would have one),
            # so Chrome's popup blocker must refuse the window.
            html = "<script>document.title = String(window.open('about:blank') === null)</script>"
            await page.goto("data:text/html," + html)
            await page.wait_for_function("() => document.title !== ''")
            return await page.title(), len(context.pages)
        finally:
            await eng.stop()

    assert run(go()) == ("true", 1)


def test_third_party_storage_is_partitioned(chrome_exe, tmp_path):
    """Playwright turns partitioning off; the engine must turn it back on."""

    async def go():
        top = await Origin("127.0.0.1").start()
        third = await Origin("localhost").start()
        eng = ChromeEngine(tmp_path, executable=chrome_exe, persona=persona(), headless=True)
        context = await eng.start()
        try:
            page = context.pages[0]
            await page.goto(third.url + "/")
            await page.evaluate("() => localStorage.setItem('k', 'first-party')")
            await page.goto(top.url + "/")
            await page.evaluate(
                """(src) => new Promise(r => { const f = document.createElement('iframe');
                   f.src = src; f.onload = r; document.body.appendChild(f); })""",
                third.url + "/",
            )
            frame = next(f for f in page.frames if f.url.startswith(third.url))
            return await frame.evaluate("() => localStorage.getItem('k')")
        finally:
            await eng.stop()
            await top.stop()
            await third.stop()

    assert run(go()) is None


def test_runtime_enable_is_not_visible(chrome_exe, tmp_path):
    _, fp = run(_probe(chrome_exe, tmp_path, persona=persona(), headless=True))
    assert fp["cdpDetected"] is False
    assert fp["main"]["webdriver"] is False


def test_webrtc_policy_follows_the_proxy(chrome_exe, tmp_path):
    """Direct: public interface only (never a LAN address). Proxied: no UDP at all."""
    import json

    def policy(profile):
        prefs = json.loads((profile / "Default" / "Preferences").read_text("utf-8"))
        return prefs["webrtc"]["ip_handling_policy"]

    async def go():
        eng = ChromeEngine(tmp_path, executable=chrome_exe, persona=persona(), headless=True)
        context = await eng.start()
        try:
            fp = await probe.collect(context)
        finally:
            await eng.stop()
        assert probe.check(eng.persona, fp) == []
        assert policy(tmp_path) == engine_mod.WEBRTC_POLICY_DIRECT

        upstream = await HttpProxy().start()
        try:
            eng = ChromeEngine(
                tmp_path,
                executable=chrome_exe,
                persona=persona(),
                proxy=upstream.url,
                headless=True,
            )
            await eng.start()
            await eng.stop()
        finally:
            await upstream.stop()
        assert policy(tmp_path) == engine_mod.WEBRTC_POLICY_PROXY

    run(go())


def test_parallel_profiles_are_isolated(chrome_exe, tmp_path):
    """Three profiles at once: separate devices, separate cookies."""

    async def one(i, origin):
        p = persona(seed=5000 + i, hardware_concurrency=4 * (i + 1))
        eng = ChromeEngine(tmp_path / f"p{i}", executable=chrome_exe, persona=p, headless=True)
        context = await eng.start()
        try:
            page = context.pages[0]
            await page.goto(origin.url + "/")
            await page.evaluate(f"() => {{ document.cookie = 'who={i}; max-age=600'; }}")
            fp = await probe.collect(context)
            await page.goto(origin.url + "/")
            cookie = await page.evaluate("() => document.cookie")
            return eng.persona, fp, cookie
        finally:
            await eng.stop()

    async def go():
        origin = await Origin("localhost").start()
        try:
            return await asyncio.gather(*(one(i, origin) for i in range(3)))
        finally:
            await origin.stop()

    results = run(go(), timeout=180)
    for i, (p, fp, cookie) in enumerate(results):
        assert probe.check(p, fp) == []
        assert cookie == f"who={i}"
        assert fp["main"]["hardwareConcurrency"] == 4 * (i + 1)
    assert len({fp["canvasHash"] for _, fp, _ in results}) == 3


def test_runtime_locale_coherent_across_realms_and_origins(chrome_exe, tmp_path):
    """Runtime CDP locale bridge aligns Window/Worker/SharedWorker Intl without clobbering Accept-Language."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    worker_js = (
        "self.onmessage=()=>self.postMessage({"
        "lang:navigator.language,"
        "langs:Array.from(navigator.languages||[]),"
        "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
        "nf:Intl.NumberFormat().resolvedOptions().locale,"
        "num:(123456.789).toLocaleString()"
        "});"
    )
    shared_js = (
        "onconnect=e=>{const p=e.ports[0];p.onmessage=()=>p.postMessage({"
        "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
        "nf:Intl.NumberFormat().resolvedOptions().locale,"
        "num:(123456.789).toLocaleString()"
        "});p.start();};"
    )
    html = """<!doctype html><script>
window.__firstInline = {
  lang: navigator.language,
  langs: Array.from(navigator.languages || []),
  dtf: Intl.DateTimeFormat().resolvedOptions().locale,
  nf: Intl.NumberFormat().resolvedOptions().locale,
  num: (123456.789).toLocaleString(),
};
window.__seenLocales = [window.__firstInline.dtf];
setInterval(() => {
  const cur = Intl.DateTimeFormat().resolvedOptions().locale;
  if (window.__seenLocales[window.__seenLocales.length - 1] !== cur) {
    window.__seenLocales.push(cur);
  }
}, 1);
window.checkLocale = async () => {
  const h = await (await fetch('/h')).json();
  const w = new Worker('/w.js');
  const worker = await new Promise((res, rej) => {
    w.onmessage = e => res(e.data);
    w.onerror = e => rej(new Error(e.message));
    w.postMessage(1);
  });
  w.terminate();
  const sh = new SharedWorker('/sh.js');
  const shared = await new Promise((res, rej) => {
    sh.port.onmessage = e => res(e.data);
    sh.port.onerror = e => rej(new Error(e.message));
    sh.port.start();
    sh.port.postMessage(1);
  });
  sh.port.close();
  return {
    win: {
      lang: navigator.language,
      langs: Array.from(navigator.languages || []),
      dtf: Intl.DateTimeFormat().resolvedOptions().locale,
      nf: Intl.NumberFormat().resolvedOptions().locale,
      num: (123456.789).toLocaleString(),
    },
    worker,
    shared,
    acceptLang: h['accept-language'],
    firstInline: window.__firstInline,
    seenLocales: window.__seenLocales.slice(),
  };
};
</script>"""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/h":
                body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
                ctype = "application/json"
            elif self.path == "/w.js":
                body, ctype = worker_js.encode(), "text/javascript"
            elif self.path == "/sh.js":
                body, ctype = shared_js.encode(), "text/javascript"
            else:
                body, ctype = html.encode(), "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    async def go():
        srv_a = ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv_b = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv_a.serve_forever, daemon=True).start()
        threading.Thread(target=srv_b.serve_forever, daemon=True).start()
        try:
            eng = ChromeEngine(
                tmp_path / "prof",
                executable=chrome_exe,
                persona=persona(seed=3003, locale="de-DE", timezone="Europe/Berlin"),
                headless=True,
                geoip=False,
            )
            ctx = await eng.start()
            uses_cdp_bridge = eng._cdp_locale_override is not None
            try:
                p1 = ctx.pages[0]
                await p1.goto(f"http://127.0.0.1:{srv_a.server_port}/")
                same_origin = await p1.evaluate("window.checkLocale()")

                # Same-origin popup shares p1's renderer initially, then must recover to de-DE when p1 navigates cross-origin.
                async with ctx.expect_page() as pop1_info:
                    await p1.evaluate("window.open('/popup1')")
                pop1 = await pop1_info.value
                await pop1.wait_for_load_state()
                await p1.goto(f"http://localhost:{srv_b.server_port}/")
                cross_origin = await p1.evaluate("window.checkLocale()")
                await pop1.evaluate("new Promise(r => setTimeout(r, 50))")
                await pop1.wait_for_function(
                    "() => Intl.DateTimeFormat().resolvedOptions().locale === 'de-DE'",
                    timeout=3000,
                )
                popup_after_cross_nav = await pop1.evaluate("window.checkLocale()")
                await pop1.close()

                # Fresh cross-origin rel=noopener popup (p1 is currently on localhost:srv_b).
                async with ctx.expect_page() as pop_no_info:
                    await p1.evaluate(
                        f"window.open('http://127.0.0.1:{srv_a.server_port}/noopener', '_blank', 'noopener')"
                    )
                pop_no = await pop_no_info.value
                await pop_no.wait_for_load_state()
                await pop_no.wait_for_function(
                    "() => Intl.DateTimeFormat().resolvedOptions().locale === 'de-DE'",
                    timeout=3000,
                )
                cross_origin_noopener = await pop_no.evaluate("window.checkLocale()")
                await pop_no.close()

                # Separate tab opens its own same-origin popup, then closes directly without cross-nav.
                p2 = await ctx.new_page()
                await p2.goto(f"http://127.0.0.1:{srv_a.server_port}/")
                second_tab = await p2.evaluate("window.checkLocale()")
                async with ctx.expect_page() as pop2_info:
                    await p2.evaluate("window.open('/popup2')")
                pop2 = await pop2_info.value
                await pop2.wait_for_load_state()
                assert (
                    await pop2.evaluate(
                        "Boolean(window.opener && window.opener.location.origin === location.origin)"
                    )
                    is True
                )
                await p2.close()
                await pop2.evaluate("new Promise(r => setTimeout(r, 50))")
                await pop2.wait_for_function(
                    "() => Intl.DateTimeFormat().resolvedOptions().locale === 'de-DE'",
                    timeout=3000,
                )
                assert await pop2.evaluate("window.opener === null || window.opener.closed") is True
                popup_after_opener_close = await pop2.evaluate("window.checkLocale()")
                await pop2.close()
                return uses_cdp_bridge, {
                    "same_origin": same_origin,
                    "cross_origin": cross_origin,
                    "popup_after_cross_nav": popup_after_cross_nav,
                    "cross_origin_noopener": cross_origin_noopener,
                    "second_tab": second_tab,
                    "popup_after_opener_close": popup_after_opener_close,
                }
            finally:
                await eng.stop()
        finally:
            srv_a.shutdown()
            srv_a.server_close()
            srv_b.shutdown()
            srv_b.server_close()

    uses_cdp_bridge, samples = run(go(), timeout=120)
    expected_langs = ["de-DE", "de", "en-US", "en"]
    for name, sample in samples.items():
        assert sample["acceptLang"] == "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", name
        assert sample["firstInline"]["lang"] == "de-DE", name
        assert sample["firstInline"]["langs"] == expected_langs, name
        assert sample["win"] == {
            "lang": "de-DE",
            "langs": expected_langs,
            "dtf": "de-DE",
            "nf": "de-DE",
            "num": "123.456,789",
        }, name
        assert sample["worker"] == sample["win"], name
        assert sample["shared"] == {"dtf": "de-DE", "nf": "de-DE", "num": "123.456,789"}, name
        assert sample["seenLocales"][-1] == "de-DE", (name, sample["seenLocales"])
        if name in ("popup_after_cross_nav", "popup_after_opener_close"):
            assert sample["firstInline"]["dtf"] == "de-DE", (name, sample["firstInline"])
        if uses_cdp_bridge:
            assert set(sample["seenLocales"]) <= {"de-DE", "en-US"}, (name, sample["seenLocales"])
        else:
            assert sample["firstInline"]["dtf"] == "de-DE", (name, sample["firstInline"])
            assert sample["seenLocales"] == ["de-DE"], (name, sample["seenLocales"])
