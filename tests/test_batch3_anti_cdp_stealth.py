"""Tests for Batch 3 (Đợt 3): Anti-CDP Leak, W3C DeviceMemory C++ Seed Alignment,
WebGPU + Client Hints Coherence Probe, and Humanized Behavioral Input Stealth.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chrome_engine import engine as engine_mod
from chrome_engine import persona as personas
from chrome_engine import stealth
from chrome_engine.engine import ChromeEngine, build_args
from chrome_engine.persona import Persona
from src.api.app import create_app
from src.core.container import Container
from src.services.browser.control import PageController


def test_w3c_device_memory_seed_alignment():
    """Verify upstream #100 fix: auto-generated seeds always satisfy seed % 3 == 0."""
    for platform in ("windows", "macos", "linux"):
        for _ in range(30):
            p = personas.generate(platform)
            assert p.seed % 3 == 0
            assert p.has_valid_device_memory_seed

    unaligned = Persona(seed=1337, platform="windows", platform_version="19.0.0")
    assert not unaligned.has_valid_device_memory_seed
    aligned = unaligned.with_aligned_seed()
    assert aligned.has_valid_device_memory_seed
    assert aligned.seed % 3 == 0
    # Persona.to_args() always emits a multiple-of-3 --fingerprint= switch while keeping 5000..5002 distinct
    switches = [
        Persona(seed=s, platform="windows", platform_version="19.0.0").to_args()[0]
        for s in (5000, 5001, 5002)
    ]
    assert len(set(switches)) == 3
    assert all(int(sw.split("=")[1]) % 3 == 0 for sw in switches)


def test_anti_cdp_init_script_and_webgpu_profiles():
    """Verify build_anti_cdp_init_script embeds coherent WebGPU metadata per OS."""
    win_p = Persona(seed=300, platform="windows", platform_version="19.0.0")
    mac_p = Persona(seed=300, platform="macos", platform_version="15.6.1")
    lin_p = Persona(seed=300, platform="linux", platform_version="6.8.0")

    win_script = stealth.build_anti_cdp_init_script(win_p)
    mac_script = stealth.build_anti_cdp_init_script(mac_p)
    lin_script = stealth.build_anti_cdp_init_script(lin_p)

    assert '"platform": "windows"' in win_script
    assert '"vendor": "apple"' not in win_script
    assert '"platform": "macos"' in mac_script
    assert '"vendor": "apple"' in mac_script
    assert '"architecture": "metal-3"' in mac_script
    assert '"platform": "linux"' in lin_script
    assert "clampDeviceMemory" in win_script
    assert "RE_LEAK" in win_script


def test_bezier_curve_and_keystroke_delays():
    """Verify humanized Bezier mouse curve and typing cadence generators."""
    pts_a = stealth.bezier_curve((10.0, 20.0), (400.0, 300.0), steps=12, seed=42)
    pts_b = stealth.bezier_curve((10.0, 20.0), (400.0, 300.0), steps=12, seed=42)
    assert pts_a == pts_b
    assert len(pts_a) == 12
    assert pts_a[-1] == (400.0, 300.0)

    delays = stealth.keystroke_delays_ms("Hello, world!", base_wpm=70, seed=42)
    assert len(delays) == len("Hello, world!")
    assert all(d >= 12 for d in delays)
    assert stealth.keystroke_delays_ms("") == []


def test_chrome_engine_block_webrtc_and_init_script_injection(tmp_path, monkeypatch):
    """Verify ChromeEngine sets WEBRTC_POLICY_PROXY when block_webrtc=True and injects init script."""
    exe = tmp_path / "chrome"
    exe.write_bytes(b"")
    added_scripts: list[str] = []

    class FakeContext:
        pages: list = []

        def on(self, event, cb):
            pass

        async def add_init_script(self, script: str):
            added_scripts.append(script)

        async def close(self):
            pass

    class FakePlaywright:
        class chromium:
            @staticmethod
            async def launch_persistent_context(*args, **kwargs):
                return FakeContext()

        async def stop(self):
            pass

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    import playwright.async_api as pw

    monkeypatch.setattr(pw, "async_playwright", lambda: FakeStarter())
    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    from chrome_engine import search

    async def no_search(context):
        pass

    monkeypatch.setattr(search, "initialize_search", no_search)

    profile_dir = tmp_path / "prof"
    eng = ChromeEngine(
        profile_dir,
        platform="macos",
        executable=exe,
        block_webrtc=True,
        stealth_js=True,
    )
    asyncio.run(eng.start())
    try:
        prefs = json.loads((profile_dir / "Default" / "Preferences").read_text("utf-8"))
        assert prefs["webrtc"]["ip_handling_policy"] == engine_mod.WEBRTC_POLICY_PROXY
        assert len(added_scripts) == 1
        assert "clampDeviceMemory" in added_scripts[0]
        assert "--disable-blink-features=AutomationControlled" not in build_args(eng.persona)
        assert "--disable-blink-features=AutomationControlled" in engine_mod.IGNORED_DEFAULT_ARGS
    finally:
        asyncio.run(eng.stop())


def test_chrome_runner_passes_block_webrtc_when_configured(monkeypatch):
    """Verify chrome_engine.runner forwards block_webrtc=True when webrtc_mode == 'disable'."""
    import chrome_engine.runner as runner

    captured_kwargs: dict = {}

    async def run():
        page = SimpleNamespace(on=lambda *args: None, is_closed=lambda: False, url="about:blank")
        context = SimpleNamespace(pages=[page], on=lambda *args: None)
        engine = SimpleNamespace(
            start=AsyncMock(return_value=context), stop=AsyncMock(), warnings=[]
        )

        def fake_chrome_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return engine

        monkeypatch.setattr(runner, "ChromeEngine", fake_chrome_engine)
        monkeypatch.setattr(runner, "ensure_installed", AsyncMock())
        monkeypatch.setattr(runner, "RESTORE_TABS", False)
        monkeypatch.setattr(
            runner,
            "_read_profile_extra",
            lambda name: {"webrtc_mode": "disable", "start_url": ""},
        )
        monkeypatch.setattr(
            runner.control,
            "serve",
            AsyncMock(return_value=(SimpleNamespace(cleanup=AsyncMock()), 1, "tok")),
        )
        shutdown = asyncio.Event()
        shutdown.set()
        monkeypatch.setattr(runner, "_shutdown", shutdown)
        assert await runner.run_browser("fixture", "None", "macos") == 0
        assert captured_kwargs.get("block_webrtc") is True

    asyncio.run(run())


def test_page_controller_humanized_click_type_and_scroll():
    """Verify PageController supports humanized click_at, keyboard_type, and scroll actions."""
    moves: list[tuple[float, float]] = []
    clicks: list[tuple[float, float]] = []
    wheels: list[tuple[float, float]] = []
    typed: list[str] = []

    class FakeMouse:
        async def move(self, x: float, y: float):
            moves.append((x, y))

        async def click(self, x: float, y: float):
            clicks.append((x, y))

        async def wheel(self, dx: float, dy: float):
            wheels.append((dx, dy))

    class FakeKeyboard:
        async def type(self, text: str):
            typed.append(text)

    class RichFakePage:
        url = "https://example.test/"
        mouse = FakeMouse()
        keyboard = FakeKeyboard()

        def is_closed(self):
            return False

        async def title(self):
            return "Rich"

    ctx = SimpleNamespace(pages=[RichFakePage()])
    controller = PageController(ctx)

    res_click = asyncio.run(
        controller.dispatch("click_at", {"x": 240.0, "y": 180.0, "humanize": True})
    )
    assert res_click["url"] == "https://example.test/"
    assert len(moves) >= 2
    assert moves[-1] == (240.0, 180.0)
    assert clicks == [(240.0, 180.0)]

    res_type = asyncio.run(controller.dispatch("keyboard_type", {"text": "abc", "humanize": True}))
    assert res_type["title"] == "Rich"
    assert "".join(typed) == "abc"

    # Verify proportional cadence scaling in human_type preserves non-uniform rhythm
    sleeps: list[float] = []
    orig_sleep = stealth.asyncio.sleep

    async def record_sleep(sec: float):
        sleeps.append(sec)

    stealth.asyncio.sleep = record_sleep
    try:
        sample_delays = stealth.keystroke_delays_ms("Hello, world!", seed=42)
        asyncio.run(
            stealth.human_type(
                RichFakePage(), "Hello, world!", delays_ms=sample_delays, max_total_sleep=2.5
            )
        )
        assert len(set(sleeps)) > 5
        assert sum(sleeps) <= 2.501
    finally:
        stealth.asyncio.sleep = orig_sleep

    res_scroll = asyncio.run(
        controller.dispatch("scroll", {"delta_y": 300.0, "delta_x": 0.0, "steps": 5})
    )
    assert res_scroll["url"] == "https://example.test/"
    assert len(wheels) == 5
    assert sum(dy for _, dy in wheels) == pytest.approx(300.0)


def test_api_scroll_route_and_mcp_scroll_tool(tmp_path, monkeypatch):
    """Verify FastAPI /api/v1/browser/{name}/page/scroll and MCP browser_scroll tool."""
    import src.services.profile.manager as pm_mod
    from src.core import config as app_config
    from src.mcp_server.server import mcp

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))

    container = Container()
    container.profile_manager.add_profile("scroll-prof", "", "macos", engine="chrome")

    captured_control: list[tuple[str, str, dict]] = []

    def fake_control(name: str, action: str, params: dict | None = None, timeout: float = 30.0):
        captured_control.append((name, action, params or {}))
        return {"url": "https://example.test/", "title": "Scrolled"}

    monkeypatch.setattr(container.browser_launcher, "control", fake_control)

    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        res = client.post(
            "/api/v1/browser/scroll-prof/page/scroll",
            json={"delta_y": 420.0, "delta_x": 0.0, "steps": 7, "humanize": True},
        )
        assert res.status_code == 200
        assert res.json()["title"] == "Scrolled"
        assert captured_control == [
            (
                "scroll-prof",
                "scroll",
                {"delta_y": 420.0, "delta_x": 0.0, "steps": 7, "humanize": True},
            )
        ]

    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "browser_scroll" in tool_names


def test_worker_variants_base_url_module_tla_and_shared_worker_semantics():
    """Verify R2-B3-F2a/b/c: classic URL worker relative deps/fetch/nested worker,
    module worker top-level await & import.meta.url, and URL SharedWorker GPU + shared instance identity.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    exe = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not exe.exists():
        pytest.skip("stock Chrome unavailable")

    scripts = {
        "/sub/classic.js": (
            "try {"
            " importScripts('./dep.js');"
            " fetch('./dep.js').then(r => r.text()).then(txt => {"
            "   const nw = new Worker('./nested.js');"
            "   nw.onmessage = (e) => postMessage({"
            "     value: self.depValue,"
            "     fetched: txt.includes('depValue'),"
            "     pathname: self.location.pathname,"
            "     nested: e.data,"
            "   });"
            " });"
            "} catch (e) { postMessage({error: String(e)}); }"
        ),
        "/sub/dep.js": "self.depValue = 42;",
        "/sub/nested.js": (
            "importScripts('./dep.js');"
            "(async () => {"
            "  const a = await navigator.gpu.requestAdapter();"
            "  postMessage({val: self.depValue + 8, vendor: a ? a.info.vendor : null});"
            "})();"
        ),
        "/sub/module.js": (
            "import { bonus } from './mod_dep.js';"
            "const a = await navigator.gpu.requestAdapter();"
            "postMessage({vendor: a ? a.info.vendor : null, bonus, metaPath: new URL(import.meta.url).pathname});"
        ),
        "/sub/mod_dep.js": (
            "const a = await navigator.gpu.requestAdapter();"
            "export const bonus = a ? a.info.vendor : 'none';"
        ),
        "/shared.js": (
            "let connects = 0;"
            "onconnect = (e) => {"
            "  connects += 1;"
            "  const p = e.ports[0];"
            "  p.onmessage = async () => {"
            "    const a = await navigator.gpu.requestAdapter();"
            "    p.postMessage({vendor: a ? a.info.vendor : null, connects, pathname: self.location.pathname});"
            "  };"
            "  p.start();"
            "};"
        ),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = scripts.get(self.path, "<html><body>ok</body></html>")
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/javascript" if self.path in scripts else "text/html",
            )
            if self.path == "/csp":
                self.send_header("Content-Security-Policy", "worker-src 'self' blob:")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(exe), headless=True)
            try:
                ctx = browser.new_context()
                ctx.add_init_script(
                    stealth.build_anti_cdp_init_script(
                        Persona(seed=300, platform="windows", platform_version="19.0.0")
                    )
                )
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/")

                main_vendor = page.evaluate(
                    "async () => { const a = await navigator.gpu.requestAdapter(); return a ? a.info.vendor : null; }"
                )
                classic_res = page.evaluate(
                    """async () => {
                      const w = new Worker('/sub/classic.js');
                      try {
                        return await new Promise((resolve) => {
                          const t = setTimeout(() => resolve({error: 'classic timeout'}), 3000);
                          w.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
                          w.onerror = (e) => { clearTimeout(t); resolve({error: e.message || 'err'}); };
                        });
                      } finally { w.terminate(); }
                    }"""
                )
                assert classic_res == {
                    "value": 42,
                    "fetched": True,
                    "pathname": "/sub/classic.js",
                    "nested": {"val": 50, "vendor": main_vendor},
                }, classic_res

                mod_res = page.evaluate(
                    """async () => {
                      const w = new Worker('/sub/module.js', {type: 'module'});
                      try {
                        return await new Promise((resolve) => {
                          const t = setTimeout(() => resolve({error: 'module timeout'}), 3000);
                          w.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
                          w.onerror = (e) => { clearTimeout(t); resolve({error: e.message || 'err'}); };
                        });
                      } finally { w.terminate(); }
                    }"""
                )
                assert mod_res.get("vendor") == main_vendor, mod_res
                assert mod_res.get("bonus") == main_vendor, mod_res
                assert mod_res.get("metaPath") == "/sub/module.js", mod_res

                shared_res = page.evaluate(
                    """async () => {
                      const ask = (path, name) => new Promise((resolve) => {
                        const t = setTimeout(() => resolve({error: 'shared timeout'}), 3000);
                        const sw = new SharedWorker(path, {name});
                        sw.port.onmessage = (e) => { clearTimeout(t); sw.port.close(); resolve(e.data); };
                        sw.onerror = (e) => { clearTimeout(t); resolve({error: e.message || 'sw err'}); };
                        sw.port.start();
                        sw.port.postMessage('go');
                      });
                      const first = await ask('/shared.js', 'pool-a');
                      const second = await ask('shared.js', 'pool-a');
                      const other = await ask('/shared.js', 'pool-b');
                      return {first, second, other};
                    }"""
                )
                assert shared_res["first"]["vendor"] == main_vendor
                assert shared_res["first"]["connects"] == 1
                assert shared_res["first"]["pathname"] == "/shared.js"
                assert shared_res["second"]["vendor"] == main_vendor
                assert shared_res["second"]["connects"] == 2
                assert shared_res["other"]["connects"] == 1

                # null is an empty options dictionary, not the legacy name "null".
                default_options = page.evaluate(
                    """async () => {
                      const url = URL.createObjectURL(new Blob([
                        'let n=0;onconnect=e=>{const i=++n;const p=e.ports[0];p.onmessage=()=>p.postMessage({name:self.name,connection:i});p.start();};'
                      ], {type: 'text/javascript'}));
                      const results = [];
                      for (const options of [null, undefined, {}, '', 'null']) {
                        const sw = new SharedWorker(url, options);
                        (globalThis.defaultOptionWorkers ||= []).push(sw);
                        results.push(await new Promise(resolve => {
                          const t = setTimeout(() => resolve({error: 'timeout'}), 3000);
                          sw.onerror = () => { clearTimeout(t); resolve({error: 'worker error'}); };
                          sw.port.onmessage = e => { clearTimeout(t); resolve(e.data); };
                          sw.port.start(); sw.port.postMessage('name');
                        }));
                      }
                      return results;
                    }"""
                )
                assert default_options == [{"name": "", "connection": i} for i in range(1, 5)] + [
                    {"name": "null", "connection": 1}
                ], default_options

                # Verify R4-B3-F1: zero stealth registry keys or markers in web-observable storage
                # and R5-B3-F2: synchronous TypeError on invalid SharedWorker options
                storage_probe = page.evaluate(
                    """() => {
                      let illegalPortThrows = false;
                      try { Reflect.get(SharedWorker.prototype, 'port', {}); } catch (e) {
                        illegalPortThrows = e instanceof TypeError;
                      }
                      const invalidOptionErrors = [];
                      for (const opts of [{type: 'invalid'}, {credentials: 'invalid'}]) {
                        try {
                          new SharedWorker('/shared.js', opts);
                          invalidOptionErrors.push('accepted');
                        } catch (e) {
                          invalidOptionErrors.push(e.name);
                        }
                      }
                      return {
                        lsKeys: Object.keys(localStorage),
                        lsOwnKeys: Reflect.ownKeys(localStorage),
                        lsFirst: localStorage.key(0),
                        lsLen: localStorage.length,
                        ssKeys: Object.keys(sessionStorage),
                        illegalPortThrows,
                        invalidOptionErrors,
                      };
                    }"""
                )
                assert storage_probe == {
                    "lsKeys": [],
                    "lsOwnKeys": [],
                    "lsFirst": None,
                    "lsLen": 0,
                    "ssKeys": [],
                    "illegalPortThrows": True,
                    "invalidOptionErrors": ["TypeError", "TypeError"],
                }, storage_probe

                # Ponytail Finding #8 (and Codex Re-Review #1 & #2):
                # 1. Binary/image Blobs are ignored by blobMap, and the oldest still-valid script Blob URL
                #    retains its realm stealth bootstrap even after >64 subsequent script Blob URLs are created.
                # 2. Closing SharedWorker ports releases connection references so activeSharedRefs does not grow unboundedly.
                blob_churn_ok = page.evaluate(
                    """async () => {
                      const askGpu = async (u) => {
                        const w = new Worker(u);
                        return await new Promise((resolve) => {
                          const t = setTimeout(() => resolve('timeout'), 3000);
                          w.onmessage = (e) => { clearTimeout(t); w.terminate(); resolve(e.data); };
                          w.postMessage('go');
                        });
                      };
                      const code = "onmessage = async () => { const a = await navigator.gpu.requestAdapter(); postMessage(a ? a.info.vendor : null); };";
                      const firstUrl = URL.createObjectURL(new Blob([code], {type: 'text/javascript'}));
                      const beforeVendor = await askGpu(firstUrl);
                      for (let i = 0; i < 80; i++) {
                        const imgUrl = URL.createObjectURL(new Blob([new Uint8Array(64)], {type: 'image/png'}));
                        URL.revokeObjectURL(imgUrl);
                      }
                      let lastUrl = '';
                      for (let i = 0; i < 70; i++) {
                        lastUrl = URL.createObjectURL(new Blob([code], {type: 'text/javascript'}));
                      }
                      const afterFirstVendor = await askGpu(firstUrl);
                      const afterLastVendor = await askGpu(lastUrl);
                      const mainAdapter = await navigator.gpu.requestAdapter();
                      const mainVendor = mainAdapter ? mainAdapter.info.vendor : null;
                      return (
                        beforeVendor === mainVendor &&
                        afterFirstVendor === mainVendor &&
                        afterLastVendor === mainVendor
                      );
                    }"""
                )
                assert blob_churn_ok is True

                ref_ctx = browser.new_context()
                try:
                    ref_script = stealth.build_anti_cdp_init_script(
                        Persona(seed=300, platform="windows", platform_version="19.0.0")
                    ).replace(
                        "const activeSharedRefs = [];",
                        "const activeSharedRefs = []; globalThis.__reviewRefs = activeSharedRefs;",
                    )
                    ref_ctx.add_init_script(ref_script)
                    ref_page = ref_ctx.new_page()
                    ref_page.goto(f"http://127.0.0.1:{server.server_port}/")
                    close_res = ref_page.evaluate(
                        """async () => {
                          const frame = document.createElement('iframe');
                          document.body.append(frame);
                          for (let i = 0; i < 6; i++) {
                            const w = new SharedWorker('/shared.js', {name: 'close-check-' + i});
                            await new Promise((resolve, reject) => {
                              const t = setTimeout(() => reject(new Error('timeout')), 2000);
                              w.port.onmessage = () => { clearTimeout(t); resolve(); };
                              w.port.start();
                              w.port.postMessage('go');
                            });
                            if (i % 2 === 0) {
                              w.port.close();
                            } else {
                              frame.contentWindow.MessagePort.prototype.close.call(w.port);
                            }
                          }
                          await new Promise((r) => setTimeout(r, 40));
                          let illegalCloseErr = '';
                          try {
                            frame.contentWindow.MessagePort.prototype.close.call({});
                          } catch (e) {
                            illegalCloseErr = e.name;
                          }
                          return {refs: globalThis.__reviewRefs.length, illegalCloseErr};
                        }"""
                    )
                    assert close_res == {"refs": 0, "illegalCloseErr": "TypeError"}, close_res
                finally:
                    ref_ctx.close()

                # Verify R5-B3-F1: same-origin SharedWorker works under Content-Security-Policy: worker-src 'self' blob:
                # and R6-B3-F1: page BroadcastChannel cannot observe or query internal coordination channel
                csp_page = ctx.new_page()
                try:
                    csp_page.goto(f"http://127.0.0.1:{server.server_port}/csp")
                    csp_res = csp_page.evaluate(
                        """async () => {
                          const bcMessages = [];
                          let toStringCalls = 0;
                          let symbolBcThrows = false;
                          try { new BroadcastChannel(Symbol('x')); } catch (e) {
                            symbolBcThrows = e instanceof TypeError;
                          }
                          const protoCtorMatches = (
                            BroadcastChannel.prototype.constructor === BroadcastChannel &&
                            Worker.prototype.constructor === Worker &&
                            SharedWorker.prototype.constructor === SharedWorker
                          );
                          let illegalBcNameThrows = false;
                          try { Reflect.get(BroadcastChannel.prototype, 'name', {}); } catch (e) {
                            illegalBcNameThrows = e instanceof TypeError;
                          }
                          const ch1 = new BroadcastChannel('__fp_sw_bc_v1__');
                          const ch3 = new BroadcastChannel.prototype.constructor('\\x00__fp_bc_sw');
                          const ch4 = new BroadcastChannel({
                            toString() {
                              return toStringCalls++ === 0 ? 'ordinary-channel' : '\\x00__fp_bc_sw';
                            }
                          });
                          for (const c of [ch1, ch3, ch4]) {
                            c.onmessage = (e) => bcMessages.push(e.data);
                          }
                          const sw = new SharedWorker('/shared.js', {name: 'csp-pool'});
                          const data = await new Promise((resolve) => {
                            const t = setTimeout(() => resolve({error: 'csp timeout'}), 3000);
                            sw.onerror = (e) => { clearTimeout(t); resolve({error: e.message || 'csp err'}); };
                            sw.port.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
                            sw.port.start();
                            sw.port.postMessage('go');
                          });
                          for (const c of [ch1, ch3, ch4]) {
                            c.postMessage({type: 'sync_req'});
                          }
                          await new Promise((r) => setTimeout(r, 60));
                          const ch2 = new BroadcastChannel('\\x00__fp_bc_sw');
                          const names = [ch1.name, ch2.name, ch3.name, ch4.name];
                          for (const c of [ch1, ch2, ch3, ch4]) {
                            c.close();
                          }
                          return {
                            data,
                            bcMessages,
                            names,
                            toStringCalls,
                            symbolBcThrows,
                            protoCtorMatches,
                            illegalBcNameThrows,
                          };
                        }"""
                    )
                    assert csp_res["data"]["vendor"] == main_vendor, csp_res
                    assert csp_res["data"]["connects"] == 1, csp_res
                    assert csp_res["bcMessages"] == [], csp_res
                    assert csp_res["names"] == [
                        "__fp_sw_bc_v1__",
                        "\x00__fp_bc_sw",
                        "\x00__fp_bc_sw",
                        "ordinary-channel",
                    ], csp_res
                    assert csp_res["toStringCalls"] == 1, csp_res
                    assert csp_res["symbolBcThrows"] is True, csp_res
                    assert csp_res["protoCtorMatches"] is True, csp_res
                    assert csp_res["illegalBcNameThrows"] is True, csp_res
                finally:
                    csp_page.close()

                # Verify R3-B3-F2c: a second page in the same BrowserContext shares the same SharedWorker instance
                page2 = ctx.new_page()
                page2.goto(f"http://127.0.0.1:{server.server_port}/")
                shared_page2 = page2.evaluate(
                    """async () => {
                      const t = setTimeout(() => null, 3000);
                      const sw = new SharedWorker('/shared.js', {name: 'pool-a'});
                      (globalThis.heldWorkers ||= []).push(sw);
                      return await new Promise((resolve) => {
                        sw.port.onmessage = (e) => { clearTimeout(t); resolve(e.data); };
                        sw.onerror = (e) => { clearTimeout(t); resolve({error: e.message || 'sw2 err'}); };
                        sw.port.start();
                        sw.port.postMessage('go');
                      });
                    }"""
                )
                assert shared_page2["vendor"] == main_vendor
                assert shared_page2["connects"] == 3

                # Verify creator-tab-close resilience: close page1 while page2 holds pool-a, then join from page3
                page.close()
                page3 = ctx.new_page()
                page3.goto(f"http://127.0.0.1:{server.server_port}/")
                shared_page3 = page3.evaluate(
                    """async () => {
                      const sw = new SharedWorker('/shared.js', {name: 'pool-a'});
                      return await new Promise((resolve) => {
                        sw.port.onmessage = (e) => resolve(e.data);
                        sw.port.start();
                        sw.port.postMessage('go');
                      });
                    }"""
                )
                assert shared_page3["vendor"] == main_vendor
                assert shared_page3["connects"] == 4

                # Isolated BrowserContext (separate profile/storage partition) does not share the worker
                ctx2 = browser.new_context()
                try:
                    ctx2.add_init_script(
                        stealth.build_anti_cdp_init_script(
                            Persona(seed=300, platform="windows", platform_version="19.0.0")
                        )
                    )
                    iso_page = ctx2.new_page()
                    iso_page.goto(f"http://127.0.0.1:{server.server_port}/")
                    iso_res = iso_page.evaluate(
                        """async () => {
                          const sw = new SharedWorker('/shared.js', {name: 'pool-a'});
                          return await new Promise((resolve) => {
                            sw.port.onmessage = (e) => resolve(e.data);
                            sw.port.start();
                            sw.port.postMessage('go');
                          });
                        }"""
                    )
                    assert iso_res["connects"] == 1
                finally:
                    ctx2.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
