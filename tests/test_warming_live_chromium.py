"""Live headless Chromium execution test for human-like Warming Script interactions."""

from __future__ import annotations

import asyncio
import http.server
import os
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from src.services.browser import control
from src.services.browser.launcher import BrowserLauncher
from src.services.profile import manager as pm_mod
from src.services.warming.scheduler import WarmingSchedulerService

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def _start_fixture_site():
    requests_seen: list[str] = []
    keydowns_seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/event":
                qs = parse_qs(parsed.query)
                if "key" in qs:
                    keydowns_seen.append(qs["key"][0])
                self.send_response(204)
                self.end_headers()
                return

            requests_seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if parsed.path == "/":
                body = (
                    "<!doctype html><html><body style='height:2400px'>"
                    "<h1>Fixture Home</h1>"
                    "<form action='/search' method='GET'>"
                    "<input id='searchInput' name='q' type='text'>"
                    "</form>"
                    "</body></html>"
                )
            elif parsed.path == "/search":
                q = parse_qs(parsed.query).get("q", [""])[0]
                body = (
                    "<!doctype html><html><body style='height:2600px'>"
                    f"<div class='results'>Results for: {q}</div>"
                    "<a class='article-link' href='/article'>Read Fixture Article</a>"
                    "<script>"
                    "window.addEventListener('keydown', e => {"
                    "  fetch('/event?key=' + encodeURIComponent(e.key)).catch(() => {});"
                    "});"
                    "</script>"
                    "</body></html>"
                )
            else:
                body = (
                    "<!doctype html><html><body style='height:2400px'>"
                    "<article class='article-body'>Detailed Fixture Article Body</article>"
                    "</body></html>"
                )
            self.wfile.write(body.encode("utf-8"))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, requests_seen, keydowns_seen


@pytest.mark.browser
def test_warming_script_real_chromium_human_interactions(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright

    chrome_exe = next((p for p in _CHROME_CANDIDATES if os.path.exists(p)), None)
    server, site_thread, requests_seen, keydowns_seen = _start_fixture_site()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("real-chrome-warm", "", "macos", engine="chrome")
    ws = WarmingSchedulerService(file_path=str(tmp_path / "warming.json"))

    ready = threading.Event()
    stop_browser = threading.Event()
    bridge: dict[str, object] = {}

    async def _browser_owner():
        async with async_playwright() as pw:
            launch_kwargs: dict[str, object] = {"headless": True}
            if chrome_exe:
                launch_kwargs["executable_path"] = chrome_exe
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as exc:  # pragma: no cover
                bridge["skip"] = str(exc)
                ready.set()
                return
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            runner, port, token = await control.serve(ctx)
            bridge["port"] = port
            bridge["token"] = token
            bridge["page"] = page
            try:
                ready.set()
                while not stop_browser.is_set():
                    await asyncio.sleep(0.05)
                bridge["final_url"] = page.url
                bridge["scroll_y"] = await page.evaluate("window.scrollY")
            finally:
                await runner.cleanup()
                await ctx.close()
                await browser.close()

    def _run_owner():
        asyncio.run(_browser_owner())

    owner_thread = threading.Thread(target=_run_owner, daemon=True)
    owner_thread.start()
    assert ready.wait(15)
    if "skip" in bridge:
        stop_browser.set()
        owner_thread.join(5)
        server.shutdown()
        server.server_close()
        site_thread.join(5)
        pytest.skip(f"Headless Chromium unavailable: {bridge['skip']}")

    bl = BrowserLauncher()
    bl._active_sessions["real-chrome-warm"] = SimpleNamespace(poll=lambda: None)
    bl._controls["real-chrome-warm"] = (int(bridge["port"]), str(bridge["token"]))

    try:
        task = ws.add_task(
            "Real Chromium Human Interaction",
            ["real-chrome-warm"],
            preset="script",
            duration_minutes=2,
            script_steps=[
                {"action": "navigate", "url": f"{base_url}/"},
                {"action": "wait", "seconds": 0.1},
                {
                    "action": "type",
                    "selector": "#searchInput",
                    "text": "RonC warming",
                    "submit": True,
                },
                {"action": "wait_for", "selector": ".results", "timeout": 5000},
                {"action": "scroll", "delta_y": 600, "steps": 6, "humanize": True},
                {"action": "press", "key": "ArrowDown"},
                {"action": "click", "selector": ".article-link"},
                {"action": "wait_for", "selector": ".article-body", "timeout": 5000},
                {"action": "back"},
                {"action": "wait_for", "selector": ".results", "timeout": 5000},
                {"action": "wait", "seconds": 0.15},
            ],
        )
        assert task is not None
        assert ws.execute_task_sync(task.id, pm, bl) is True
        assert task.status == "completed"
        assert task.last_result == "OK (1/1 profiles)"
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_browser.set()
        owner_thread.join(10)
        server.shutdown()
        server.server_close()
        site_thread.join(5)

    assert any(r == "/" for r in requests_seen)
    assert any(r.startswith("/search?q=RonC+warming") for r in requests_seen)
    assert "/article" in requests_seen
    assert "ArrowDown" in keydowns_seen
    assert str(bridge.get("final_url", "")).startswith(f"{base_url}/search")
    assert float(bridge.get("scroll_y", 0)) >= 0
