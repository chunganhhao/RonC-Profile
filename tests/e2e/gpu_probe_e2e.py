"""Actual runner with temporary data: hidden probe followed by one headed profile."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/headless-probe-adapter-2026-11-09"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ronc-probe-adapter-") as directory:
        for key in (
            "DATA_DIR",
            "PROFILES_FILE",
            "LOG_DIR",
            "CHROME_HOME",
            "CORE_HOME",
            "CORE_BACKUP_HOME",
        ):
            os.environ["FOXPROFILE_" + key] = str(Path(directory) / key.lower())
        os.environ["FOXPROFILE_HEADLESS"] = "false"
        os.environ["FOXPROFILE_RESTORE_TABS"] = "false"
        sys.path.insert(0, str(ROOT))
        from playwright.async_api import BrowserType as AsyncBrowserType
        from playwright.sync_api import BrowserType as SyncBrowserType

        from src.services.browser import runner

        probes, profiles = [], []
        original_probe = SyncBrowserType.launch
        original_profile = AsyncBrowserType.launch_persistent_context

        def launch_probe(self, *args, **kwargs):
            probes.append({"headless": kwargs.get("headless"), "persistent": False})
            return original_probe(self, *args, **kwargs)

        async def launch_profile(self, *args, **kwargs):
            profiles.append(
                {
                    "headless": kwargs.get("headless"),
                    "user_data_dir": str(kwargs.get("user_data_dir", args[0] if args else "")),
                }
            )
            return await original_profile(self, *args, **kwargs)

        SyncBrowserType.launch = launch_probe
        AsyncBrowserType.launch_persistent_context = launch_profile
        # Offline harness only: never rewrite the installed search policy or query exit IP.
        runner.ensure_google_search = lambda: False
        runner.exit_ip = lambda *_: False
        original_serve = runner.control.serve
        original_print = runner._safe_print
        runner._safe_print = lambda value: original_print(
            "CONTROL:[temporary]" if value.startswith("CONTROL:") else value
        )

        async def checked_serve(context):
            page = context.pages[0]
            await page.set_content(
                "<!doctype html><title>RonC temporary headed profile</title><h1>Probe hidden; profile headed</h1>"
            )
            assert await page.title() == "RonC temporary headed profile"
            assert probes == [{"headless": True, "persistent": False}], probes
            assert len(profiles) == 1 and profiles[0]["headless"] is False, profiles
            assert Path(profiles[0]["user_data_dir"]).is_relative_to(directory)
            await page.screenshot(path=str(OUT / "headed-profile.png"))
            server = await original_serve(context)
            runner._shutdown.set()
            return server

        runner.control.serve = checked_serve
        sys.argv = ["runner.py", "temporary-probe", "None", "macos", "UTC", "en-US"]
        try:
            code = asyncio.run(runner._async_main())
            assert code == 0, code
            (OUT / "e2e-result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "probe_launches": probes,
                        "profile_headless": [p["headless"] for p in profiles],
                        "temporary_profile_only": True,
                        "runner_exit": code,
                        "real_profile_untouched": True,
                    },
                    indent=2,
                )
            )
            print(
                "PASS: one headless GPU probe, one headed temporary profile; runner closed owned browsers"
            )
        finally:
            SyncBrowserType.launch = original_probe
            AsyncBrowserType.launch_persistent_context = original_profile


if __name__ == "__main__":
    main()
