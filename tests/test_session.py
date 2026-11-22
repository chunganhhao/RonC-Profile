import asyncio
import json
from types import SimpleNamespace

import pytest

from src.services.browser import runner, session


def test_save_and_load_round_trip(tmp_path):
    urls = ["https://a.test/", "http://b.test/x?y=1"]
    assert session.save(str(tmp_path), urls) is True
    assert session.load(str(tmp_path)) == urls


def test_only_http_urls_are_kept(tmp_path):
    session.save(
        str(tmp_path),
        ["about:blank", "https://a.test/", "file:///etc/passwd", "javascript:alert(1)", "data:,x"],
    )
    assert session.load(str(tmp_path)) == ["https://a.test/"]


def test_empty_list_keeps_previous_snapshot(tmp_path):
    session.save(str(tmp_path), ["https://a.test/"])
    assert session.save(str(tmp_path), ["about:blank"]) is False
    assert session.load(str(tmp_path)) == ["https://a.test/"]


def test_tab_count_is_capped(tmp_path):
    session.save(str(tmp_path), [f"https://t{i}.test/" for i in range(100)])
    assert len(session.load(str(tmp_path))) == session.MAX_TABS


def test_missing_or_corrupt_file_loads_nothing(tmp_path):
    assert session.load(str(tmp_path)) == []
    session.tabs_path(str(tmp_path)).write_text("{broken", encoding="utf-8")
    assert session.load(str(tmp_path)) == []
    session.tabs_path(str(tmp_path)).write_text(json.dumps({"tabs": "nope"}), encoding="utf-8")
    assert session.load(str(tmp_path)) == []


def test_tampered_file_cannot_restore_local_urls(tmp_path):
    session.tabs_path(str(tmp_path)).write_text(
        json.dumps({"tabs": ["file:///C:/Windows/win.ini", "https://ok.test/"]}),
        encoding="utf-8",
    )
    assert session.load(str(tmp_path)) == ["https://ok.test/"]


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.failed_navigation = False
        self.context = None

    def is_closed(self):
        return self.closed

    async def goto(self, url, **_):
        if self.failed_navigation:
            raise RuntimeError("Navigation interrupted by pending browser error page")
        if "fail" in url:
            self.failed_navigation = True
            raise RuntimeError("net::ERR")
        self.url = url

    async def bring_to_front(self):
        pass

    async def close(self):
        self.closed = True
        if self.context:
            self.context.pages.remove(self)

    def on(self, *_):
        pass


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.pages[0].context = self

    async def new_page(self):
        page = FakePage()
        page.context = self
        self.pages.append(page)
        return page

    def on(self, *_):
        pass


def test_restore_reuses_blank_tab_and_opens_the_rest():
    ctx = FakeContext()
    n = asyncio.run(runner.restore_tabs(ctx, ["https://a.test/", "https://b.test/"]))
    assert n == 2
    assert [p.url for p in ctx.pages] == ["https://a.test/", "https://b.test/"]


def test_restore_survives_a_failing_tab():
    ctx = FakeContext()
    asyncio.run(runner.restore_tabs(ctx, ["https://fail.test/", "https://ok.test/"]))
    assert [p.url for p in ctx.pages] == ["about:blank", "https://ok.test/"]


@pytest.mark.parametrize(
    "urls, expected",
    [
        ([], 0),
        (["https://fail.test/", "https://fail2.test/"], 0),
        (["https://fail.test/", "https://ok.test/"], 1),
    ],
)
def test_restore_counts_only_loaded_tabs(urls, expected):
    ctx = FakeContext()
    assert asyncio.run(runner.restore_tabs(ctx, urls)) == expected


@pytest.mark.parametrize("engine_name", ["chrome", "camoufox"])
@pytest.mark.parametrize(
    "saved",
    [[], ["https://fail.test/", "https://fail2.test/"], ["https://fail.test/", "https://ok.test/"]],
)
def test_launch_restores_or_falls_back_without_extra_blank_tabs(
    tmp_path, monkeypatch, capsys, engine_name, saved
):
    from chrome_engine import runner as chrome

    target = chrome if engine_name == "chrome" else runner
    ctx = FakeContext()
    startup = "https://startup.test/"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(target, "_read_profile_extra", lambda _: {"start_url": startup})
    monkeypatch.setattr(target, "RESTORE_TABS", True)
    monkeypatch.setattr(target, "_shutdown", asyncio.Event())
    monkeypatch.setattr(session, "load", lambda _: saved)
    monkeypatch.setattr(session, "save", lambda *_: True)

    async def noop():
        pass

    async def serve(_):
        target._shutdown.set()
        return SimpleNamespace(cleanup=noop), 1234, "fixture-token"

    monkeypatch.setattr(target.control, "serve", serve)
    if engine_name == "chrome":

        class Engine:
            warnings = []

            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                return ctx

            async def stop(self):
                pass

        monkeypatch.setattr(chrome, "ChromeEngine", Engine)
        monkeypatch.setattr(chrome, "ensure_installed", noop)
    else:

        class Browser:
            async def __aenter__(self):
                return ctx

            async def __aexit__(self, *_):
                pass

        monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: Browser())
        monkeypatch.setattr(runner, "load_or_create", lambda *_: {})
        monkeypatch.setattr(runner, "exit_ip", lambda *_: "198.51.100.1")
        monkeypatch.setattr(runner, "pin_geoip_locale", lambda _: None)
        monkeypatch.setattr(runner, "HEADLESS", True)

    assert asyncio.run(target.run_browser("fixture", "None", "linux", locale="en-US")) == 0
    output = capsys.readouterr().out
    visible = [p.url for p in ctx.pages if not p.is_closed()]
    if any("ok.test" in url for url in saved):
        assert "Restored 1 tab(s)" in output
        assert visible == ["about:blank", "https://ok.test/"]
    else:
        assert "Restored " not in output
        assert visible == [startup]


def test_snapshot_saves_changes_and_keeps_tabs_when_window_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TAB_SNAPSHOT_SECONDS", 0.01)
    ctx = FakeContext()
    ctx.pages[0].url = "https://a.test/"

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.snapshot_tabs(ctx, str(tmp_path), stop))
        await asyncio.sleep(0.05)
        ctx.pages.append(FakePage("https://b.test/"))
        await asyncio.sleep(0.05)
        for page in ctx.pages:  # user closes every tab: the browser window goes away
            page.closed = True
        stop.set()
        await task

    asyncio.run(scenario())
    assert session.load(str(tmp_path)) == ["https://a.test/", "https://b.test/"]
