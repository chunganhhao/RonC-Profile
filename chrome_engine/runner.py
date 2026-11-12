"""FoxProfile runner for the Chrome engine.

Same command line and stdout protocol as src/services/browser/runner.py, so
BrowserLauncher can start a Chrome profile exactly like a Camoufox one:

    python -m chrome_engine.runner <name> <proxy> <os> [timezone] [locale]

prints CONTROL:<port>:<token>, then BROWSER_STARTED, and BROWSER_CLOSED when
the last tab closes (or LAUNCH_FAILED: ...). The page-control server, tab
restore and tab snapshots are FoxProfile's own, shared with Camoufox.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from time import perf_counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chrome_engine import fetch
from chrome_engine.engine import ChromeEngine
from src.core.config import DATA_DIR, HEADLESS, RESTORE_TABS
from src.services.browser import control, session
from src.services.browser.runner import _read_profile_extra, restore_tabs, snapshot_tabs
from src.services.browser.timing import launch_stage, report_timing
from src.utils.validation import normalize_start_url

_shutdown = asyncio.Event()


def _say(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def _compact(exc: BaseException, limit: int = 220) -> str:
    text = " ".join(str(exc).split())
    text = text.split("Call log:", 1)[0].strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _install_progress() -> fetch.Progress:
    """Report download progress every 10% (the launcher shows these lines in the log)."""
    last = -1

    def report(done: int, total: int) -> None:
        nonlocal last
        pct = done * 100 // total if total else 0
        if pct // 10 != last // 10:
            last = pct
            _say(f"Downloading Chrome engine: {pct}% ({done / 1e6:.0f}/{total / 1e6:.0f} MB)")

    return report


async def ensure_installed() -> None:
    """Download the pinned browser build on first use."""
    if fetch.installed_executable():
        return
    _say("Chrome engine is not installed yet; downloading it once...")
    await asyncio.to_thread(fetch.install, progress=_install_progress())
    _say("Chrome engine installed")


def _headless() -> bool:
    # "virtual" means a hidden Xvfb display for Camoufox; Chrome runs headless instead.
    return HEADLESS in (True, "virtual")


async def run_browser(
    profile_name: str,
    proxy_str: str,
    os_type: str,
    timezone: str = "",
    locale: str = "",
    core_version: str = "",
) -> int:
    launch_started = perf_counter()
    profile_dir = os.path.join(os.getcwd(), DATA_DIR, profile_name)
    proxy = proxy_str if proxy_str and proxy_str != "None" else None
    with launch_stage("profile_metadata"):
        extra = _read_profile_extra(profile_name)
    if extra.get("core_version"):
        core_version = extra["core_version"]
    engine_kwargs: dict[str, object] = {}
    if core_version:
        try:
            from src.services.browser.cores import bound_executable

            with launch_stage("core_resolve"):
                engine_kwargs["executable"] = bound_executable("chrome", core_version)
        except (ValueError, OSError, RuntimeError) as exc:
            _say(f"LAUNCH_FAILED: {_compact(exc, 220)}")
            return 1
    if extra.get("webrtc_mode") == "disable":
        engine_kwargs["block_webrtc"] = True
    try:
        from src.services.browser.extensions import ExtensionService

        raw_ext_ids = extra.get("extension_ids")
        ext_paths, pinned_ids, unpinned_ids = ExtensionService().resolve_for_profile(
            [str(x) for x in raw_ext_ids if x] if isinstance(raw_ext_ids, list) else None
        )
        if ext_paths:
            engine_kwargs["extensions"] = ext_paths
        if pinned_ids:
            engine_kwargs["pinned_extension_ids"] = pinned_ids
        if unpinned_ids:
            engine_kwargs["unpinned_extension_ids"] = unpinned_ids
    except Exception:
        pass
    engine = ChromeEngine(
        profile_dir,
        platform=os_type,
        proxy=proxy,
        headless=_headless(),
        # "" keeps the timezone / locale following the proxy's exit IP.
        timezone=timezone,
        locale=locale,
        **engine_kwargs,
    )
    _say(f"Starting Chrome engine for {profile_name}...")
    try:
        if not core_version:
            with launch_stage("install_check"):
                await ensure_installed()
        with launch_stage("browser_start"):
            context = await engine.start()
    except asyncio.CancelledError:
        _say("LAUNCH_CANCELLED")
        await engine.stop()
        return 1
    except Exception as e:
        _say(f"LAUNCH_FAILED: {type(e).__name__}: {_compact(e)}")
        await engine.stop()
        return 1

    try:
        for warning in engine.warnings:
            _say(f"Warning: {warning}")
        with launch_stage("restore_tabs"):
            if not context.pages:
                await context.new_page()
            restored = 0
            if RESTORE_TABS:
                restored = await restore_tabs(context, session.load(profile_dir))
                if restored:
                    _say(f"Restored {restored} tab(s)")
            start_url = normalize_start_url(extra.get("start_url"))
            if not restored and start_url:
                await restore_tabs(context, session.restorable([start_url]))

        close_event = asyncio.Event()

        def on_page_close(_page: object) -> None:
            if not [p for p in context.pages if not p.is_closed()]:
                close_event.set()

        def watch(page: object) -> None:
            page.on("close", on_page_close)

        for page in context.pages:
            watch(page)
        context.on("page", watch)
        context.on("close", lambda _: close_event.set())

        with launch_stage("control_ready"):
            control_runner, port, token = await control.serve(context)
        # CONTROL must precede BROWSER_STARTED (see the Camoufox runner).
        _say(f"CONTROL:{port}:{token}")
        report_timing("launch_ready_total", launch_started)
        _say("BROWSER_STARTED")

        snapshot_task = (
            asyncio.create_task(snapshot_tabs(context, profile_dir, close_event))
            if RESTORE_TABS
            else None
        )
        close_task = asyncio.create_task(close_event.wait())
        shutdown_task = asyncio.create_task(_shutdown.wait())
        done, pending = await asyncio.wait(
            [close_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
        )
        if close_task in done:
            _say("BROWSER_CLOSED")
        elif RESTORE_TABS:
            session.save(profile_dir, [p.url for p in context.pages if not p.is_closed()])
        if snapshot_task:
            snapshot_task.cancel()
        await control_runner.cleanup()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return 0
    finally:
        await engine.stop()


async def _async_main(argv: list[str]) -> int:
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        pass
    from src.services.browser.cores import launch_binding
    from src.services.browser.lease import profile_lease

    with profile_lease(os.path.join(os.getcwd(), DATA_DIR, argv[0])):
        args = argv[:5]
        args += [""] * (5 - len(args))
        try:
            with launch_stage("launch_binding"):
                version = launch_binding(args[0], "chrome", argv[5] if len(argv) > 5 else "")
        except (ValueError, OSError, RuntimeError) as exc:
            _say(f"LAUNCH_FAILED: {_compact(exc, 220)}")
            return 1
        return await run_browser(*args, version)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 4:
        _say("Usage: python -m chrome_engine.runner <name> <proxy> <os> [timezone] [locale]")
        return 1
    try:
        return asyncio.run(_async_main(sys.argv[1:]))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
