"""Check the actual Flet controls over its optional web host, temporary data only.

Start profiles_fixture.py --flet with isolated optional flet-web matching Flet.
"""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(__file__).resolve().parents[2] / "artifacts/launch-ui-2026-11-09/flet"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
        )
        try:
            page = browser.new_page(
                viewport={"width": 1440, "height": 900},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page.goto("http://127.0.0.1:18764", wait_until="networkidle")
            page.locator("flt-semantics-placeholder").evaluate("el => el.click()")
            expect(page.get_by_text("Studio Gmail", exact=False).first).to_be_visible()
            page.locator('[aria-label^="Edit Notes"] [role="button"]').first.click()
            box = page.get_by_role("textbox", name="Notes / Recovery Info", exact=True)
            expect(box).to_be_visible()
            box.click()
            box.press("Meta+A")
            page.keyboard.type("Flet reminder saved")
            page.get_by_role("button", name="Save Changes", exact=True).click()
            expect(page.get_by_text("Flet reminder saved", exact=False).first).to_be_visible()
            page.locator('[aria-label="Edit Tag / Group"] [role="button"]').first.click()
            expect(page.get_by_role("textbox", name="Tag / Group", exact=True)).to_be_visible()
            page.get_by_role("textbox", name="Tag / Group", exact=True).click()
            page.keyboard.type("QA")
            page.get_by_role("textbox", name="Tag / Group", exact=True).press("Enter")
            expect(page.get_by_text("QA", exact=False).first).to_be_visible()
            remove = page.locator('[aria-label="Remove Gemini Pixel"] [role="button"]')
            expect(remove).to_be_enabled()
            remove.click()
            expect(page.locator('[aria-label="Remove Gemini Pixel"]')).to_have_count(0)
            long_tag = "W" * 64
            long_remove = page.locator(f'[aria-label="Remove {long_tag}"] [role="button"]')
            expect(long_remove).to_be_enabled()
            expect(long_remove).to_be_in_viewport()
            long_remove.click()
            expect(long_remove).to_have_count(0)
            page.screenshot(path=str(OUT / "tags-modal.png"))
            page.get_by_role("button", name="Save Changes", exact=True).click()
            expect(page.get_by_role("button", name="Save Changes", exact=True)).to_have_count(0)

            def edit_text(label, value):
                page.locator(f'[aria-label="{label}"] [role="button"]').first.click()
                field = page.locator(
                    "flt-semantics input:not([aria-label='Search by name or proxy']), flt-semantics textarea:not([disabled])"
                ).last
                expect(field).to_be_visible()
                field.click()
                field.press("Meta+A")
                page.keyboard.type(value)
                page.get_by_role("button", name="Save Changes", exact=True).click()
                expect(page.get_by_role("button", name="Save Changes", exact=True)).to_have_count(0)

            edit_text("Edit Profile", "Studio renamed")
            expect(page.get_by_text("Studio renamed", exact=False).first).to_be_visible()
            expect(page.get_by_text("studio@example.test", exact=False).first).to_be_visible()
            edit_text("Edit Proxy", "http://127.0.0.1:8088")
            expect(page.get_by_text("127.0.0.1:8088", exact=False).first).to_be_visible()
            edit_text("Edit ID", "2")
            for width in (1440, 1180):
                page.set_viewport_size({"width": width, "height": 900})
                for theme in ("Light", "Dark"):
                    # Top toggle always switches to named theme; choose sidebar only if present.
                    candidates = page.get_by_role("button", name=theme, exact=True)
                    if candidates.count():
                        candidates.first.click()
                        expect(
                            page.get_by_role(
                                "button", name="Dark" if theme == "Light" else "Light", exact=True
                            ).first
                        ).to_be_visible()
                    page.screenshot(path=str(OUT / f"profiles-{width}-{theme.lower()}.png"))
            page.mouse.move(800, 260)
            page.mouse.wheel(700, 0)
            expect(page.get_by_role("button", name="Stop", exact=True)).to_be_in_viewport()
            page.screenshot(path=str(OUT / "profiles-1180-scrolled.png"))
            page.locator('[aria-label="Copy Gmail / user"] [role="button"]').first.click()
            page.wait_for_function(
                "navigator.clipboard.readText().then(t => t === 'studio@example.test')"
            )
            (OUT / "result.txt").write_text(
                "PASS actual Flet controls: copy Gmail, notes save, multi-tag popup/save, light/dark1440/1180. Optional flet-web served temporary store, no real profiles or launches.\n"
            )
            print("PASS Flet rendered controls and field saves")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
