"""Branding DOM E2E against an isolated API/server, never existing profiles."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "ronc-profile-branding-2026-11-09"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ronc-branding-e2e-") as temp:
        env = os.environ.copy()
        for key, filename in {
            "PROFILES_FILE": "profiles.json",
            "PROXY_POOL_FILE": "proxies.json",
            "WARMING_FILE": "warming.json",
            "DATA_DIR": "data",
            "LOG_DIR": "logs",
        }.items():
            env[f"FOXPROFILE_{key}"] = str(Path(temp) / filename)
        env["FOXPROFILE_API_TOKEN"] = "t" * 32  # Fixture only, not a user credential.
        env["FOXPROFILE_LANG"] = "en"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (OUT / "server.log").open("w") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.server", "--port", str(port)],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError("Isolated server exited; see server.log")
                    try:
                        with urllib.request.urlopen(origin + "/api/v1/info", timeout=1) as response:
                            assert json.load(response)["name"] == "RonC Profile"
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated server did not become ready")
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        headless=True,
                    )
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 900})
                        page.goto(origin)
                        expect(page).to_have_title("RonC Profile")
                        expect(page.locator("#login .brand b")).to_have_text("RonC Profile")
                        page.wait_for_function(
                            "document.querySelector('#login .brand img').naturalWidth > 0"
                        )
                        page.screenshot(path=str(OUT / "login.png"), full_page=True)
                        page.locator("#token").fill("t" * 32)
                        page.locator("#login-form button").click()
                        expect(page.locator("#app")).to_be_visible()
                        expect(page.locator("#app .brand b")).to_have_text("RonC Profile")
                        expect(page.locator(".rail-foot .seg")).to_have_count(0)
                        page.locator("#prefs-btn").click()
                        expect(page.locator("#prefs-popover")).to_be_visible()
                        page.locator("#lang-select-btn").click()
                        page.locator("[data-lang='vi']").click()
                        expect(page.locator("#app .brand b")).to_have_text("RonC Profile")
                        page.wait_for_function(
                            "document.querySelector('#app .brand img').naturalWidth > 0"
                        )
                        for preset in ("slate", "blue", "green", "orange"):
                            page.locator(f"[data-accent-btn='{preset}']").click()
                            assert (
                                page.evaluate("document.documentElement.dataset.accent") == preset
                            )
                        page.screenshot(
                            path=str(OUT / "appearance-popover.png"),
                            full_page=True,
                            animations="disabled",
                        )
                        page.locator("#prefs-btn").click()
                        page.locator("#new-btn").click()
                        page.locator("#f-name").fill("ronc-branding-fixture")
                        page.locator("#modal-root .modal-foot .btn-act").click()
                        expect(
                            page.get_by_role("row").filter(has_text="ronc-branding-fixture")
                        ).to_be_visible()
                        page.screenshot(
                            path=str(OUT / "panel.png"), full_page=True, animations="disabled"
                        )
                        page.locator("#prefs-btn").click()
                        page.locator("[data-theme-btn='dark']").click()
                        page.wait_for_function(
                            "document.querySelector('#app .brand img').naturalWidth > 0"
                        )
                        page.screenshot(
                            path=str(OUT / "panel-dark.png"), full_page=True, animations="disabled"
                        )
                        page.locator("[data-theme-btn='light']").click()
                        page.locator("#prefs-btn").click()
                        assert (
                            page.request.delete(
                                origin + "/api/v1/profiles/ronc-branding-fixture",
                                headers={"Authorization": "Bearer " + "t" * 32},
                            ).status
                            == 200
                        )
                        (OUT / "result.json").write_text(
                            json.dumps(
                                {
                                    "result": "PASS",
                                    "brand": "RonC Profile",
                                    "login_logo": True,
                                    "panel_logo": True,
                                    "vi_en_brand": True,
                                    "profile_crud": True,
                                    "isolated_runtime": True,
                                    "browser": browser.version,
                                },
                                indent=2,
                            )
                        )
                        print(
                            "PASS: login/panel logos, EN/VI branding, isolated profile create/delete"
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
