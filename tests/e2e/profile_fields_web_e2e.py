"""Rendered field-editor checks against an owned temporary profile store."""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/launch-ui-2026-11-09/web"
TOKEN = "fixture-token-" + "t" * 32


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    with (OUT / "server.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/e2e/profiles_fixture.py"), "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Fixture exited")
                try:
                    with urllib.request.urlopen(origin + "/api/v1/info", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Fixture not ready")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    headless=True,
                )
                try:
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        permissions=["clipboard-read", "clipboard-write"],
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(origin)
                    page.locator("#token").fill(TOKEN)
                    page.locator("#login-form button").click()
                    expect(page.locator("#app")).to_be_visible()
                    row = page.locator(".row.body").filter(
                        has=page.locator(".name", has_text="Studio Gmail")
                    )
                    expect(row).to_have_count(1)
                    row.get_by_role("button", name="Copy Gmail: Studio Gmail", exact=True).click()
                    assert page.evaluate("navigator.clipboard.readText()") == "studio@example.test"
                    expect(
                        row.get_by_role("button", name="Copy Gmail: Studio Gmail", exact=True)
                    ).to_have_class(re.compile(r"is-copied"))
                    expect(page.locator("#toast")).to_be_visible()
                    # Cancel preserves the original notes.
                    row.get_by_role("button", name="Edit Notes: Studio Gmail", exact=True).click()
                    page.locator("#field-value").fill("discard this")
                    page.get_by_role("button", name="Cancel", exact=True).click()
                    expect(row).to_contain_text("Review before signing in")
                    for field, value in [
                        ("ID", "2"),
                        ("Notes", "Keep this useful note"),
                        ("Proxy", "http://127.0.0.1:8088"),
                    ]:
                        row.get_by_role(
                            "button", name=f"Edit {field}: Studio Gmail", exact=True
                        ).click()
                        page.locator("#field-value").fill(value)
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                    row.get_by_role("button", name="Edit Tags: Studio Gmail", exact=True).click()
                    page.get_by_role("button", name="Remove Gemini Pixel", exact=True).click()
                    page.locator("#tag-input").fill("Research <safe>")
                    page.get_by_role("button", name="Add tag", exact=True).click()
                    page.screenshot(path=str(OUT / "tags-modal.png"))
                    page.locator("#modal-root .btn-act").click()
                    expect(row).to_contain_text("Research <safe>")
                    # Profile name editor never exposes or updates account_uid.
                    row.get_by_role("button", name="Edit Profile: Studio Gmail", exact=True).click()
                    expect(page.locator("#modal-root input")).to_have_count(1)
                    page.locator("#field-value").fill("Studio renamed")
                    page.locator("#modal-root .btn-act").click()
                    expect(page.get_by_role("dialog")).to_have_count(0)
                    renamed = page.locator(".row.body").filter(
                        has=page.locator(".name", has_text="Studio renamed")
                    )
                    expect(renamed).to_contain_text("studio@example.test")
                    response = page.request.get(
                        origin + "/api/v1/profiles/Studio%20renamed",
                        headers={"Authorization": "Bearer " + TOKEN},
                    )
                    data = response.json()
                    assert data["display_order"] == 2
                    assert data["tags"] == ["Gmail", "Research <safe>"]
                    assert data["notes"] == "Keep this useful note"
                    assert data["account_uid"] == "studio@example.test"
                    assert data["proxy"] == "http://127.0.0.1:8088"
                    # Invalid server-side proxy keeps the editor and error visible.
                    renamed.get_by_role(
                        "button", name="Edit Proxy: Studio renamed", exact=True
                    ).click()
                    page.locator("#field-value").fill("not a proxy")
                    page.locator("#modal-root .btn-act").click()
                    expect(page.get_by_role("dialog")).to_be_visible()
                    expect(page.locator("#modal-root [role=alert]")).not_to_be_empty()
                    page.get_by_role("button", name="Cancel", exact=True).click()
                    # Simulate status only in owned UI memory, never launch a browser.
                    page.evaluate(
                        "running.add('Studio renamed'); liveSince.set('Studio renamed', new Date(2026, 10, 9, 21, 11)); lastRender=''; render()"
                    )
                    expect(renamed.locator(".stamp")).to_contain_text("Running")
                    expect(
                        renamed.get_by_role(
                            "button", name="Edit Profile: Studio renamed", exact=True
                        )
                    ).to_be_disabled()
                    for width in (1440, 1180, 375):
                        page.set_viewport_size({"width": width, "height": 900})
                        for theme in ("light", "dark"):
                            if page.locator("#prefs-popover").is_hidden():
                                page.locator("#prefs-btn").click()
                            page.locator(f"[data-theme-btn='{theme}']").click()
                            page.locator("#prefs-btn").click()
                            page.screenshot(
                                path=str(OUT / f"profiles-{width}-{theme}.png"), full_page=True
                            )
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            )
                    assert not errors, errors
                    (OUT / "result.json").write_text(
                        json.dumps(
                            {
                                "result": "PASS",
                                "checks": [
                                    "copy uid",
                                    "cancel",
                                    "order",
                                    "notes",
                                    "proxy",
                                    "multi tags",
                                    "rename preserves uid",
                                    "proxy validation",
                                    "running name guard",
                                    "light/dark 1440/1180/375 no horizontal overflow",
                                ],
                                "page_errors": errors,
                            },
                            indent=2,
                        )
                    )
                    print(
                        "PASS: field editors, copy Gmail, validation, compact status, light/dark1440/1180/375; temp store only"
                    )
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
