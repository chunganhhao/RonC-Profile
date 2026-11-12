"""Isolated DOM E2E of real core API/UI with tiny checksum-pinned fixture archive.

This tests staging/guards, not a production core upgrade or real account.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "core-manager-2026-11-09"


def fixture_service():
    from chrome_engine import fetch
    from chrome_engine.release import Asset, Release
    from src.services.browser import cores

    archive = Path(os.environ["CORE_QA_ARCHIVE"])
    asset = Asset(
        archive.as_uri(),
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        archive.stat().st_size,
        "zip",
        "chrome",
    )
    cores.RELEASES = {"999.0.0.1": Release("999.0.0.1", {"test": asset})}
    cores._host_asset = lambda *_: asset
    cores._camoufox_inventory = lambda: {
        "engine": "camoufox",
        "selected_version": "fixture-existing",
        "installed": True,
    }
    cores._stack = lambda: dict(cores.PAIRED_STACK)
    old = archive.parent / "old-browser-fixture"
    old.write_bytes(b"old browser fixture")
    fetch.installed_executable = lambda *_: old


def main(hold=False):
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ronc-core-qa-") as directory:
        temp = Path(directory)
        archive = temp / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("chrome", b"installer fixture only, not an executable browser")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        for name in [
            "PROFILES_FILE",
            "PROXY_POOL_FILE",
            "WARMING_FILE",
            "DATA_DIR",
            "LOG_DIR",
            "CORE_HOME",
            "CHROME_HOME",
            "CORE_BACKUP_HOME",
        ]:
            env["FOXPROFILE_" + name] = str(temp / name.lower())
        env["CORE_QA_ARCHIVE"] = str(archive)
        env["FOXPROFILE_API_TOKEN"] = "t" * 32
        env["FOXPROFILE_LANG"] = "en"
        env["FOXPROFILE_HEADLESS"] = "true"
        env["FOXPROFILE_RESTORE_TABS"] = "false"
        sentinel = temp / "data_dir" / "core-qa" / "session.fixture"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"synthetic private state must remain unchanged")
        before = sentinel.read_bytes()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (OUT / "server.log").open("w") as log:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)],
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError("isolated server exited")
                    try:
                        urllib.request.urlopen(origin + "/api/v1/info", timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("server unavailable")
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        headless=True,
                    )
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        page.goto(origin)
                        page.locator("#token").fill("t" * 32)
                        page.locator("#login-form button").click()
                        page.locator("#cores-btn").click()
                        dialog = page.get_by_role("dialog")
                        expect(dialog.get_by_role("heading")).to_have_text("Browser cores")
                        expect(dialog.locator('[data-core="camoufox"]')).to_contain_text(
                            "fixture-existing"
                        )
                        expect(dialog.locator("#core-stage")).to_be_disabled()
                        expect(dialog.locator("#core-apply")).to_be_disabled()
                        dialog.locator("#core-choice").select_option("chrome:999.0.0.1")
                        expect(dialog.locator("#core-stage")).to_be_disabled()
                        dialog.locator("#core-confirm").check()
                        dialog.locator("#core-stage").click()
                        expect(dialog.locator("#core-result")).to_have_text(
                            "Download verified and staged. Active core and profiles are unchanged."
                        )
                        expect(dialog.locator("#core-stage")).to_be_disabled()
                        expect(dialog.locator("#core-confirm")).not_to_be_checked()
                        headers = {"Authorization": "Bearer " + "t" * 32}
                        assert (
                            page.request.post(
                                origin + "/api/v1/cores/activate",
                                headers=headers,
                                data={
                                    "name": "core-qa",
                                    "engine": "chrome",
                                    "version": "999.0.0.1",
                                },
                            ).status
                            == 409
                        )
                        assert (
                            page.request.post(
                                origin + "/api/v1/cores/stage",
                                headers=headers,
                                data={
                                    "engine": "chrome",
                                    "version": "latest",
                                    "confirm_download": True,
                                },
                            ).status
                            == 409
                        )
                        assert page.request.get(origin + "/api/v1/cores").status == 401
                        dialog.locator("#core-profile").select_option("core-qa")
                        expect(dialog.locator("#core-apply")).to_be_disabled()
                        dialog.locator("#core-backup").check()
                        expect(dialog.locator("#core-apply")).to_be_disabled()
                        dialog.locator("#core-apply-ack").check()
                        dialog.locator("#core-apply").click()
                        expect(dialog.locator("#core-result")).to_contain_text(
                            "Core bound for next launch"
                        )
                        expect(dialog.locator("#core-prune")).to_be_disabled()
                        dialog.locator("#core-prune-ack").check()
                        expect(dialog.locator("#core-prune")).to_be_enabled()
                        dialog.locator("#core-prune").click()
                        expect(dialog.locator("#core-result")).to_contain_text(
                            "Pruned 0 unused core(s) and 0 old backup(s)."
                        )
                        expect(dialog.locator("#core-prune")).to_be_disabled()
                        bound = page.request.get(
                            origin + "/api/v1/profiles/core-qa", headers=headers
                        ).json()
                        assert bound["core_version"] == "999.0.0.1"
                        assert not bound["is_running"]
                        backups = list((temp / "core_backup_home").rglob("*.zip"))
                        assert len(backups) == 1
                        with zipfile.ZipFile(backups[0]) as z:
                            assert z.read("data/session.fixture") == before
                        assert sentinel.read_bytes() == before
                        assert not (temp / "chrome_home").exists()
                        assert (temp / "core_home/chrome/999.0.0.1/.installed").exists()
                        page.screenshot(
                            path=str(OUT / "panel-en.png"), full_page=True, animations="disabled"
                        )
                        dialog.locator(".modal-foot").get_by_role(
                            "button", name="Close", exact=True
                        ).click()
                        page.locator("#prefs-btn").click()
                        page.locator("#lang-select-btn").click()
                        page.locator('[data-lang="vi"]').click()
                        page.locator("#prefs-btn").click()
                        page.locator("#cores-btn").click()
                        expect(page.get_by_role("dialog").get_by_role("heading")).to_have_text(
                            "Quản lý core"
                        )
                        page.screenshot(
                            path=str(OUT / "panel-vi.png"), full_page=True, animations="disabled"
                        )
                        result = {
                            "passed": True,
                            "real_api_dom": True,
                            "archive": "synthetic checksum-pinned zip, NOT a real core",
                            "confirmation_guard": True,
                            "backup_and_apply": True,
                            "dual_confirmation": True,
                            "unapproved_rejected": True,
                            "auth_guard": True,
                            "synthetic_session_unchanged": True,
                            "active_cache_absent": True,
                            "only_synthetic_profile_applied": True,
                            "vi_en": True,
                            "browser": browser.version,
                        }
                        (OUT / "result.json").write_text(json.dumps(result, indent=2))
                        print(json.dumps(result), flush=True)
                        if hold:
                            print("HOLD", origin, flush=True)
                            until = time.monotonic() + 180
                            while time.monotonic() < until and not (OUT / "stop").exists():
                                time.sleep(1)
                    finally:
                        browser.close()
            finally:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    print("owned temporary runtime removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=int)
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--desktop", action="store_true")
    args = parser.parse_args()
    if args.serve:
        fixture_service()
        import uvicorn

        from src.api.app import create_app
        from src.core.container import Container

        container = Container()
        container.profile_manager.add_profile("core-qa", "None", "macos", engine="chrome")
        uvicorn.run(create_app(container), host="127.0.0.1", port=args.serve, log_level="warning")
    elif args.desktop:
        # Native harness owns every path; never defaults to the user's store.
        with tempfile.TemporaryDirectory(prefix="ronc-core-native-qa-") as directory:
            temp = Path(directory)
            for name in [
                "PROFILES_FILE",
                "PROXY_POOL_FILE",
                "WARMING_FILE",
                "DATA_DIR",
                "LOG_DIR",
                "CORE_HOME",
                "CHROME_HOME",
                "CORE_BACKUP_HOME",
            ]:
                os.environ["FOXPROFILE_" + name] = str(temp / name.lower())
            archive = temp / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("chrome", b"synthetic installer only")
            os.environ["CORE_QA_ARCHIVE"] = str(archive)
            fixture_service()
            import flet as ft

            from src.core.container import Container
            from src.ui.dialogs.cores import open_core_manager

            container = Container()
            container.profile_manager.add_profile("native-qa", "None", "macos", engine="chrome")
            source = temp / "data_dir/native-qa"
            source.mkdir(exist_ok=True)
            (source / "session.fixture").write_bytes(b"synthetic native session")

            def desktop(page):
                page.title = "RonC Core Manager QA (temporary)"
                page.window.width = 1000
                page.window.height = 1000
                page.add(
                    ft.Button(
                        "Browser cores",
                        on_click=lambda _: open_core_manager(
                            page, container.profile_manager, container.browser_launcher
                        ),
                    )
                )
                open_core_manager(page, container.profile_manager, container.browser_launcher)

            print("NATIVE_QA_ONLY", directory, flush=True)
            ft.run(desktop)
    else:
        main(args.hold)
