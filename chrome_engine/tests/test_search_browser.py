"""Real-core search bootstrap retries without rewriting a later user choice."""

import asyncio

import pytest

from chrome_engine import search
from chrome_engine.engine import ChromeEngine

pytestmark = pytest.mark.browser


def test_failed_bootstrap_retries_after_web_data_exists(chrome_exe, tmp_path, monkeypatch):
    original = search.initialize_search

    async def fail(context):
        raise RuntimeError("transient-search-error")

    async def run():
        monkeypatch.setattr(search, "initialize_search", fail)
        engine = ChromeEngine(tmp_path, executable=chrome_exe, headless=True, geoip=False)
        with pytest.raises(RuntimeError, match="transient-search-error"):
            await engine.start()
        assert (tmp_path / "Default" / "Web Data").exists()
        assert (tmp_path / ".search-bootstrap-pending").exists()
        monkeypatch.setattr(search, "initialize_search", original)
        engine = ChromeEngine(tmp_path, executable=chrome_exe, headless=True, geoip=False)
        try:
            context = await engine.start()
            assert not (tmp_path / ".search-bootstrap-pending").exists()
            page = await context.new_page()
            await page.goto("chrome://settings/searchEngines")
            entries = await page.evaluate(search._LIST)
            assert any(e.get("default") and e.get("keyword") == "google.com" for e in entries)
        finally:
            await engine.stop()

    asyncio.run(run())
