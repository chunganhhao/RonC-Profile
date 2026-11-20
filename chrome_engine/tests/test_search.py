"""Search initialization preserves choices and uses Chromium's native editor."""

import asyncio

import pytest

from chrome_engine.search import initialize_search


class Page:
    def __init__(self, entries):
        self.entries = entries
        self.closed = False
        self.calls = []

    async def goto(self, url):
        assert url == "chrome://settings/searchEngines"

    async def evaluate(self, script, arg=None):
        self.calls.append(script)
        if "searchEngineEditStarted" in script:
            self.entries.append({"id": 8, "url": "https://www.google.com/search?q=%s"})
        elif "setDefaultSearchEngine" in script:
            for entry in self.entries:
                entry["default"] = entry["id"] == arg
        else:
            return self.entries

    async def close(self):
        self.closed = True


class Context:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def test_new_nosearch_is_replaced_with_google():
    page = Page([{"id": 2, "default": True, "keyword": "nosearch", "url": "http://%s"}])
    asyncio.run(initialize_search(Context(page)))
    assert page.entries[-1]["default"]
    assert page.closed


def test_custom_provider_is_not_replaced():
    page = Page(
        [{"id": 2, "default": True, "keyword": "ddg", "url": "https://duckduckgo.com/?q=%s"}]
    )
    asyncio.run(initialize_search(Context(page)))
    assert len(page.calls) == 1
    assert page.closed


def test_retry_reuses_google_registered_before_a_failure():
    page = Page(
        [
            {"id": 2, "default": True, "keyword": "nosearch", "url": "http://%s"},
            {"id": 8, "url": "https://www.google.com/search?q=%s"},
        ]
    )
    asyncio.run(initialize_search(Context(page)))
    assert len(page.entries) == 2
    assert page.entries[-1]["default"]
    assert not any("searchEngineEditStarted" in call for call in page.calls)


def test_failed_editor_closes_internal_page():
    class Broken(Page):
        async def evaluate(self, script, arg=None):
            raise RuntimeError("settings-unavailable")

    page = Broken([])
    with pytest.raises(RuntimeError, match="settings-unavailable"):
        asyncio.run(initialize_search(Context(page)))
    assert page.closed
