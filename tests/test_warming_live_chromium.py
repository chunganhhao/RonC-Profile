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
    inbox_scrolls_before_click: list[float] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/event":
                qs = parse_qs(parsed.query)
                if "key" in qs:
                    keydowns_seen.append(qs["key"][0])
                if "inbox_scroll" in qs and "/mail/1" not in requests_seen:
                    inbox_scrolls_before_click.append(float(qs["inbox_scroll"][0]))
                self.send_response(204)
                self.end_headers()
                return

            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if parsed.path in ("/session-expire-inbox", "/return-expire-inbox") and (
                parsed.path in requests_seen
            ):
                requests_seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/signed-out")
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
            elif parsed.path == "/older":
                body = "<!doctype html><html><body><h1>Older History</h1></body></html>"
            elif parsed.path == "/previous":
                body = "<!doctype html><html><body><h1>Previous History</h1></body></html>"
            elif parsed.path == "/signed-out":
                body = (
                    "<!doctype html><html><body>"
                    "<h1>Sign in to continue</h1><form><input name='email'></form>"
                    "</body></html>"
                )
            elif parsed.path == "/signed-out-table":
                body = (
                    "<!doctype html><html><body>"
                    "<div role='main'><table><tbody>"
                    "<tr><td>Sign in to your account</td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/gmail-empty":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr><td class='TC'>Your Primary tab is empty.</td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/click2-pending-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/mail/1'\">"
                    "<span class='bog'>Click2 Pending Row 1</span></td></tr>"
                    "<tr class='zA'><td class='a4W' onclick='armSignoutOnScroll()'>"
                    "<span class='bog'>Click2 Pending Row 2</span></td></tr>"
                    "</tbody></table></div>"
                    "<script>"
                    "function armSignoutOnScroll() {"
                    "  fetch('/event?key=click2_executed').catch(() => {});"
                    "  window.addEventListener('wheel', () => {"
                    "    fetch('/event?key=armed_scroll').catch(() => {});"
                    "    location.href = '/signed-out';"
                    "  }, { once: true });"
                    "}"
                    "</script></body></html>"
                )
            elif parsed.path == "/one-mail-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/mail/1'\">"
                    "<span class='bog'>Only One Email</span></td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/recoverable-signout-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/signed-out'\">"
                    "<span class='bog'>Recoverable Signout Row 1</span></td></tr>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/signed-out'\">"
                    "<span class='bog'>Recoverable Signout Row 2</span></td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/scroll-drift-inbox":
                drift_script = (
                    "<script>"
                    "window.addEventListener('wheel', () => {"
                    "  if (!location.pathname.endsWith('/other-message')) {"
                    "    history.pushState(null, '', '/other-message');"
                    "    document.getElementById('inbox-pane').innerHTML = "
                    "      \"<h2 class='hP'>Other Message</h2><div data-message-id='other' class='a3s'>Drifted</div>\";"
                    "  }"
                    "}, { once: true });"
                    "</script>"
                    if requests_seen.count("/scroll-drift-inbox") >= 3
                    else ""
                )
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    + "".join(
                        f"<tr class='zA'><td class='a4W' onclick=\"location.href='/mail/{i}'\">"
                        f"<span class='bog'>Drift Subject {i}</span></td></tr>"
                        for i in range(1, 6)
                    )
                    + "</tbody></table></div>"
                    + drift_script
                    + "</body></html>"
                )
            elif parsed.path == "/session-expire-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/signed-out'\">"
                    "<span class='bog'>Session Expiring Subject 1</span></td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/return-expire-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    "<tr class='zA'><td class='a4W' onclick=\"location.href='/mail/1'\">"
                    "<span class='bog'>Return Expiring Subject 1</span></td></tr>"
                    "</tbody></table></div></body></html>"
                )
            elif parsed.path == "/replace-inbox":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "tr.zA td{height:100px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    + "".join(
                        f"<tr class='zA'><td class='a4W' onclick=\"openReplace({i})\">"
                        f"<span class='bog'>Replace Subject {i}</span></td></tr>"
                        for i in range(1, 6)
                    )
                    + "</tbody></table></div>"
                    "<script>"
                    "function openReplace(i) {"
                    "  history.replaceState(null, '', '/mail/' + i);"
                    "  fetch('/event?key=replace_' + i).catch(() => {});"
                    "  document.getElementById('inbox-pane').innerHTML = "
                    '    "<h2 class=\'hP\'>Replace Subject " + i + "</h2>" + '
                    "    \"<div data-message-id='replace-\" + i + \"' class='a3s mail-body' style='height:1800px'>Replaced /mail/\" + i + \"</div>\";"
                    "}"
                    "</script></body></html>"
                )
            elif parsed.path == "/gmail":
                body = (
                    "<!doctype html><html><head><style>"
                    "html,body{margin:0;height:100vh;overflow:hidden;display:flex;flex-direction:column}"
                    ".top-bar{height:56px;flex:none;background:#eee}"
                    ".layout{display:flex;flex:1;min-height:0}"
                    ".side{width:200px;flex:none;background:#f5f5f5}"
                    "div[role='main']{flex:1;overflow-y:auto;height:100%}"
                    "table{width:100%;border-collapse:collapse}"
                    "tr.zA td{height:120px;border-bottom:1px solid #ddd;cursor:pointer}"
                    "</style></head><body>"
                    "<div class='top-bar'>Mail Header</div>"
                    "<div class='layout'><div class='side'>Inbox</div>"
                    "<div role='main' id='inbox-pane'><table><tbody>"
                    + "".join(
                        f"<tr class='zA'><td class='a4W' onclick=\"location.href='/mail/{i}'\">"
                        f"<span class='bog'>Subject {i}</span></td></tr>"
                        for i in range(1, 14)
                    )
                    + "</tbody></table></div></div>"
                    "<script>"
                    "const pane = document.getElementById('inbox-pane');"
                    "pane.addEventListener('scroll', () => {"
                    "  if (pane.scrollTop > 0) {"
                    "    fetch('/event?inbox_scroll=' + pane.scrollTop).catch(() => {});"
                    "  }"
                    "}, { once: true });"
                    "</script></body></html>"
                )
            elif parsed.path.startswith("/mail/"):
                msg_id = parsed.path.rsplit("/", 1)[-1]
                body = (
                    "<!doctype html><html><body style='height:2400px'>"
                    "<div role='main'>"
                    f"<h2 class='hP'>Subject {msg_id}</h2>"
                    f"<div data-message-id='msg-{msg_id}' class='a3s mail-body'>Opened {parsed.path}</div>"
                    "</div></body></html>"
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
    return server, thread, requests_seen, keydowns_seen, inbox_scrolls_before_click


@pytest.mark.browser
def test_warming_script_real_chromium_human_interactions(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright

    chrome_exe = next((p for p in _CHROME_CANDIDATES if os.path.exists(p)), None)
    server, site_thread, requests_seen, keydowns_seen, inbox_scrolls_before_click = (
        _start_fixture_site()
    )
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
                bridge["inbox_scroll_top"] = await page.evaluate(
                    "() => { const el = document.querySelector('#inbox-pane'); return el ? el.scrollTop : -1; }"
                )
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
                # Optional missing banner must skip cleanly (PreActionTimeoutError -> unknown_state=False)
                {
                    "action": "click",
                    "selector": "button#nonexistentConsent:visible",
                    "timeout": 400,
                    "optional": True,
                },
                {
                    "action": "type",
                    "selector": "#searchInput",
                    "texts": ["giá vàng hôm nay"],
                    "clear": False,
                    "submit": True,
                },
                {"action": "wait_for", "selector": ".results", "timeout": 5000},
                {"action": "scroll", "delta_y": 600, "steps": 6, "humanize": True},
                {"action": "press", "key": "ArrowDown"},
                {"action": "click", "selector": ".article-link"},
                {"action": "wait_for", "selector": ".article-body", "timeout": 5000},
                {"action": "back"},
                {"action": "wait_for", "selector": ".results", "timeout": 5000},
                # Gmail-like SPA with overflow:hidden body & nested scrollable inbox pane
                {"action": "navigate", "url": f"{base_url}/gmail"},
                {"action": "wait_for", "selector": "tr.zA:visible", "timeout": 5000},
                {"action": "scroll", "delta_y": 500, "steps": 6, "humanize": True},
                {"action": "click", "selector": "tr.zA td.a4W:visible", "timeout": 5000},
                {"action": "wait_for", "selector": ".mail-body", "timeout": 5000},
                {"action": "back"},
                {"action": "wait_for", "selector": "tr.zA:visible", "timeout": 5000},
                {"action": "scroll", "delta_y": 420, "steps": 6, "humanize": True},
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
    assert any(r.startswith("/search?q=") and "v%C3%A0ng" in r for r in requests_seen), (
        requests_seen
    )
    assert "/article" in requests_seen
    assert "/gmail" in requests_seen
    assert "/mail/1" in requests_seen
    assert "ArrowDown" in keydowns_seen
    assert str(bridge.get("final_url", "")).startswith(f"{base_url}/gmail")
    assert float(bridge.get("inbox_scroll_top", 0)) > 0
    # First scroll on /gmail (right after top-left .article-link click on /search, before clicking any mail) must scroll #inbox-pane
    assert any(s > 0 for s in inbox_scrolls_before_click), inbox_scrolls_before_click


@pytest.mark.browser
def test_codex_s7_1_gmail_signed_out_and_empty_inbox_never_back_into_prior_history(
    tmp_path, monkeypatch
):
    from playwright.async_api import async_playwright

    from src.services.warming import scheduler as sched_mod

    chrome_exe = next((p for p in _CHROME_CANDIDATES if os.path.exists(p)), None)
    server, site_thread, requests_seen, _keydowns, _inbox_scrolls = _start_fixture_site()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("gmail-hist-prof", "", "macos", engine="chrome")
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
            # Prefill prior browser history on a reused tab: /older -> /previous
            await page.goto(f"{base_url}/older", wait_until="domcontentloaded")
            await page.goto(f"{base_url}/previous", wait_until="domcontentloaded")
            runner, port, token = await control.serve(ctx)
            bridge["port"] = port
            bridge["token"] = token
            bridge["page"] = page
            try:
                ready.set()
                while not stop_browser.is_set():
                    await asyncio.sleep(0.05)
                bridge["final_url"] = page.url
            finally:
                await runner.cleanup()
                await ctx.close()
                await browser.close()

    owner_thread = threading.Thread(target=lambda: asyncio.run(_browser_owner()), daemon=True)
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
    bl._active_sessions["gmail-hist-prof"] = SimpleNamespace(poll=lambda: None)
    bl._controls["gmail-hist-prof"] = (int(bridge["port"]), str(bridge["token"]))

    def _short_gmail_playbook(entry_url: str) -> list[dict[str, object]]:
        steps = []
        for st in sched_mod.WARMING_SCENARIO_PLAYBOOKS["gmail_trust"]:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = entry_url
            elif "timeout" in s:
                s["timeout"] = 400
            steps.append(s)
        return steps

    orig_playbooks = dict(sched_mod.WARMING_SCENARIO_PLAYBOOKS)
    try:
        # 1. Signed-out page: fails cleanly, never goes Back into /previous or /older
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {**orig_playbooks, "gmail_trust": _short_gmail_playbook(f"{base_url}/signed-out")},
        )
        t_out = ws.add_task("Signed Out Gmail", ["gmail-hist-prof"], preset="gmail_trust")
        assert ws.execute_task_sync(t_out.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_out.status == "failed"
        assert "No messages" not in t_out.last_result
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/signed-out")

        # 1b. Codex §9 #2: Sign-in page with generic div[role='main'] table must fail (never "No messages")
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {
                **orig_playbooks,
                "gmail_trust": _short_gmail_playbook(f"{base_url}/signed-out-table"),
            },
        )
        t_out_tbl = ws.add_task("Signed Out Table Gmail", ["gmail-hist-prof"], preset="gmail_trust")
        assert ws.execute_task_sync(t_out_tbl.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_out_tbl.status == "failed"
        assert "No messages" not in t_out_tbl.last_result
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/signed-out-table")

        # 1c. Codex §11 #1: Session expires during Click 1 (valid inbox row -> /signed-out, subsequent /inbox -> 302 /signed-out)
        # Must fail immediately with zero message loads and no further inbox/message actions after session loss
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {
                **orig_playbooks,
                "gmail_trust": _short_gmail_playbook(f"{base_url}/session-expire-inbox"),
            },
        )
        t_exp_click = ws.add_task(
            "Session Expire Click Gmail", ["gmail-hist-prof"], preset="gmail_trust"
        )
        assert ws.execute_task_sync(t_exp_click.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_exp_click.status == "failed"
        assert "OK" not in t_exp_click.last_result and "No messages" not in t_exp_click.last_result
        assert requests_seen == ["/session-expire-inbox", "/signed-out"], requests_seen
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/signed-out")

        # 1d. Codex §11 #1: Session expires on Return 1 (/mail/1 opened, Return 1 redirects -> /signed-out)
        # Must fail immediately on return verification with zero Click 2 / further message actions
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {
                **orig_playbooks,
                "gmail_trust": _short_gmail_playbook(f"{base_url}/return-expire-inbox"),
            },
        )
        t_exp_ret = ws.add_task(
            "Session Expire Return Gmail", ["gmail-hist-prof"], preset="gmail_trust"
        )
        assert ws.execute_task_sync(t_exp_ret.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_exp_ret.status == "failed"
        assert "OK" not in t_exp_ret.last_result and "No messages" not in t_exp_ret.last_result
        assert requests_seen == [
            "/return-expire-inbox",
            "/mail/1",
            "/return-expire-inbox",
            "/signed-out",
        ], requests_seen
        assert "/mail/2" not in requests_seen
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/signed-out")

        # 2. Verified empty inbox: reports "No messages", never goes Back into /previous or /older
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {**orig_playbooks, "gmail_trust": _short_gmail_playbook(f"{base_url}/gmail-empty")},
        )
        t_empty = ws.add_task("Empty Gmail", ["gmail-hist-prof"], preset="gmail_trust")
        assert ws.execute_task_sync(t_empty.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_empty.status == "completed"
        assert t_empty.last_result == "No messages (1/1 profiles)"
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/gmail-empty")

        # 2b. Codex §9 #1: history.replaceState message click on reused tab never pops into /previous or /older
        requests_seen.clear()
        _keydowns.clear()
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/older"})
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/previous"})
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {**orig_playbooks, "gmail_trust": _short_gmail_playbook(f"{base_url}/replace-inbox")},
        )
        t_rep = ws.add_task("ReplaceState Gmail", ["gmail-hist-prof"], preset="gmail_trust")
        assert ws.execute_task_sync(t_rep.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_rep.status == "completed"
        assert t_rep.last_result == "OK (1/1 profiles)"
        assert "replace_1" in _keydowns and "replace_2" in _keydowns
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/replace-inbox")

        # 2c. Codex §9 #1: Gmail template copied to preset="script" on /gmail-empty never pops prior history
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/older"})
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/previous"})
        requests_seen.clear()
        copied_steps = _short_gmail_playbook(f"{base_url}/gmail-empty")
        t_script_empty = ws.add_task(
            "Copied Gmail Script Empty",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_steps,
        )
        assert ws.execute_task_sync(t_script_empty.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_script_empty.status == "completed"
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/gmail-empty")

        # 2d. Codex §13 #1: Unedited Gmail catalog template copied to preset="script" must ALSO fail closed
        # on auth redirect during Click 1 (even when /inbox recovers) instead of running the rest of the template.
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/older"})
        bl.control("gmail-hist-prof", "navigate", {"url": f"{base_url}/previous"})
        requests_seen.clear()
        monkeypatch.setattr(sched_mod, "WARMING_SCENARIO_PLAYBOOKS", orig_playbooks)
        catalog_gmail = next(
            s["sample_steps"]
            for s in sched_mod.list_warming_scenarios()
            if s["id"] == "gmail_trust"
        )
        copied_redir_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/recoverable-signout-inbox"
            elif "timeout" in s:
                s["timeout"] = 400
            copied_redir_steps.append(s)
        t_script_redir = ws.add_task(
            "Copied Gmail Script Auth Redirect",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_redir_steps,
        )
        assert ws.execute_task_sync(t_script_redir.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_script_redir.status == "failed"
        assert "OK" not in t_script_redir.last_result
        assert requests_seen == ["/recoverable-signout-inbox", "/signed-out"], requests_seen
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/signed-out")

        # 2e. Codex §13 #2: Final scroll triggers same-origin view change away from verified inbox (/other-message)
        # Must fail immediately without reporting OK or dispatching any late return.
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {
                **orig_playbooks,
                "gmail_trust": _short_gmail_playbook(f"{base_url}/scroll-drift-inbox"),
            },
        )
        t_scroll_drift = ws.add_task(
            "Final Scroll Same-Origin Drift Gmail", ["gmail-hist-prof"], preset="gmail_trust"
        )
        assert ws.execute_task_sync(t_scroll_drift.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_scroll_drift.status == "failed"
        assert "OK" not in t_scroll_drift.last_result
        assert "/mail/1" in requests_seen and "/mail/2" in requests_seen
        assert requests_seen.count("/scroll-drift-inbox") == 3
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/other-message")

        # 2f. Codex §15 #1: Copied Gmail catalog template (preset="script") with an EXECUTED Click 2 whose result
        # stays on /inbox and arms /signed-out on the next scroll must fail at Step 23 with ZERO follow-up scrolls.
        requests_seen.clear()
        _keydowns.clear()
        copied_pending_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/click2-pending-inbox"
            elif "timeout" in s:
                s["timeout"] = 400
            copied_pending_steps.append(s)
        t_script_c2_pending = ws.add_task(
            "Copied Gmail Script Click2 Pending",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_pending_steps,
        )
        assert ws.execute_task_sync(t_script_c2_pending.id, pm, bl, step_dwell_seconds=0.0) is False
        assert t_script_c2_pending.status == "failed"
        assert "OK" not in t_script_c2_pending.last_result
        assert "click2_executed" in _keydowns
        assert "armed_scroll" not in _keydowns
        assert "/signed-out" not in requests_seen
        assert requests_seen == [
            "/click2-pending-inbox",
            "/mail/1",
            "/click2-pending-inbox",
        ], requests_seen

        # 2g. Codex §15 #1: Copied Gmail catalog template (preset="script") where Row 2 is genuinely missing
        # (1-email inbox) accepts the inbox fallback because Click 2 was confirmed not to dispatch (action_timeout).
        requests_seen.clear()
        copied_one_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/one-mail-inbox"
            elif "timeout" in s:
                s["timeout"] = 400
            copied_one_steps.append(s)
        t_script_one = ws.add_task(
            "Copied Gmail Script One Mail",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_one_steps,
        )
        assert ws.execute_task_sync(t_script_one.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_script_one.status == "completed"
        assert t_script_one.last_result == "OK (1/1 profiles)"
        assert requests_seen == [
            "/one-mail-inbox",
            "/mail/1",
            "/one-mail-inbox",
            "/one-mail-inbox",
        ], requests_seen

        # 2h. Codex §15 #1: Copied Gmail catalog template (preset="script") on normal 2-message /gmail succeeds.
        requests_seen.clear()
        copied_two_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/gmail"
            elif "timeout" in s:
                s["timeout"] = 400
            copied_two_steps.append(s)
        t_script_two = ws.add_task(
            "Copied Gmail Script Two Mails",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_two_steps,
        )
        assert ws.execute_task_sync(t_script_two.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_script_two.status == "completed"
        assert t_script_two.last_result == "OK (1/1 profiles)"
        assert "/mail/1" in requests_seen and "/mail/2" in requests_seen

        # 2i. Codex §17 #1: Copied Gmail catalog template with an intervening wait between Click 2 and its verifier
        # preserves confirmed no-dispatch missing-row verification on /one-mail-inbox.
        requests_seen.clear()
        copied_one_wait_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/one-mail-inbox"
            elif "timeout" in s:
                s["timeout"] = 400
            if (
                s["action"] == "wait_for"
                and s.get("selector") == sched_mod._GMAIL_CLICK2_VERIFY_SELECTOR
            ):
                copied_one_wait_steps.append({"action": "wait", "seconds": 0})
            copied_one_wait_steps.append(s)
        assert len(copied_one_wait_steps) == len(catalog_gmail) + 1
        t_script_one_wait = ws.add_task(
            "Copied Gmail Script One Mail With Wait",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_one_wait_steps,
        )
        assert ws.execute_task_sync(t_script_one_wait.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_script_one_wait.status == "completed"
        assert t_script_one_wait.last_result == "OK (1/1 profiles)"
        assert requests_seen == [
            "/one-mail-inbox",
            "/mail/1",
            "/one-mail-inbox",
            "/one-mail-inbox",
        ], requests_seen

        # 2j. Codex §17 #1: Paired executed-but-unverified Click 2 with an intervening wait between Click 2
        # and its verifier still fails closed before any follow-up scroll or return navigation.
        requests_seen.clear()
        _keydowns.clear()
        copied_pending_wait_steps = []
        for st in catalog_gmail:
            s = dict(st)
            if s["action"] == "navigate":
                s["url"] = f"{base_url}/click2-pending-inbox"
            elif "timeout" in s:
                s["timeout"] = 400
            if (
                s["action"] == "wait_for"
                and s.get("selector") == sched_mod._GMAIL_CLICK2_VERIFY_SELECTOR
            ):
                copied_pending_wait_steps.append({"action": "wait", "seconds": 0})
            copied_pending_wait_steps.append(s)
        assert len(copied_pending_wait_steps) == len(catalog_gmail) + 1
        t_script_pending_wait = ws.add_task(
            "Copied Gmail Script Click2 Pending With Wait",
            ["gmail-hist-prof"],
            preset="script",
            script_steps=copied_pending_wait_steps,
        )
        assert (
            ws.execute_task_sync(t_script_pending_wait.id, pm, bl, step_dwell_seconds=0.0) is False
        )
        assert t_script_pending_wait.status == "failed"
        assert "OK" not in t_script_pending_wait.last_result
        assert "click2_executed" in _keydowns
        assert "armed_scroll" not in _keydowns
        assert "/signed-out" not in requests_seen
        assert requests_seen == [
            "/click2-pending-inbox",
            "/mail/1",
            "/click2-pending-inbox",
        ], requests_seen

        # 3. Verified populated inbox: opens /mail/1 and /mail/2 and returns to /gmail without touching /previous
        requests_seen.clear()
        monkeypatch.setattr(
            sched_mod,
            "WARMING_SCENARIO_PLAYBOOKS",
            {**orig_playbooks, "gmail_trust": _short_gmail_playbook(f"{base_url}/gmail")},
        )
        t_ok = ws.add_task("Populated Gmail", ["gmail-hist-prof"], preset="gmail_trust")
        assert ws.execute_task_sync(t_ok.id, pm, bl, step_dwell_seconds=0.0) is True
        assert t_ok.status == "completed"
        assert t_ok.last_result == "OK (1/1 profiles)"
        assert "/mail/1" in requests_seen and "/mail/2" in requests_seen
        assert "/previous" not in requests_seen and "/older" not in requests_seen
        assert bl.control("gmail-hist-prof", "tabs")["tabs"][0]["url"].endswith("/gmail")
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_browser.set()
        owner_thread.join(10)
        server.shutdown()
        server.server_close()
        site_thread.join(5)
