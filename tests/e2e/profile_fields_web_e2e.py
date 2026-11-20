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
                    for new_tag in ("Research <safe>", "Antigravity", "Gemini Pixel Fam"):
                        page.locator("#tag-input").fill(new_tag)
                        page.get_by_role("button", name="Add tag", exact=True).click()
                    page.screenshot(path=str(OUT / "tags-modal.png"))
                    page.locator("#modal-root .btn-act").click()
                    # All 4 tags must be rendered directly without "+1 / +2" truncation
                    for visible_tag in (
                        "Gmail",
                        "Research <safe>",
                        "Antigravity",
                        "Gemini Pixel Fam",
                    ):
                        expect(row.locator(".c-tags")).to_contain_text(visible_tag)
                    assert "+1" not in row.locator(".c-tags").inner_text()
                    assert "+2" not in row.locator(".c-tags").inner_text()
                    # Backup Codes quick modal: synthetic canary sheet, on-demand load, generic toast
                    backup_sheet = (
                        "FIXTURE BACKUP CODES\n"
                        "1. 1111 0001        6. 1111 0006\n"
                        "2. 1111 0002        7. 1111 0007\n"
                        "3. 1111 0003        8. 1111 0008\n"
                        "4. 1111 0004        9. 1111 0009\n"
                        "5. 1111 0005       10. 1111 0010"
                    )
                    row.get_by_role("button", name="Backup Codes: Studio Gmail", exact=True).click()
                    expect(page.locator("#field-value")).to_be_enabled()
                    page.locator("#field-value").fill(backup_sheet)
                    expect(page.locator("[data-backup-code='1111 0001']")).to_be_visible()
                    expect(page.locator("[data-backup-code='1111 0010']")).to_be_visible()
                    page.locator("[data-backup-code='1111 0001']").click()
                    assert page.evaluate("navigator.clipboard.readText()") == "1111 0001"
                    expect(page.locator("#toast")).to_contain_text("Backup codes copied")
                    assert "1111 0001" not in page.locator("#toast").inner_text()
                    page.locator("#modal-root .btn-act").click()
                    expect(page.get_by_role("dialog")).to_have_count(0)
                    # Rows-per-page selector (5, 10, 20, 50, 100, 200) and horizontal scroll container
                    expect(page.locator("#sheet-scroll")).to_be_visible()
                    expect(page.locator("#page-size-select")).to_have_value("20")
                    assert page.locator("#page-size-select option").all_inner_texts() == [
                        "5",
                        "10",
                        "20",
                        "50",
                        "100",
                        "200",
                    ]
                    page.locator("#page-size-select").select_option("50")
                    expect(page.locator("#page-size-select")).to_have_value("50")
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
                    assert data["tags"] == [
                        "Gmail",
                        "Research <safe>",
                        "Antigravity",
                        "Gemini Pixel Fam",
                    ]
                    assert "backup_codes" not in data
                    assert data["has_backup_codes"] is True
                    bkp_res = page.request.get(
                        origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                        headers={"Authorization": "Bearer " + TOKEN},
                    )
                    assert bkp_res.status == 200
                    assert bkp_res.headers.get("cache-control") == "no-store"
                    assert bkp_res.json()["backup_codes"] == backup_sheet
                    # Two-editor stale backup guard + failed reveal error state + explicit clear
                    sheet_b = "1. 2222 0001\n2. 2222 0002"
                    ctx_b = browser.new_context(viewport={"width": 1280, "height": 800})
                    try:
                        page_b = ctx_b.new_page()
                        page_b.on("pageerror", lambda exc: errors.append(str(exc)))
                        page_b.goto(origin)
                        page_b.locator("#token").fill(TOKEN)
                        page_b.locator("#login-form button").click()
                        expect(page_b.locator("#app")).to_be_visible()

                        # Case 1: Tab A opens full editor unrevealed; Tab B updates sheet to B; Tab A edits Notes only
                        stale_patches = []

                        def _capture_patch(route):
                            if route.request.method == "PATCH":
                                stale_patches.append(json.loads(route.request.post_data or "{}"))
                            route.continue_()

                        page.route("**/api/v1/profiles/Studio%20renamed", _capture_patch)
                        renamed.get_by_role(
                            "button", name="Edit: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#f-reveal-backup-codes")).to_be_visible()

                        page_b.locator('[aria-label="Backup Codes: Studio renamed"]').click()
                        expect(page_b.locator("#field-value")).to_be_enabled()
                        page_b.locator("#field-value").fill(sheet_b)
                        page_b.locator("#modal-root .btn-act").click()
                        expect(page_b.get_by_role("dialog")).to_have_count(0)

                        page.locator("#f-notes").fill("Two-editor note A1")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert "backup_codes" not in stale_patches[-1]
                        assert (
                            page.request.get(
                                origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                                headers={"Authorization": "Bearer " + TOKEN},
                            ).json()["backup_codes"]
                            == sheet_b
                        )

                        # Case 2: Reveal load error (HTTP 500) keeps Save disabled in quick editor and preserves sheet B on full Notes edit
                        def _fail_backup_get(route):
                            if route.request.method == "GET":
                                route.fulfill(
                                    status=500,
                                    content_type="application/json",
                                    body=json.dumps({"detail": "injected-backup-500"}),
                                )
                            else:
                                route.continue_()

                        page.route(
                            "**/api/v1/profiles/Studio%20renamed/backup-codes", _fail_backup_get
                        )
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#modal-root [role=alert]")).to_contain_text(
                            "injected-backup-500"
                        )
                        expect(page.locator("#modal-root .btn-act")).to_be_disabled()
                        page.get_by_role("button", name="Cancel", exact=True).click()

                        renamed.get_by_role(
                            "button", name="Edit: Studio renamed", exact=True
                        ).click()
                        page.locator("#f-reveal-backup-codes").click()
                        expect(page.locator("#f-backup-err")).to_contain_text("injected-backup-500")
                        page.locator("#f-notes").fill("Two-editor note A2")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert "backup_codes" not in stale_patches[-1]
                        page.unroute(
                            "**/api/v1/profiles/Studio%20renamed/backup-codes", _fail_backup_get
                        )
                        page.unroute("**/api/v1/profiles/Studio%20renamed", _capture_patch)
                        assert (
                            page.request.get(
                                origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                                headers={"Authorization": "Bearer " + TOKEN},
                            ).json()["backup_codes"]
                            == sheet_b
                        )

                        # Case 3: Reveal & explicit clear removes sheet
                        renamed.get_by_role(
                            "button", name="Edit: Studio renamed", exact=True
                        ).click()
                        page.locator("#f-reveal-backup-codes").click()
                        expect(page.locator("#f-backup-codes")).to_have_value(sheet_b)
                        page.locator("#f-backup-codes").fill("")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        cleared = page.request.get(
                            origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                            headers={"Authorization": "Bearer " + TOKEN},
                        ).json()
                        assert (
                            cleared["has_backup_codes"] is False and cleared["backup_codes"] is None
                        )

                        # Restore backup codes for downstream checks
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        page.locator("#field-value").fill(backup_sheet)
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)

                        # Case 4: Quick editor no-op Save (including edit-then-revert) sends no PATCH and preserves concurrent sheet B
                        quick_patches = []

                        def _capture_quick_patch(route):
                            if route.request.method == "PATCH":
                                quick_patches.append(json.loads(route.request.post_data or "{}"))
                            route.continue_()

                        page.route("**/api/v1/profiles/Studio%20renamed", _capture_quick_patch)
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        expect(page.locator("#field-value")).to_have_value(backup_sheet)

                        page_b.locator('[aria-label="Backup Codes: Studio renamed"]').click()
                        expect(page_b.locator("#field-value")).to_be_enabled()
                        page_b.locator("#field-value").fill(sheet_b)
                        page_b.locator("#modal-root .btn-act").click()
                        expect(page_b.get_by_role("dialog")).to_have_count(0)

                        page.locator("#field-value").fill("1. 9999 0001")
                        page.locator("#field-value").fill("  " + backup_sheet + "\n")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert len(quick_patches) == 0
                        assert (
                            page.request.get(
                                origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                                headers={"Authorization": "Bearer " + TOKEN},
                            ).json()["backup_codes"]
                            == sheet_b
                        )

                        # Case 5: Quick editor intentional clear ("") and real edit still persist
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        expect(page.locator("#field-value")).to_have_value(sheet_b)
                        page.locator("#field-value").fill("")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert len(quick_patches) == 1 and quick_patches[-1]["backup_codes"] == ""
                        quick_cleared = page.request.get(
                            origin + "/api/v1/profiles/Studio%20renamed/backup-codes",
                            headers={"Authorization": "Bearer " + TOKEN},
                        ).json()
                        assert (
                            quick_cleared["has_backup_codes"] is False
                            and quick_cleared["backup_codes"] is None
                        )

                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        page.locator("#field-value").fill(backup_sheet)
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert (
                            len(quick_patches) == 2
                            and quick_patches[-1]["backup_codes"] == backup_sheet
                        )
                        page.unroute("**/api/v1/profiles/Studio%20renamed", _capture_quick_patch)

                        # Case 6: Login transition on 401 and Logout removes open quick/full Backup Codes modal & ignores late in-flight GET
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        expect(page.locator("[data-backup-code='1111 0001']")).to_be_visible()

                        fail_once = {"done": False}

                        def _one_401(route):
                            if not fail_once["done"] and route.request.method == "GET":
                                fail_once["done"] = True
                                route.fulfill(
                                    status=401,
                                    content_type="application/json",
                                    body=json.dumps({"detail": "expired"}),
                                )
                            else:
                                route.continue_()

                        page.route("**/api/v1/profiles", _one_401)
                        page.evaluate("refresh()")
                        expect(page.locator("#login")).to_be_visible()
                        expect(page.locator("#app")).to_be_hidden()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        expect(page.locator("#field-value")).to_have_count(0)
                        expect(page.locator("[data-backup-code]")).to_have_count(0)
                        expect(page.locator("#copy-all-backup-codes")).to_have_count(0)
                        page.unroute("**/api/v1/profiles", _one_401)

                        # Sign back in -> open full editor + reveal -> Logout removes modal/sheet from DOM
                        page.locator("#token").fill(TOKEN)
                        page.locator("#login-form button").click()
                        expect(page.locator("#app")).to_be_visible()
                        renamed.get_by_role(
                            "button", name="Edit: Studio renamed", exact=True
                        ).click()
                        page.locator("#f-reveal-backup-codes").click()
                        expect(page.locator("#f-backup-codes")).to_be_visible()
                        expect(page.locator("#f-backup-codes")).to_have_value(backup_sheet)
                        page.evaluate("$('logout-btn').click()")
                        expect(page.locator("#login")).to_be_visible()
                        expect(page.locator("#app")).to_be_hidden()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        expect(page.locator("#f-backup-codes")).to_have_count(0)

                        # Sign back in -> delayed GET /backup-codes finishing after 401 transition does not re-render stale state
                        page.locator("#token").fill(TOKEN)
                        page.locator("#login-form button").click()
                        expect(page.locator("#app")).to_be_visible()

                        def _delayed_bkp_after_login(route):
                            page.evaluate("showLogin()")
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                headers={"Cache-Control": "no-store"},
                                body=json.dumps(
                                    {
                                        "name": "Studio renamed",
                                        "has_backup_codes": True,
                                        "backup_codes": backup_sheet,
                                    }
                                ),
                            )

                        page.route(
                            "**/api/v1/profiles/Studio%20renamed/backup-codes",
                            _delayed_bkp_after_login,
                        )
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#login")).to_be_visible()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        expect(page.locator("[data-backup-code]")).to_have_count(0)
                        page.unroute(
                            "**/api/v1/profiles/Studio%20renamed/backup-codes",
                            _delayed_bkp_after_login,
                        )
                        page.locator("#token").fill(TOKEN)
                        page.locator("#login-form button").click()
                        expect(page.locator("#app")).to_be_visible()

                        # Case 7: Stale prior-session GET /backup-codes returning 401 after Escape -> Logout -> Login does not sign out or discard unsaved draft
                        held_stale = []

                        def _stash_stale_bkp(route):
                            if not held_stale and route.request.method == "GET":
                                held_stale.append(route)
                            else:
                                route.continue_()

                        page.route(
                            "**/api/v1/profiles/Studio%20renamed/backup-codes",
                            _stash_stale_bkp,
                        )
                        try:
                            renamed.get_by_role(
                                "button", name="Backup Codes: Studio renamed", exact=True
                            ).click()
                            expect(page.locator("#modal-root .msg.info")).to_be_visible()
                            assert len(held_stale) == 1
                            page.keyboard.press("Escape")
                            expect(page.get_by_role("dialog")).to_have_count(0)
                            page.evaluate("$('logout-btn').click()")
                            expect(page.locator("#login")).to_be_visible()
                            page.locator("#token").fill(TOKEN)
                            page.locator("#login-form button").click()
                            expect(page.locator("#app")).to_be_visible()

                            renamed.get_by_role(
                                "button", name="Edit Notes: Studio renamed", exact=True
                            ).click()
                            page.locator("#field-value").fill("Unsaved draft session 2")
                            held_stale.pop().fulfill(
                                status=401,
                                content_type="application/json",
                                body=json.dumps({"detail": "stale-session-401"}),
                            )
                        finally:
                            page.unroute(
                                "**/api/v1/profiles/Studio%20renamed/backup-codes",
                                _stash_stale_bkp,
                            )
                        page.wait_for_timeout(300)
                        expect(page.locator("#app")).to_be_visible()
                        expect(page.locator("#login")).to_be_hidden()
                        expect(page.get_by_role("dialog")).to_have_count(1)
                        expect(page.locator("#field-value")).to_have_value(
                            "Unsaved draft session 2"
                        )
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        assert (
                            page.request.get(
                                origin + "/api/v1/profiles/Studio%20renamed",
                                headers={"Authorization": "Bearer " + TOKEN},
                            ).json()["notes"]
                            == "Unsaved draft session 2"
                        )

                        # Control: current-epoch 401 still closes active Backup Codes modal and wipes revealed state
                        renamed.get_by_role(
                            "button", name="Backup Codes: Studio renamed", exact=True
                        ).click()
                        expect(page.locator("#field-value")).to_be_enabled()
                        expect(page.locator("[data-backup-code='1111 0001']")).to_be_visible()
                        page.route("**/api/v1/profiles", _one_401)
                        fail_once["done"] = False
                        page.evaluate("refresh()")
                        expect(page.locator("#login")).to_be_visible()
                        expect(page.locator("#app")).to_be_hidden()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        expect(page.locator("#field-value")).to_have_count(0)
                        expect(page.locator("[data-backup-code]")).to_have_count(0)
                        expect(page.locator("#copy-all-backup-codes")).to_have_count(0)
                        page.unroute("**/api/v1/profiles", _one_401)

                        # Re-login and run Case 8: overlapping failed login A cannot erase valid session B token
                        page.locator("#token").fill(TOKEN)
                        page.locator("#login-form button").click()
                        expect(page.locator("#app")).to_be_visible()

                        page.evaluate("$('logout-btn').click()")
                        expect(page.locator("#login")).to_be_visible()
                        held_bad_login = []

                        def _stash_bad_login(route):
                            auth_hdr = route.request.headers.get("authorization", "")
                            if (
                                not held_bad_login
                                and route.request.method == "GET"
                                and "wrong-overlap-token" in auth_hdr
                            ):
                                held_bad_login.append(route)
                            else:
                                route.continue_()

                        page.route("**/api/v1/profiles", _stash_bad_login)
                        try:
                            page.locator("#token").fill("wrong-overlap-token")
                            page.locator("#login-form button").click()
                            for _ in range(50):
                                if held_bad_login:
                                    break
                                page.wait_for_timeout(20)
                            assert len(held_bad_login) == 1

                            page.locator("#token").fill(TOKEN)
                            page.locator("#login-form button").click()
                            expect(page.locator("#app")).to_be_visible()

                            renamed.get_by_role(
                                "button", name="Edit Notes: Studio renamed", exact=True
                            ).click()
                            page.locator("#field-value").fill("Draft after overlapping login")

                            held_bad_login.pop().continue_()
                        finally:
                            page.unroute("**/api/v1/profiles", _stash_bad_login)

                        page.wait_for_timeout(300)
                        expect(page.locator("#app")).to_be_visible()
                        expect(page.locator("#login")).to_be_hidden()
                        expect(page.get_by_role("dialog")).to_have_count(1)
                        expect(page.locator("#field-value")).to_have_value(
                            "Draft after overlapping login"
                        )
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        expect(page.locator("#app")).to_be_visible()
                        assert (
                            page.request.get(
                                origin + "/api/v1/profiles/Studio%20renamed",
                                headers={"Authorization": "Bearer " + TOKEN},
                            ).json()["notes"]
                            == "Draft after overlapping login"
                        )

                        # Restore notes for downstream assertions
                        renamed.get_by_role(
                            "button", name="Edit Notes: Studio renamed", exact=True
                        ).click()
                        page.locator("#field-value").fill("Two-editor note A2")
                        page.locator("#modal-root .btn-act").click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                    finally:
                        ctx_b.close()
                    assert (
                        page.request.get(
                            origin + "/api/v1/profiles/Studio%20renamed",
                            headers={"Authorization": "Bearer " + TOKEN},
                        ).json()["notes"]
                        == "Two-editor note A2"
                    )
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
                    # Toolbar Running filter button toggles running-only filter cleanly
                    expect(page.locator("#running-filter-btn")).to_contain_text("Running (1)")
                    page.locator("#running-filter-btn").click()
                    expect(page.locator("#running-filter-btn")).to_have_attribute(
                        "aria-pressed", "true"
                    )
                    expect(page.locator(".row.body")).to_have_count(1)
                    expect(page.locator("#total-count")).to_contain_text("Total: 1")
                    page.locator("#running-filter-btn").click()
                    expect(page.locator("#running-filter-btn")).to_have_attribute(
                        "aria-pressed", "false"
                    )
                    expect(page.locator(".row.body")).to_have_count(2)
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
