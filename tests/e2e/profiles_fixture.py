"""Owned temporary server for Web UI and Flet rendered checks; never production data."""

import argparse
import json
import os
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--flet", action="store_true")
    parser.add_argument("--running-display", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ronc-ui-fixture-") as temp:
        root = Path(temp)
        for key, name in {
            "PROFILES_FILE": "profiles.json",
            "PROXY_POOL_FILE": "proxies.json",
            "WARMING_FILE": "warming.json",
            "DATA_DIR": "data",
            "LOG_DIR": "logs",
            "CORE_HOME": "cores",
            "CORE_BACKUP_HOME": "backups",
            "CHROME_HOME": "chrome",
        }.items():
            os.environ["FOXPROFILE_" + key] = str(root / name)
        os.environ["FOXPROFILE_API_TOKEN"] = "fixture-token-" + "t" * 32
        os.environ["FOXPROFILE_LANG"] = "en"
        data = {}
        for name, uid, group in [
            ("Studio Gmail", "studio@example.test", "Gmail"),
            ("Research", "research@example.test", "AGY"),
        ]:
            data[name] = {
                "name": name,
                "os_type": "macos",
                "account_uid": uid,
                "group": group,
                "tags": [group, "Gemini Pixel"],
                "notes": "Review before signing in",
            }
        if args.flet:
            data["Studio Gmail"]["tags"].append("W" * 64)
        (root / "profiles.json").write_text(json.dumps(data))
        if args.flet:
            import flet as ft

            from src.core.container import Container
            from src.ui.app import App
            from src.ui.theme.page import ASSETS_DIR

            container = Container()

            def fixture_main(page):
                gui = App(container)
                if args.running_display:
                    gui.bl.is_running = lambda name: name == "Research"
                    gui.bl.running_profile_names = lambda: {"Research"}
                gui._main(page)
                page.title = "RonC Profile UI fixture"
                page.update()

            os.environ["FLET_FORCE_WEB_SERVER"] = "1"
            ft.run(fixture_main, view=None, host="127.0.0.1", port=args.port, assets_dir=ASSETS_DIR)
        else:
            import uvicorn

            from src.api.app import create_app
            from src.core.container import Container

            uvicorn.run(
                create_app(Container()), host="127.0.0.1", port=args.port, log_level="warning"
            )


if __name__ == "__main__":
    main()
