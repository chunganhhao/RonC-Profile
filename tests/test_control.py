import asyncio

import pytest

from src.services.browser.control import ControlError, PageController, _check_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("example.com", "https://example.com"),
        ("http://a.vn/x", "http://a.vn/x"),
        ("about:blank", "about:blank"),
        ("localhost:8080/x", "https://localhost:8080/x"),
    ],
)
def test_allowed_urls(url, expected):
    assert _check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "view-source:https://a.com",
        "javascript:alert(1)",
        "chrome://x",
        "about:config",
        "about:logins",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_blocked_urls(url):
    with pytest.raises(ControlError):
        _check_url(url)


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T"

    async def goto(self, url, wait_until="load"):
        self.url = url

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass


class FakeContext:
    def __init__(self, n=1):
        self.pages = [FakePage(f"https://p{i}.test/") for i in range(n)]

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


def run(coro):
    return asyncio.run(coro)


def test_dispatch_rejects_unknown_action_and_bad_params():
    c = PageController(FakeContext())
    with pytest.raises(ControlError, match="Unknown action"):
        run(c.dispatch("rm_rf", {}))
    with pytest.raises(ControlError, match="Invalid parameters"):
        run(c.dispatch("navigate", {"nope": 1}))


def test_navigate_uses_active_tab():
    ctx = FakeContext(2)
    c = PageController(ctx)
    result = run(c.dispatch("navigate", {"url": "example.com"}))
    assert result == {"url": "https://example.com", "title": "T"}
    assert ctx.pages[-1].url == "https://example.com"


def test_tabs_select_and_close():
    ctx = FakeContext(2)
    c = PageController(ctx)
    tabs = run(c.dispatch("tab_select", {"index": 0}))["tabs"]
    assert [t["active"] for t in tabs] == [True, False]
    tabs = run(c.dispatch("tab_close", {"index": 0}))["tabs"]
    assert [t["url"] for t in tabs] == ["https://p1.test/"]
    with pytest.raises(ControlError, match="last tab"):
        run(c.dispatch("tab_close", {"index": 0}))
    with pytest.raises(ControlError, match="No tab"):
        run(c.dispatch("tab_select", {"index": 5}))


# ---------------------------------------------------------------------------
# Percent-encoded scheme bypass (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "%66ile:///C:/Windows/win.ini",  # decodes to file:///...
        "java%73cript:alert(1)",  # decodes to javascript:...
        "%76iew-source:https://a.com",  # decodes to view-source:...
    ],
)
def test_percent_encoded_blocked_schemes(url):
    """Percent-encoded disallowed schemes must be caught before scheme detection."""
    with pytest.raises(ControlError):
        _check_url(url)


@pytest.mark.parametrize("url", ["[", "[]", "[::1", "http://[::1"])
def test_malformed_url_is_a_control_error(url):
    with pytest.raises(ControlError):
        _check_url(url)


def test_control_server_cancels_timed_out_action_and_serializes_uninterruptible():
    import threading
    from types import SimpleNamespace

    from src.services.browser import control
    from src.services.browser.launcher import BrowserControlError, BrowserLauncher

    release_uninterruptible = threading.Event()
    uninterruptible_entered = threading.Event()

    class TimedPage(FakePage):
        def __init__(self):
            super().__init__("https://start.test/")
            self.events: list[str] = []
            self.block_cancel = False

        async def goto(self, url, wait_until="load"):
            self.events.append("goto-started")
            if self.block_cancel:
                uninterruptible_entered.set()
                while not release_uninterruptible.is_set():
                    try:
                        await asyncio.sleep(0.02)
                    except asyncio.CancelledError:
                        # Simulate a Playwright RPC that ignores cancellation until completion.
                        continue
                self.events.append("goto-uninterruptible-effect")
                return
            await asyncio.sleep(5.0)
            self.events.append("goto-late-effect")

        async def go_back(self):
            self.events.append("back-effect")

    page = TimedPage()
    ctx = SimpleNamespace(pages=[page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["p"] = SimpleNamespace(poll=lambda: None)
    bl._controls["p"] = (info["port"], info["token"])
    try:
        # 1. Cancellable slow step times out deterministically with 504 and leaves no late effect.
        with pytest.raises(BrowserControlError) as exc1:
            bl.control("p", "navigate", {"url": "https://slow.test"}, timeout=0.2)
        assert exc1.value.status == 504
        assert page.events == ["goto-started"]

        # Subsequent action succeeds cleanly after cancellation settled.
        bl.control("p", "back", {}, timeout=1.0)
        assert page.events == ["goto-started", "back-effect"]

        # 2. Uninterruptible step holds action_lock after 504 so next action cannot overlap it.
        page.events.clear()
        page.block_cancel = True
        with pytest.raises(BrowserControlError) as exc2:
            bl.control("p", "navigate", {"url": "https://stuck.test"}, timeout=0.2)
        assert exc2.value.status == 504
        assert uninterruptible_entered.is_set()

        with pytest.raises(BrowserControlError) as exc_busy:
            bl.control("p", "back", {}, timeout=0.15)
        assert exc_busy.value.status == 504
        assert "Previous browser action is still running" in str(exc_busy.value)
        assert page.events == ["goto-started"]

        release_uninterruptible.set()
        # Wait briefly for uninterruptible coroutine to finish and release action_lock.
        for _ in range(50):
            try:
                bl.control("p", "back", {}, timeout=0.5)
                break
            except BrowserControlError:
                threading.Event().wait(0.02)
        assert page.events == ["goto-started", "goto-uninterruptible-effect", "back-effect"]
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        release_uninterruptible.set()
        stop_loop.set()
        srv.join(3)


def test_control_server_skips_dispatch_when_deadline_already_expired(monkeypatch):
    import threading
    from types import SimpleNamespace

    from src.services.browser import control
    from src.services.browser.launcher import BrowserControlError, BrowserLauncher

    class WatchPage(FakePage):
        def __init__(self):
            super().__init__("https://start.test/")
            self.events: list[str] = []

        async def go_back(self):
            self.events.append("back-effect")

    page = WatchPage()
    ctx = SimpleNamespace(pages=[page])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    # First call sets deadline = 100.0 + 0.1 = 100.1; second call after lock is 100.101 -> expired.
    ticks = iter([100.0, 100.101])
    monkeypatch.setattr(control, "_monotonic", lambda: next(ticks, 100.2))

    bl = BrowserLauncher()
    bl._active_sessions["p"] = SimpleNamespace(poll=lambda: None)
    bl._controls["p"] = (info["port"], info["token"])
    try:
        with pytest.raises(BrowserControlError) as exc:
            bl.control("p", "back", {}, timeout=0.1)
        assert exc.value.status == 504
        assert exc.value.error_code == "deadline_expired"
        assert page.events == []
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)


def test_control_server_rejects_closed_target_on_tab_select_and_require_current():
    import threading
    from types import SimpleNamespace

    from src.services.browser import control
    from src.services.browser.launcher import BrowserControlError, BrowserLauncher

    class TabStub:
        def __init__(self, url: str, *, close_on_front: bool = False):
            self.url = url
            self.closed = False
            self.close_on_front = close_on_front
            self.events: list[str] = []

        def is_closed(self):
            return self.closed

        async def title(self):
            return self.url

        async def bring_to_front(self):
            self.events.append("bring_to_front")
            if self.close_on_front:
                self.closed = True

        async def go_back(self):
            self.events.append("back")

    tab0 = TabStub("https://base.test/")
    tab1 = TabStub("https://popup-close.test/", close_on_front=True)
    tab2 = TabStub("https://popup-open.test/")
    ctx = SimpleNamespace(pages=[tab0, tab1, tab2])
    ready = threading.Event()
    stop_loop = threading.Event()
    info: dict = {}

    def _serve():
        async def _run():
            runner, port, token = await control.serve(ctx)
            info["port"], info["token"] = port, token
            ready.set()
            while not stop_loop.is_set():
                await asyncio.sleep(0.02)
            await runner.cleanup()

        asyncio.run(_run())

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    assert ready.wait(3)

    bl = BrowserLauncher()
    bl._active_sessions["p"] = SimpleNamespace(poll=lambda: None)
    bl._controls["p"] = (info["port"], info["token"])
    try:
        # 1. Invalid require_current type -> HTTP 400
        with pytest.raises(BrowserControlError) as exc_bad:
            bl.control("p", "tabs", {}, require_current="yes")  # type: ignore[arg-type]
        assert exc_bad.value.status == 400

        # 2. tab_select silently closes target during bring_to_front -> 422 state_uncertain
        with pytest.raises(BrowserControlError) as exc_sel:
            bl.control("p", "tab_select", {"index": 1})
        assert exc_sel.value.status == 422
        assert exc_sel.value.error_code == "state_uncertain"
        assert exc_sel.value.unknown_state is True
        assert tab0.events == []

        # 3. Selecting open tab2 succeeds; closing it before require_current=True aborts cleanly
        res = bl.control("p", "tab_select", {"index": 1})  # tab1 is closed, so index 1 is tab2
        assert res["tabs"][1]["active"] is True
        tab2.closed = True
        with pytest.raises(BrowserControlError) as exc_pin:
            bl.control("p", "back", {}, require_current=True)
        assert exc_pin.value.status == 422
        assert exc_pin.value.error_code == "state_uncertain"
        assert exc_pin.value.unknown_state is True
        assert tab0.events == []

        # 4. require_current=False still falls back to remaining open tab0
        bl.control("p", "back", {}, require_current=False)
        assert tab0.events == ["back"]

        # 5. target_page_id validates type/non-empty and rejects tab_select switch while open
        with pytest.raises(BrowserControlError) as exc_bad_pid:
            bl.control("p", "tabs", {}, target_page_id="")
        assert exc_bad_pid.value.status == 400

        res_back, pid2 = bl.control("p", "back", {}, return_page_id=True)
        assert res_back["url"] == "https://base.test/"
        assert isinstance(pid2, str) and pid2

        # Open a second tab and switch active tab to index 1 while original tab0 stays open
        ctx.pages = [tab0, TabStub("https://other.test/")]
        tab0_events_before = len(tab0.events)
        _, pid0 = bl.control("p", "tab_select", {"index": 0}, return_page_id=True)
        bl.control("p", "tab_select", {"index": 1})
        with pytest.raises(BrowserControlError) as exc_switched:
            bl.control("p", "back", {}, require_current=True, target_page_id=pid0)
        assert exc_switched.value.status == 422
        assert exc_switched.value.error_code == "state_uncertain"
        assert exc_switched.value.unknown_state is True
        assert ctx.pages[1].events == ["bring_to_front"]
        assert len(tab0.events) == tab0_events_before + 1  # bring_to_front only

        # Switching back to tab0 restores pid0 identity so back succeeds
        bl.control("p", "tab_select", {"index": 0})
        bl.control("p", "back", {}, require_current=True, target_page_id=pid0)
        assert tab0.events[-1] == "back"
    finally:
        bl._active_sessions.clear()
        bl._controls.clear()
        stop_loop.set()
        srv.join(3)
