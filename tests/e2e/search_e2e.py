"""Real Camoufox browser-chrome search; only an owned temporary profile is used.

Run: .venv/bin/python -m tests.e2e.search_e2e (or execute with PYTHONPATH=.)
Marionette is enabled only for this test, never by the production launcher.
"""

import asyncio
import contextlib
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from camoufox.async_api import AsyncCamoufox

from src.services.browser.search import ensure_google_search


class Marionette:
    def __init__(self, port):
        deadline = time.monotonic() + 30
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.settimeout(45)
        self.seq = 0
        self.recv()
        self.call("WebDriver:NewSession", {"capabilities": {}})
        self.call("Marionette:SetContext", {"value": "chrome"})

    def recv(self):
        header = b""
        while not header.endswith(b":"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("closed")
            header += chunk
        remaining = int(header[:-1])
        data = b""
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
            remaining -= len(chunk)
        return json.loads(data)

    def call(self, method, params):
        self.seq += 1
        data = json.dumps([0, self.seq, method, params]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        result = self.recv()
        if result[2]:
            raise RuntimeError(result[2])
        return result[3]

    def js(self, script):
        return self.call("WebDriver:ExecuteScript", {"script": script, "args": []})["value"]

    def search(self, text):
        self.js("gURLBar.focus(); gURLBar.select(); return true;")
        element = self.call(
            "WebDriver:FindElement", {"using": "css selector", "value": "#urlbar-input"}
        )
        element_id = element["value"]["element-6066-11e4-a52e-4f735466cecf"]
        self.call("WebDriver:ElementSendKeys", {"id": element_id, "text": text})
        self.call("WebDriver:ElementSendKeys", {"id": element_id, "text": "\ue007"})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            url = self.js("return gBrowser.selectedBrowser.currentURI.spec;")
            if url.startswith(("http:", "https:")):
                return url
            time.sleep(0.1)
        raise AssertionError("search never navigated")


async def run(out, baseline=False):
    out.mkdir(parents=True, exist_ok=True)
    changed = False if baseline else await asyncio.to_thread(ensure_google_search)
    results = []
    with tempfile.TemporaryDirectory(prefix="ronc-search-e2e-") as directory:
        for query in ["mail"] if baseline else ["mail", "ronc profile & test"]:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            async with AsyncCamoufox(
                headless=True,
                os="macos",
                persistent_context=True,
                user_data_dir=directory,
                args=["--marionette", "--remote-allow-system-access"],
                env={**os.environ, "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"},
                firefox_user_prefs={"marionette.port": port},
            ) as context:
                page = context.pages[0]
                requests = []
                page.on(
                    "request",
                    lambda request, requests=requests: (
                        requests.append(request.url) if request.is_navigation_request() else None
                    ),
                )
                client = await asyncio.to_thread(Marionette, port)
                try:
                    url = await asyncio.to_thread(client.search, query)
                    if baseline:
                        assert urlsplit(url).hostname == "127.0.0.1", url
                    else:
                        # Google may redirect to CAPTCHA before chrome's currentURI is read.
                        url = next(
                            (
                                u
                                for u in requests
                                if urlsplit(u).hostname == "www.google.com"
                                and urlsplit(u).path == "/search"
                            ),
                            "",
                        )
                        parsed = urlsplit(url)
                        assert parsed.scheme == "https", requests
                        assert parsed.path == "/search" and parse_qs(parsed.query)["q"] == [
                            query
                        ], url
                    # URL dispatch is asserted separately from Google's external response.
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state("domcontentloaded", timeout=30000)
                    results.append(
                        {
                            "query": query,
                            "url": url,
                            "final_url": page.url,
                            "title": await page.title(),
                        }
                    )
                    await page.screenshot(path=str(out / f"{len(results)}.png"))
                finally:
                    client.sock.close()
    result = {
        "passed": True,
        "baseline": baseline,
        "policy_changed": changed,
        "temporary_profile_cleaned": not Path(directory).exists(),
        "searches": results,
    }
    (out / ("baseline.json" if baseline else "result.json")).write_text(
        json.dumps(result, indent=2)
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("artifacts/google-search-2026-11-09"))
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.out, args.baseline))
