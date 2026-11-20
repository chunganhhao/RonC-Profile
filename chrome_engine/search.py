"""Initialize a new Chromium profile through its own search-engine settings API."""

from __future__ import annotations

import asyncio
from typing import Any

_LIST = """async () => {
  const {sendWithPromise} = await import('chrome://resources/js/cr.js');
  return Object.values(await sendWithPromise('getSearchEnginesList')).flat();
}"""
_GOOGLE_URL = "https://www.google.com/search?q=%s"


async def initialize_search(context: Any) -> None:
    """Called only for a new profile; never replace an existing search choice."""
    page = await context.new_page()
    try:
        await page.goto("chrome://settings/searchEngines")
        entries = await page.evaluate(_LIST)
        current = next((e for e in entries if e.get("default")), None)
        if not current or current.get("keyword") != "nosearch" or current.get("url") != "http://%s":
            return
        # Ungoogled Chromium deliberately replaces Google's prepopulated record.
        # Use its native editor, not unsigned/signed Preferences or Web Data rewrites.
        google = next((e for e in entries if e.get("url") == _GOOGLE_URL), None)
        if google is None:
            await page.evaluate("""() => {
              chrome.send('searchEngineEditStarted', [0]);
              chrome.send('searchEngineEditCompleted',
                ['Google', 'google.com', 'https://www.google.com/search?q=%s', '']);
            }""")
        for _ in range(50):
            entries = await page.evaluate(_LIST)
            google = next((e for e in entries if e.get("url") == _GOOGLE_URL), None)
            if google:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Chromium did not register the initial Google search engine")
        await page.evaluate(
            "id => chrome.send('setDefaultSearchEngine', [id, 1, null])", google["id"]
        )
        for _ in range(50):
            entries = await page.evaluate(_LIST)
            if any(e.get("default") and e.get("url") == _GOOGLE_URL for e in entries):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("Chromium did not select the initial Google search engine")
    finally:
        await page.close()
