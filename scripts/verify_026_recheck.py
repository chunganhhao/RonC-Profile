"""Isolated verification script for Codex 026 recheck fixes & save-contract audit.

Spawns an isolated child process with all 10 `FOXPROFILE_*` env vars rooted in one
`TemporaryDirectory`, verifies real `profiles.json` is never modified before/after,
executes live Run #19 headless Chromium launches, Playwright HTTP-500 & two-editor
stale-seed Web DOM E2E, Flet CPU-only save-contract checks, platform-less API PATCH
checks, and atomic refusal of incompatible 026 migrations.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
REAL_PROFILES = REPO / "profiles.json"
RUN19_EXE = (
    pathlib.Path.home()
    / "Library/Caches/foxprofile/chrome/148.0.7778.215/Chromium.app/Contents/MacOS/Chromium"
)
TRACKED_FILES: tuple[str, ...] = (
    "chrome_engine/engine.py",
    "chrome_engine/persona.py",
    "src/services/browser/fingerprint.py",
    "src/services/profile/manager.py",
    "src/api/routes/profiles.py",
    "src/ui/actions/profile.py",
    "src/ui/dialogs/profile.py",
    "src/web/index.html",
    "chrome_engine/tests/test_engine_unit.py",
    "tests/test_engine.py",
    "tests/test_totp_and_hardware.py",
    "scripts/verify_026_recheck.py",
    "native/chromium-148/patches/024-webgl-readback-parity.patch",
    "native/chromium-148/patches/025-audio-clientrects-parity.patch",
    "native/chromium-148/patches/026-apple-silicon-device-coherence.patch",
)


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_tracked_digests() -> dict[str, str]:
    return {rel: _sha256_file(REPO / rel) for rel in TRACKED_FILES}


def _resolve_out_path(argv: list[str]) -> pathlib.Path | None:
    if len(argv) == 1:
        return None
    if len(argv) == 3 and argv[1] == "--out" and argv[2].strip():
        return pathlib.Path(argv[2])
    sys.stderr.write("Usage: verify_026_recheck.py [--out <new-file.json>]\n")
    raise SystemExit(2)


def _run_parent(out_path: pathlib.Path | None = None) -> int:
    if out_path is not None and out_path.exists():
        sys.stderr.write(f"Refusing to overwrite existing evidence file: {out_path}\n")
        return 2

    real_before_sha = _sha256_file(REAL_PROFILES)
    real_before_count = len(json.loads(REAL_PROFILES.read_text(encoding="utf-8")))
    tracked_before = _snapshot_tracked_digests()

    with tempfile.TemporaryDirectory(prefix="foxprofile_026_recheck_") as tmpdir:
        tmp = pathlib.Path(tmpdir)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FOXPROFILE_")}
        env.update(
            {
                "FOXPROFILE_API_TOKEN": "",
                "FOXPROFILE_PROFILES_FILE": str(tmp / "profiles.json"),
                "FOXPROFILE_DATA_DIR": str(tmp / "camoufox_data"),
                "FOXPROFILE_EXTENSIONS_FILE": str(tmp / "extensions.json"),
                "FOXPROFILE_EXTENSIONS_DIR": str(tmp / "extensions"),
                "FOXPROFILE_LOG_DIR": str(tmp / "logs"),
                "FOXPROFILE_PROXY_POOL_FILE": str(tmp / "proxies.json"),
                "FOXPROFILE_WARMING_FILE": str(tmp / "warming.json"),
                "FOXPROFILE_CORE_HOME": str(tmp / "core-staging"),
                "FOXPROFILE_CORE_BACKUP_HOME": str(tmp / "core-backups"),
                "FOXPROFILE_CHROME_HOME": str(tmp / "chrome-home"),
                "FOXPROFILE_CHROME_EXECUTABLE": str(RUN19_EXE),
                "FOXPROFILE_HEADLESS": "true",
                "PYTHONPATH": str(REPO),
            }
        )
        proc = subprocess.run(
            [sys.executable, __file__, "--child", tmpdir],
            cwd=tmpdir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode or 1
        child_report = json.loads((tmp / "child_report.json").read_text(encoding="utf-8"))
        launch_timing_lines = [
            line for line in proc.stdout.splitlines() if line.startswith("LAUNCH_TIMING ")
        ]

    real_after_sha = _sha256_file(REAL_PROFILES)
    real_after_count = len(json.loads(REAL_PROFILES.read_text(encoding="utf-8")))
    assert real_after_sha == real_before_sha, (
        f"Real profiles.json mutated: {real_before_sha} -> {real_after_sha}"
    )
    assert real_after_count == real_before_count, (
        f"Real profile count mutated: {real_before_count} -> {real_after_count}"
    )

    tracked_after = _snapshot_tracked_digests()
    changed_tracked = sorted(
        rel for rel, before_sha in tracked_before.items() if tracked_after.get(rel) != before_sha
    )
    if changed_tracked:
        sys.stderr.write(
            f"Tracked files changed during verification run: {', '.join(changed_tracked)}\n"
        )
        return 2

    report = {
        "date": "2026-11-19",
        "scope": (
            "source_unit + save_contract + persistence_validation + live_run19 + "
            "fault_injected_web_dom_e2e (live_026_candidate=NOT_RUN)"
        ),
        "isolated_temp_env_vars": sorted(k for k in env if k.startswith("FOXPROFILE_")),
        "tracked_file_sha256": tracked_before,
        "real_profiles_guard": {
            "path": str(REAL_PROFILES),
            "before_sha256": real_before_sha,
            "after_sha256": real_after_sha,
            "count_before": real_before_count,
            "count_after": real_after_count,
            "unchanged": real_before_sha == real_after_sha
            and real_before_count == real_after_count,
        },
        "launch_timing_lines": launch_timing_lines,
        **child_report,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with out_path.open("x", encoding="utf-8") as f:
                f.write(rendered)
        except FileExistsError:
            sys.stderr.write(f"Refusing to overwrite existing evidence file: {out_path}\n")
            return 2
    sys.stdout.write(rendered)
    return 0


async def _run_child_async(tmp: pathlib.Path) -> dict:
    import asyncio
    import hashlib as _hl
    import socket

    import flet as ft
    import uvicorn
    from fastapi.testclient import TestClient
    from playwright.async_api import async_playwright

    from chrome_engine import engine as engine_mod
    from chrome_engine import fetch as _fetch
    from chrome_engine import persona as chrome_persona
    from chrome_engine.engine import ChromeEngine, EngineError
    from chrome_engine.native_bundle import bundle_sha256 as _bsha
    from src.api.app import create_app
    from src.core import config as cfg
    from src.core.container import Container
    from src.services.browser import chrome_prefetch, cores, fingerprint
    from src.ui.dialogs.profile import open_profile_dialog

    for name in (
        "PROFILES_FILE",
        "DATA_DIR",
        "EXTENSIONS_FILE",
        "EXTENSIONS_DIR",
        "LOG_DIR",
        "PROXY_POOL_FILE",
        "WARMING_FILE",
    ):
        val = pathlib.Path(getattr(cfg, name)).resolve()
        assert val.is_relative_to(tmp.resolve()), f"{name}={val} escaped {tmp}"

    chrome_prefetch.start = lambda log=None: False
    app = create_app(Container())
    client = TestClient(app, base_url="http://127.0.0.1")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.setblocking(False)
    port = sock.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)

    console_errors: list[str] = []
    injected_500_console_count = 0
    page_errors: list[str] = []
    captured_patches: list[dict] = []

    try:
        # 1. Live Run #19: Web-shaped POST -> baseline persona -> real headless Chromium launch.
        r_post = client.post(
            "/api/v1/profiles",
            json={
                "name": "live-web-mac",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 3, "platform": "macos"},
            },
        )
        assert r_post.status_code == 201, r_post.text
        web_dir = tmp / "camoufox_data/live-web-mac"
        web_persona = chrome_persona.load(web_dir)
        assert web_persona is not None
        assert not web_persona.requires_apple_silicon_026
        assert web_persona.device_model is None

        async with ChromeEngine(
            web_dir, platform="macos", executable=RUN19_EXE, headless=True
        ) as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(f"http://127.0.0.1:{port}/api/v1/health", wait_until="load")
                live_web_eval = await page.evaluate(
                    "() => ({ hc: navigator.hardwareConcurrency, platform: navigator.platform })"
                )
            finally:
                await page.close()

        # 2. Live Run #19: Legacy baseline (`16` -> `Auto`) keeps legacy baseline -> real launch.
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": "live-legacy-mac", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        legacy_dir = tmp / "camoufox_data/live-legacy-mac"
        chrome_persona.save(
            legacy_dir,
            chrome_persona.Persona(
                seed=45,
                platform="macos",
                platform_version="14.6.1",
                brand="Chrome",
                hardware_concurrency=16,
            ),
        )
        r_auto = client.patch(
            "/api/v1/profiles/live-legacy-mac",
            json={
                "initial_fingerprint": {
                    "seed": 45,
                    "platform": "macos",
                    "hardware_concurrency": None,
                }
            },
        )
        assert r_auto.status_code == 200, r_auto.text
        legacy_persona = chrome_persona.load(legacy_dir)
        assert legacy_persona is not None
        assert not legacy_persona.requires_apple_silicon_026
        assert legacy_persona.device_model is None
        expected_legacy_cores = chrome_persona.default_cpu_for_persona(
            "macos", 45, coherence_026=False
        )
        assert legacy_persona.hardware_concurrency == expected_legacy_cores

        async with ChromeEngine(
            legacy_dir, platform="macos", executable=RUN19_EXE, headless=True
        ) as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(f"http://127.0.0.1:{port}/api/v1/health", wait_until="load")
                live_legacy_eval = await page.evaluate(
                    "() => ({ hc: navigator.hardwareConcurrency, platform: navigator.platform })"
                )
            finally:
                await page.close()
        assert live_legacy_eval["hc"] == expected_legacy_cores

        # 3. Live Run #19 negative control: explicit 026 persona fails closed & keeps bytes untouched.
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": "live-explicit-026", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        exp_dir = tmp / "camoufox_data/live-explicit-026"
        p026 = fingerprint.generate_chrome_persona("macos", seed=3, coherence_026=True)
        fingerprint.save_chrome_persona(str(exp_dir), p026, coherence_026=True)
        bytes_before = chrome_persona.persona_path(exp_dir).read_bytes()
        refused_msg = ""
        try:
            async with ChromeEngine(exp_dir, platform="macos", executable=RUN19_EXE, headless=True):
                raise AssertionError("Expected Run #19 EngineError for explicit 026 persona")
        except EngineError as exc:
            refused_msg = str(exc)
        assert "requires a verified 026+" in refused_msg
        assert chrome_persona.persona_path(exp_dir).read_bytes() == bytes_before

        # 4. Fault-injected Web DOM E2E: HTTP 500 on GET /fingerprint -> CPU-only edit preserves seed/model/brand.
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": "p2-dom-mac", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        dom_dir = tmp / "camoufox_data/p2-dom-mac"
        p2_init = fingerprint.generate_chrome_persona(
            "macos", seed=3, cpu_cores=8, coherence_026=True
        )
        p2_init["brand"] = "Edge"
        fingerprint.save_chrome_persona(str(dom_dir), p2_init, coherence_026=True)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(executable_path=str(RUN19_EXE), headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            def _on_console(msg):
                nonlocal injected_500_console_count
                if msg.type != "error":
                    return
                if "500" in msg.text:
                    injected_500_console_count += 1
                else:
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            async def _handle_fp(route):
                if route.request.method == "GET":
                    await route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps({"detail": "injected-fp-500"}),
                    )
                    return
                await route.continue_()

            async def _spy_patch(route):
                if route.request.method == "PATCH":
                    captured_patches.append(json.loads(route.request.post_data or "{}"))
                await route.continue_()

            await page.route("**/api/v1/profiles/*/fingerprint", _handle_fp)
            await page.route("**/api/v1/profiles/p2-dom-mac", _spy_patch)

            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            edit_btn = page.locator(
                'button[aria-label="Edit: p2-dom-mac"], button[aria-label="Sửa: p2-dom-mac"]'
            ).first
            await edit_btn.wait_for(state="visible")
            await edit_btn.click()
            await page.wait_for_selector(".modal #f-cores")

            saved_badge_text = await page.locator(".modal .note.calm b").first.inner_text()
            assert "#SEED-SAVED" in saved_badge_text, saved_badge_text

            await page.select_option("#f-cores", "12")
            await page.locator(".modal-foot .btn-act").click()
            await page.wait_for_selector(".modal", state="detached")

            saved_after_cpu = fingerprint.load_saved_chrome_persona(str(dom_dir))
            assert saved_after_cpu is not None
            assert saved_after_cpu["seed"] == 3
            assert saved_after_cpu["device_model"] == p2_init["device_model"]
            assert saved_after_cpu["screen_width"] == p2_init["screen_width"]
            assert saved_after_cpu["brand"] == "Edge"
            assert saved_after_cpu["hardware_concurrency"] == 12
            assert "seed" not in captured_patches[0]["initial_fingerprint"]

            await edit_btn.wait_for(state="visible")
            await edit_btn.click()
            await page.wait_for_selector(".modal #f-roll-fp")
            await page.click("#f-roll-fp")
            rolled_badge_text = await page.locator(".modal .note.calm b").first.inner_text()
            assert "#SEED-" in rolled_badge_text and "#SEED-SAVED" not in rolled_badge_text
            await page.locator(".modal-foot .btn-act").click()
            await page.wait_for_selector(".modal", state="detached")
            assert isinstance(captured_patches[1]["initial_fingerprint"].get("seed"), int)

            await context.close()

            # 5. Two-editor stale-seed Playwright Web DOM E2E.
            assert (
                client.post(
                    "/api/v1/profiles",
                    json={"name": "p2-stale-dom", "os_type": "macos", "engine": "chrome"},
                ).status_code
                == 201
            )
            stale_dir = tmp / "camoufox_data/p2-stale-dom"
            stale_init = fingerprint.generate_chrome_persona(
                "macos", seed=3, cpu_cores=8, coherence_026=True
            )
            stale_init["brand"] = "Edge"
            fingerprint.save_chrome_persona(str(stale_dir), stale_init, coherence_026=True)

            stale_patches: list[dict] = []
            ctx2 = await browser.new_context()
            page_a = await ctx2.new_page()
            page_b = await ctx2.new_page()
            for p_err_target in (page_a, page_b):
                p_err_target.on("console", _on_console)
                p_err_target.on("pageerror", lambda err: page_errors.append(str(err)))

            async def _spy_stale_patch(route):
                if route.request.method == "PATCH":
                    stale_patches.append(json.loads(route.request.post_data or "{}"))
                await route.continue_()

            await page_a.route("**/api/v1/profiles/p2-stale-dom", _spy_stale_patch)

            await page_a.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            edit_a = page_a.locator(
                'button[aria-label="Edit: p2-stale-dom"], button[aria-label="Sửa: p2-stale-dom"]'
            ).first
            await edit_a.wait_for(state="visible")
            await edit_a.click()
            await page_a.wait_for_selector(".modal #f-cores")
            editor_a_badge = await page_a.locator(".modal .note.calm b").first.inner_text()
            assert "#SEED-00000003" in editor_a_badge, editor_a_badge

            await page_b.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            edit_b = page_b.locator(
                'button[aria-label="Edit: p2-stale-dom"], button[aria-label="Sửa: p2-stale-dom"]'
            ).first
            await edit_b.wait_for(state="visible")
            await edit_b.click()
            await page_b.wait_for_selector(".modal #f-roll-fp")
            await page_b.click("#f-roll-fp")
            await page_b.locator(".modal-foot .btn-act").click()
            await page_b.wait_for_selector(".modal", state="detached")
            rolled_b = fingerprint.load_saved_chrome_persona(str(stale_dir))
            assert rolled_b is not None and rolled_b["seed"] != 3

            await page_a.select_option("#f-cores", "12")
            await page_a.locator(".modal-foot .btn-act").click()
            await page_a.wait_for_selector(".modal", state="detached")
            after_stale_a = fingerprint.load_saved_chrome_persona(str(stale_dir))
            assert after_stale_a is not None
            assert after_stale_a["seed"] == rolled_b["seed"]
            assert after_stale_a.get("device_model") == rolled_b.get("device_model")
            assert after_stale_a.get("screen_width") == rolled_b.get("screen_width")
            assert after_stale_a["brand"] == rolled_b["brand"]
            assert after_stale_a["hardware_concurrency"] == 12
            assert "seed" not in stale_patches[0]["initial_fingerprint"]

            await ctx2.close()
            await browser.close()

        # 6. Legacy seed=39 (16 -> Auto) under temporary pinned-026 fixture preserves full legacy identity.
        fix_dir = tmp / "cand026_fixture"
        fix_app = fix_dir / "Chromium.app"
        fix_exe = fix_app / "Contents/MacOS/Chromium"
        fix_exe.parent.mkdir(parents=True)
        fix_exe.write_bytes(b"cand026-recheck-fixture")
        fix_bin_sha = _hl.sha256(b"cand026-recheck-fixture").hexdigest()
        fix_b_sha = _bsha(fix_app)
        (fix_dir / "native-acceptance.json").write_text(
            json.dumps(
                {
                    "binary_sha256": fix_bin_sha,
                    "bundle_sha256": fix_b_sha,
                    "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
                    "cases": {
                        "macos:2": {},
                        "macos:3": {},
                        "apple_silicon_m3_laptop": {},
                        "apple_silicon_m2_max_desktop": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        orig_realms = engine_mod.NATIVE_REALM_BUNDLES
        orig_026 = engine_mod.APPLE_SILICON_COHERENCE_BUNDLES
        orig_installed = _fetch.installed_executable
        orig_env_exe = os.environ.pop("FOXPROFILE_CHROME_EXECUTABLE", None)
        try:
            engine_mod.NATIVE_REALM_BUNDLES = orig_realms | {fix_b_sha}
            engine_mod.APPLE_SILICON_COHERENCE_BUNDLES = orig_026 | {fix_b_sha}
            _fetch.installed_executable = lambda *_: fix_exe
            assert (
                client.post(
                    "/api/v1/profiles",
                    json={"name": "legacy-seed39-026", "os_type": "macos", "engine": "chrome"},
                ).status_code
                == 201
            )
            leg39_dir = tmp / "camoufox_data/legacy-seed39-026"
            leg39_orig = chrome_persona.Persona(
                seed=39,
                platform="macos",
                platform_version="14.6.1",
                brand="Chrome",
                hardware_concurrency=16,
            )
            chrome_persona.save(leg39_dir, leg39_orig)
            assert (
                client.patch(
                    "/api/v1/profiles/legacy-seed39-026",
                    json={
                        "initial_fingerprint": {"platform": "macos", "hardware_concurrency": None}
                    },
                ).status_code
                == 200
            )
            leg39_after = chrome_persona.load(leg39_dir)
            expected_leg39_cpu = chrome_persona.default_cpu_for_persona(
                "macos", 39, coherence_026=False
            )
            assert leg39_after is not None
            assert leg39_after.hardware_concurrency == expected_leg39_cpu == 11
            assert {
                k: v for k, v in leg39_after.to_json().items() if k != "hardware_concurrency"
            } == {k: v for k, v in leg39_orig.to_json().items() if k != "hardware_concurrency"}
            async with ChromeEngine(
                leg39_dir, platform="macos", executable=RUN19_EXE, headless=True
            ) as ctx:
                page = await ctx.new_page()
                try:
                    await page.goto(f"http://127.0.0.1:{port}/api/v1/health", wait_until="load")
                    leg39_live_eval = await page.evaluate(
                        "() => ({ hc: navigator.hardwareConcurrency, platform: navigator.platform })"
                    )
                finally:
                    await page.close()
            assert leg39_live_eval["hc"] == 11
        finally:
            engine_mod.NATIVE_REALM_BUNDLES = orig_realms
            engine_mod.APPLE_SILICON_COHERENCE_BUNDLES = orig_026
            _fetch.installed_executable = orig_installed
            if orig_env_exe is not None:
                os.environ["FOXPROFILE_CHROME_EXECUTABLE"] = orig_env_exe

        # 7. Save-contract checks (Flet stale CPU edit, platformless API PATCH, bound Run #19 atomic refusal).
        class DummyPage:
            dialog = None

            def update(self):
                pass

            def show_dialog(self, dlg):
                self.dialog = dlg

            def pop_dialog(self):
                pass

        def walk(ctrl):
            yield ctrl
            for attr in ("content", "controls", "actions", "title"):
                children = getattr(ctrl, attr, None)
                for child in children if isinstance(children, list) else [children]:
                    if isinstance(child, ft.Control):
                        yield from walk(child)

        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": "sc-flet-race", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        sc_race_dir = tmp / "camoufox_data/sc-flet-race"
        chrome_persona.save(
            sc_race_dir,
            chrome_persona.Persona(
                seed=39,
                platform="macos",
                platform_version="14.6.1",
                brand="Edge",
                hardware_concurrency=16,
            ),
        )
        pm_sc = app.state.container.profile_manager
        sc_page = DummyPage()
        sc_captured: dict = {}

        def _sc_on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
            sc_captured.update(kwargs)
            pm_sc.update_profile(
                "sc-flet-race",
                name,
                proxy,
                os_type,
                timezone,
                locale,
                initial_fingerprint=kwargs["initial_fingerprint"],
            )
            return None

        open_profile_dialog(
            sc_page, None, _sc_on_save, pm_sc.profiles["sc-flet-race"], profile_manager=pm_sc
        )
        assert (
            client.patch(
                "/api/v1/profiles/sc-flet-race",
                json={"initial_fingerprint": {"seed": 45, "platform": "macos"}},
            ).status_code
            == 200
        )
        sc_ctrls = list(walk(sc_page.dialog))
        sc_cores_dd = next(
            c
            for c in sc_ctrls
            if isinstance(c, ft.Dropdown)
            and any(o.key == "12" for o in (c.options or []))
            and any(o.key == "16" for o in (c.options or []))
        )
        sc_cores_dd.value = "12"
        sc_cores_dd.on_select(None)
        sc_page.dialog.actions[-1].on_click(None)
        sc_after_flet = chrome_persona.load(sc_race_dir)
        assert sc_after_flet is not None
        assert sc_after_flet.seed == 45 and sc_after_flet.hardware_concurrency == 12
        assert sc_captured["initial_fingerprint"] == {
            "platform": "macos",
            "hardware_concurrency": 12,
        }

        # Platformless CPU-only API PATCH on uninitialized macOS profile -> saves platform=macos.
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": "sc-fresh-mac", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        assert (
            client.patch(
                "/api/v1/profiles/sc-fresh-mac",
                json={"initial_fingerprint": {"hardware_concurrency": 12}},
            ).status_code
            == 200
        )
        sc_fresh = chrome_persona.load(tmp / "camoufox_data/sc-fresh-mac")
        assert sc_fresh is not None
        assert sc_fresh.platform == "macos" and sc_fresh.hardware_concurrency == 12

        # Blocked explicit 026 migration against bound Run #19 -> HTTP 400 and byte-identical.
        orig_bound = cores.bound_executable
        cores.bound_executable = lambda *_: RUN19_EXE
        try:
            assert (
                client.post(
                    "/api/v1/profiles",
                    json={"name": "sc-bound-run19", "os_type": "macos", "engine": "chrome"},
                ).status_code
                == 201
            )
            pm_sc.profiles["sc-bound-run19"].core_version = "148.0.7778.215"
            pm_sc.save_profiles()
            sc_bdir = tmp / "camoufox_data/sc-bound-run19"
            chrome_persona.save(
                sc_bdir,
                chrome_persona.Persona(
                    seed=39,
                    platform="macos",
                    platform_version="14.6.1",
                    brand="Edge",
                    hardware_concurrency=16,
                ),
            )
            sc_snap = chrome_persona.persona_path(sc_bdir).read_bytes()
            sc_mig = chrome_persona.generate("macos", seed=39, coherence_026=True).to_json()
            sc_ref = client.patch(
                "/api/v1/profiles/sc-bound-run19",
                json={"initial_fingerprint": sc_mig},
            )
            assert sc_ref.status_code == 400
            assert chrome_persona.persona_path(sc_bdir).read_bytes() == sc_snap
        finally:
            cores.bound_executable = orig_bound

        # 8. Persistence rollback, unbound explicit 026 rejection, & malformed field validation.
        from src.services.profile import manager as pm_mod

        err_client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        tmp_reg_path = tmp / "profiles.json"
        orig_write_persona = fingerprint._write_chrome_persona
        orig_replace = pm_mod._replace
        p_026_unbound = chrome_persona.generate("macos", seed=39, coherence_026=True).to_json()

        assert (
            err_client.post(
                "/api/v1/profiles",
                json={
                    "name": "pr-before",
                    "os_type": "macos",
                    "engine": "chrome",
                    "notes": "pr-orig",
                    "initial_fingerprint": {
                        "seed": 39,
                        "platform": "macos",
                        "hardware_concurrency": 16,
                    },
                },
            ).status_code
            == 201
        )
        pr_dir = tmp / "camoufox_data/pr-before"
        pr_reg_snap = tmp_reg_path.read_bytes()
        pr_persona_snap = chrome_persona.persona_path(pr_dir).read_bytes()

        def _fail_io(*_a, **_kw):
            raise OSError("injected-io-error")

        fingerprint._write_chrome_persona = _fail_io
        try:
            r_pr_post = err_client.post(
                "/api/v1/profiles",
                json={
                    "name": "pr-post-fail",
                    "os_type": "macos",
                    "engine": "chrome",
                    "initial_fingerprint": {"seed": 39, "platform": "macos"},
                },
            )
            r_pr_patch = err_client.patch(
                "/api/v1/profiles/pr-before",
                json={
                    "name": "pr-after",
                    "notes": "pr-mutated",
                    "initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12},
                },
            )
        finally:
            fingerprint._write_chrome_persona = orig_write_persona
        assert r_pr_post.status_code == 500
        assert r_pr_patch.status_code == 500
        assert tmp_reg_path.read_bytes() == pr_reg_snap
        assert chrome_persona.persona_path(pr_dir).read_bytes() == pr_persona_snap
        assert pr_dir.exists() and not (tmp / "camoufox_data/pr-after").exists()

        pm_mod._replace = _fail_io
        try:
            r_pr_replace = err_client.post(
                "/api/v1/profiles",
                json={
                    "name": "pr-replace-fail",
                    "os_type": "macos",
                    "engine": "chrome",
                    "initial_fingerprint": {"seed": 39, "platform": "macos"},
                },
            )
        finally:
            pm_mod._replace = orig_replace
        assert r_pr_replace.status_code == 500
        assert err_client.get("/api/v1/profiles/pr-replace-fail").status_code == 404
        assert tmp_reg_path.read_bytes() == pr_reg_snap
        assert not (tmp / "camoufox_data/pr-replace-fail").exists()

        r_unbound_run19 = err_client.post(
            "/api/v1/profiles",
            json={
                "name": "pr-unbound-026",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": p_026_unbound,
            },
        )
        assert r_unbound_run19.status_code == 400
        assert tmp_reg_path.read_bytes() == pr_reg_snap

        malformed_statuses = []
        for bad_fp in (
            {"locale": 7, "hardware_concurrency": 12},
            {"platform_version": 9, "hardware_concurrency": 12},
            {"locale": "bad@x"},
            {"screen_width": "bad", "hardware_concurrency": 12},
        ):
            m_res = err_client.patch(
                "/api/v1/profiles/pr-before",
                json={"initial_fingerprint": bad_fp},
            )
            assert m_res.status_code == 400, (bad_fp, m_res.status_code, m_res.text)
            assert chrome_persona.persona_path(pr_dir).read_bytes() == pr_persona_snap
            malformed_statuses.append(m_res.status_code)

        # 9. File & edit boundary checks: publish Path.replace temp cleanup, partial 1600x1000 migration,
        #    and uninitialized Flet CPU-only race preserving B's seed/brand.
        orig_path_replace = pathlib.Path.replace
        publish_bytes: list[int] = []

        def _fail_publish_replace(self, target):
            if self.suffix == ".tmp" and pathlib.Path(target).name == chrome_persona.PERSONA_FILE:
                publish_bytes.append(len(self.read_bytes()))
                raise OSError("injected-publish-replace")
            return orig_path_replace(self, target)

        pathlib.Path.replace = _fail_publish_replace
        try:
            r_pub_post = err_client.post(
                "/api/v1/profiles",
                json={
                    "name": "fb-post-pub",
                    "os_type": "macos",
                    "engine": "chrome",
                    "initial_fingerprint": {
                        "seed": 39,
                        "platform": "macos",
                        "hardware_concurrency": 12,
                    },
                },
            )
            r_pub_patch = err_client.patch(
                "/api/v1/profiles/pr-before",
                json={
                    "name": "fb-after-pub",
                    "initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12},
                },
            )
        finally:
            pathlib.Path.replace = orig_path_replace
        assert r_pub_post.status_code == 500
        assert r_pub_patch.status_code == 500
        assert len(publish_bytes) == 2 and all(n > 0 for n in publish_bytes)
        assert not (tmp / "camoufox_data/fb-post-pub").exists()
        assert not (tmp / "camoufox_data/fb-after-pub").exists()
        assert sorted(p.name for p in pr_dir.iterdir()) == ["chrome_persona.json"]
        assert chrome_persona.persona_path(pr_dir).read_bytes() == pr_persona_snap
        assert tmp_reg_path.read_bytes() == pr_reg_snap

        # Partial 1600x1000 migration on temp verified 026 candidate fixture.
        assert (
            err_client.post(
                "/api/v1/profiles",
                json={"name": "fb-partial-mig", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        fb_mig_dir = tmp / "camoufox_data/fb-partial-mig"
        chrome_persona.save(
            fb_mig_dir,
            chrome_persona.Persona(
                seed=39,
                platform="macos",
                platform_version="14.6.1",
                brand="Edge",
                hardware_concurrency=16,
            ),
        )
        fb_orig_env_exe = os.environ.pop("FOXPROFILE_CHROME_EXECUTABLE", None)
        try:
            engine_mod.NATIVE_REALM_BUNDLES = orig_realms | {fix_b_sha}
            engine_mod.APPLE_SILICON_COHERENCE_BUNDLES = orig_026 | {fix_b_sha}
            _fetch.installed_executable = lambda *_: fix_exe
            r_fb_mig = err_client.patch(
                "/api/v1/profiles/fb-partial-mig",
                json={
                    "initial_fingerprint": {
                        "screen_width": 1600,
                        "screen_height": 1000,
                        "hardware_concurrency": 12,
                    }
                },
            )
            assert r_fb_mig.status_code == 200, r_fb_mig.text
            fb_mig_after = chrome_persona.load(fb_mig_dir)
            assert fb_mig_after is not None
            assert (fb_mig_after.screen_width, fb_mig_after.screen_height) == (1600, 1000)
            assert fb_mig_after.hardware_concurrency == 12
            fb_mig_snap = chrome_persona.persona_path(fb_mig_dir).read_bytes()
            r_fb_bad_bounds = err_client.patch(
                "/api/v1/profiles/fb-partial-mig",
                json={
                    "initial_fingerprint": {
                        "screen_width": 1600,
                        "screen_height": 1000,
                        "avail_top": 50,
                        "avail_height": 980,
                    }
                },
            )
            r_fb_bad_lone = err_client.patch(
                "/api/v1/profiles/fb-partial-mig",
                json={"initial_fingerprint": {"screen_width": 1600}},
            )
            assert r_fb_bad_bounds.status_code == 400 and r_fb_bad_lone.status_code == 400
            assert chrome_persona.persona_path(fb_mig_dir).read_bytes() == fb_mig_snap
        finally:
            engine_mod.NATIVE_REALM_BUNDLES = orig_realms
            engine_mod.APPLE_SILICON_COHERENCE_BUNDLES = orig_026
            _fetch.installed_executable = orig_installed
            if fb_orig_env_exe is not None:
                os.environ["FOXPROFILE_CHROME_EXECUTABLE"] = fb_orig_env_exe

        # Uninitialized Flet CPU-only edit after Editor B creates seed 45 / Edge / 16.
        assert (
            err_client.post(
                "/api/v1/profiles",
                json={"name": "fb-flet-uninit", "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        fb_uninit_dir = tmp / "camoufox_data/fb-flet-uninit"
        fb_uninit_captured: dict = {}
        fb_uninit_page = DummyPage()
        orig_randbelow = chrome_persona.secrets.randbelow
        chrome_persona.secrets.randbelow = lambda _: 38
        try:
            open_profile_dialog(
                fb_uninit_page,
                None,
                lambda name, proxy, os_type, tz, loc, eng, **kw: (
                    fb_uninit_captured.update(kw),
                    pm_sc.update_profile(
                        "fb-flet-uninit",
                        name,
                        proxy,
                        os_type,
                        tz,
                        loc,
                        initial_fingerprint=kw["initial_fingerprint"],
                    ),
                    None,
                )[2],
                pm_sc.profiles["fb-flet-uninit"],
                profile_manager=pm_sc,
            )
        finally:
            chrome_persona.secrets.randbelow = orig_randbelow
        assert (
            err_client.patch(
                "/api/v1/profiles/fb-flet-uninit",
                json={
                    "initial_fingerprint": {
                        "seed": 45,
                        "platform": "macos",
                        "brand": "Edge",
                        "hardware_concurrency": 16,
                    }
                },
            ).status_code
            == 200
        )
        fb_cores_dd = next(
            c
            for c in walk(fb_uninit_page.dialog)
            if isinstance(c, ft.Dropdown)
            and any(o.key == "12" for o in (c.options or []))
            and any(o.key == "16" for o in (c.options or []))
        )
        fb_cores_dd.value = "12"
        fb_cores_dd.on_select(None)
        fb_uninit_page.dialog.actions[-1].on_click(None)
        assert fb_uninit_captured["initial_fingerprint"] == {
            "platform": "macos",
            "hardware_concurrency": 12,
        }
        fb_uninit_after = chrome_persona.load(fb_uninit_dir)
        assert fb_uninit_after is not None
        assert (
            fb_uninit_after.seed,
            fb_uninit_after.brand,
            fb_uninit_after.hardware_concurrency,
        ) == (
            45,
            "Edge",
            12,
        )
    finally:
        server.should_exit = True
        await server_task
        sock.close()

    return {
        "live_run19": {
            "executable": str(RUN19_EXE),
            "executable_sha256": _sha256_file(RUN19_EXE),
            "native_realm_bundles": sorted(engine_mod.NATIVE_REALM_BUNDLES),
            "apple_silicon_coherence_bundles": sorted(engine_mod.APPLE_SILICON_COHERENCE_BUNDLES),
            "web_post_mac": {
                "requires_apple_silicon_026": web_persona.requires_apple_silicon_026,
                "device_model": web_persona.device_model,
                "hardware_concurrency": web_persona.hardware_concurrency,
                "live_eval": live_web_eval,
            },
            "legacy_auto_reset": {
                "seed": legacy_persona.seed,
                "before_cores": 16,
                "after_cores": legacy_persona.hardware_concurrency,
                "expected_baseline_cores": expected_legacy_cores,
                "requires_apple_silicon_026": legacy_persona.requires_apple_silicon_026,
                "live_eval": live_legacy_eval,
            },
            "explicit_026_fail_closed": {
                "refused_message": refused_msg,
                "persona_bytes_unchanged": True,
            },
        },
        "fault_injected_web_dom_e2e": {
            "injected_fingerprint_status": 500,
            "injected_500_console_count": injected_500_console_count,
            "initial_badge_text": saved_badge_text,
            "cpu_edit_patch_initial_fingerprint": captured_patches[0]["initial_fingerprint"],
            "persisted_after_cpu_edit": {
                "seed": saved_after_cpu["seed"],
                "device_model": saved_after_cpu["device_model"],
                "screen_width": saved_after_cpu["screen_width"],
                "brand": saved_after_cpu["brand"],
                "hardware_concurrency": saved_after_cpu["hardware_concurrency"],
            },
            "explicit_roll_badge_text": rolled_badge_text,
            "explicit_roll_patch_has_seed": isinstance(
                captured_patches[1]["initial_fingerprint"].get("seed"), int
            ),
            "console_errors": len(console_errors),
            "page_errors": len(page_errors),
        },
        "two_editor_stale_seed_dom_e2e": {
            "editor_a_badge": editor_a_badge,
            "editor_b_rolled_seed": rolled_b["seed"],
            "editor_a_cpu_patch_initial_fingerprint": stale_patches[0]["initial_fingerprint"],
            "persisted_after_stale_a_cpu_edit": {
                "seed": after_stale_a["seed"],
                "device_model": after_stale_a.get("device_model"),
                "screen_width": after_stale_a.get("screen_width"),
                "brand": after_stale_a["brand"],
                "hardware_concurrency": after_stale_a["hardware_concurrency"],
            },
        },
        "legacy_seed39_auto_under_026_fixture": {
            "seed": leg39_after.seed,
            "before_cores": 16,
            "after_cores": leg39_after.hardware_concurrency,
            "expected_legacy_cores": expected_leg39_cpu,
            "platform_version": leg39_after.platform_version,
            "device_model": leg39_after.device_model,
            "requires_apple_silicon_026": leg39_after.requires_apple_silicon_026,
            "run19_rollback_live_eval": leg39_live_eval,
        },
        "save_contract_verification": {
            "flet_stale_cpu_payload": sc_captured["initial_fingerprint"],
            "flet_after_newer_roll_seed": sc_after_flet.seed,
            "flet_after_newer_roll_cpu": sc_after_flet.hardware_concurrency,
            "api_fresh_mac_no_platform_os": sc_fresh.platform,
            "api_fresh_mac_no_platform_cpu": sc_fresh.hardware_concurrency,
            "blocked_026_migration_status": sc_ref.status_code,
            "blocked_026_migration_bytes_unchanged": True,
        },
        "persistence_and_validation_verification": {
            "post_writer_error_status": r_pr_post.status_code,
            "patch_rename_writer_error_status": r_pr_patch.status_code,
            "post_replace_error_status": r_pr_replace.status_code,
            "unbound_explicit_026_on_run19_status": r_unbound_run19.status_code,
            "malformed_field_statuses": malformed_statuses,
            "registry_and_persona_unchanged_after_all_failures": True,
        },
        "file_boundary_verification": {
            "publish_replace_post_status": r_pub_post.status_code,
            "publish_replace_patch_status": r_pub_patch.status_code,
            "publish_replace_patch_dir_entries": sorted(p.name for p in pr_dir.iterdir()),
            "partial_026_migration_screen": [
                fb_mig_after.screen_width,
                fb_mig_after.screen_height,
            ],
            "partial_026_migration_cpu": fb_mig_after.hardware_concurrency,
            "incompatible_partial_026_statuses": [
                r_fb_bad_bounds.status_code,
                r_fb_bad_lone.status_code,
            ],
            "flet_uninit_cpu_payload": fb_uninit_captured["initial_fingerprint"],
            "flet_uninit_after_identity": [
                fb_uninit_after.seed,
                fb_uninit_after.brand,
                fb_uninit_after.hardware_concurrency,
            ],
        },
    }


def _run_child(tmpdir: str) -> int:
    import asyncio

    tmp = pathlib.Path(tmpdir)
    report = asyncio.run(_run_child_async(tmp))
    (tmp / "child_report.json").write_text(json.dumps(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        raise SystemExit(_run_child(sys.argv[2]))
    raise SystemExit(_run_parent(_resolve_out_path(sys.argv)))
