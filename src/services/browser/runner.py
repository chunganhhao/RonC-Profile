import asyncio
import contextlib
import json
import os
import pathlib
import signal
import sys
from time import perf_counter

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from camoufox.async_api import AsyncCamoufox
from camoufox.virtdisplay import VirtualDisplay

from src.core.config import DATA_DIR, HEADLESS, PROFILES_FILE, RESTORE_TABS
from src.services.browser import control, session
from src.services.browser import display as xvfb
from src.services.browser.fingerprint import available_gpus, load_or_create
from src.services.browser.search import ensure_google_search
from src.services.browser.timing import launch_stage, report_timing
from src.services.proxy.geo_check import requests_proxy_url
from src.utils.proxy_parser import parse_proxy, require_proxy
from src.utils.validation import normalize_start_url

_shutdown = asyncio.Event()

# How often the open tabs are snapshotted. The browser is stopped by killing
# this process, so whatever was saved last is what the next launch reopens.
TAB_SNAPSHOT_SECONDS = 2.0


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _safe_print(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        fallback = (message + "\n").encode("utf-8", errors="replace")
        sys.stdout.buffer.write(fallback)
        sys.stdout.flush()


def _compact_error(exc: Exception, limit: int = 700) -> str:
    text = " ".join(str(exc).split())
    if "Call log:" in text:
        text = text.split("Call log:", 1)[0].strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _setup_signals(loop: asyncio.AbstractEventLoop) -> None:
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        pass


def _open_urls(context: object) -> list[str]:
    return [p.url for p in context.pages if not p.is_closed()]


async def restore_tabs(context: object, urls: list[str]) -> int:
    """Reopen saved tabs: the first in the existing blank tab, the rest in new ones.

    Waits only for navigation to commit, so a slow site does not hold up the
    launch; a tab that fails to load stays open on whatever it reached.
    """
    if not urls:
        return 0
    first = context.pages[0] if context.pages else await context.new_page()
    pages = [first] + [await context.new_page() for _ in urls[1:]]

    async def go(page: object, url: str) -> None:
        try:
            await page.goto(url, wait_until="commit", timeout=15_000)
        except Exception as e:
            _safe_print(f"Could not restore tab {url}: {_compact_error(e, 120)}")

    await asyncio.gather(*(go(p, u) for p, u in zip(pages, urls, strict=True)))
    await pages[0].bring_to_front()
    return len(urls)


async def snapshot_tabs(context: object, profile_dir: str, stop: asyncio.Event) -> None:
    """Save the open tabs whenever they change, until `stop` is set."""
    last: list[str] = []
    while not stop.is_set():
        # asyncio.TimeoutError, not the builtin: they only became one in 3.11.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TAB_SNAPSHOT_SECONDS)
        if stop.is_set():
            # The last tab closing ends the session; keep the previous snapshot
            # instead of saving the now-empty window.
            return
        urls = session.restorable(_open_urls(context))
        if urls and urls != last and session.save(profile_dir, urls):
            last = urls


def geo_overrides(timezone: str, locale: str) -> dict:
    """Launch options that pin the timezone / locale instead of following the IP.

    Camoufox's geoip only fills in the timezone and locale when they are unset
    (setdefault for the config key, and `locale=` is applied after geoip), so the
    pinned values win while geolocation and the WebRTC IP still follow the proxy.
    """
    options: dict = {}
    if timezone:
        options["config"] = {"timezone": timezone}
    if locale:
        options["locale"] = locale
    return options


def _read_profile_extra(profile_name: str) -> dict:
    """Read optional hardware/startup settings from profiles.json if available."""
    try:
        path = pathlib.Path(PROFILES_FILE)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get(profile_name), dict):
                return data[profile_name]
    except Exception:
        pass
    return {}


def exit_ip(proxy_str: str) -> str:
    """The exit IP Camoufox's geoip should use, looked up with DNS through the proxy.

    With `geoip=True` Camoufox looks it up itself, and for a socks5:// or
    socks4:// proxy that resolves the lookup hosts with the machine's own
    resolver at every launch.
    """
    from camoufox.ip import public_ip

    try:
        return public_ip(requests_proxy_url(proxy_str))
    except Exception as e:
        # The raw error is a urllib3 retry chain (class names, object addresses,
        # the lookup host); log it, but report something a user can act on.
        if not proxy_str or proxy_str == "None":
            raise
        _safe_print(f"Exit IP lookup failed: {_compact_error(e, 300)}")
        parsed = parse_proxy(proxy_str)
        server = parsed["server"].split("://", 1)[-1] if parsed else "the proxy"
        raise ProxyUnreachable(f"no internet connection through proxy {server}") from e


class ProxyUnreachable(RuntimeError):
    pass


def stable_locale(profile_dir: str, ip: str) -> str:
    """The locale for `ip`, kept per profile while the IP stays in one country.

    Left to geoip, Camoufox would pick a new locale for the country on every launch.
    """
    from camoufox.geolocation import get_geolocation

    from chrome_engine.geo import remembered_locale

    locale = get_geolocation(ip).locale
    return remembered_locale(profile_dir, locale.region, locale.as_string)


def pin_geoip_locale(locale: str) -> None:
    """Make Camoufox's geoip draw `locale` instead of a random one for the IP's country.

    Passing `locale=` is not enough: geoip still draws at random and fills the
    locale keys with setdefault, and `locale=` only overwrites the script when
    its own locale has one. Keeping zh-HK after a draw of en-Latn-HK would
    present zh-Latn-HK. This runner is one process per profile, so the patch
    only affects this launch.
    """
    from camoufox.locales import SELECTOR, normalize_locale

    pinned = normalize_locale(locale)
    SELECTOR.from_region = lambda region: pinned


async def run_browser(
    profile_name: str,
    proxy_str: str,
    os_type: str,
    timezone: str = "",
    locale: str = "",
    core_version: str = "",
) -> int:
    launch_started = perf_counter()
    try:
        proxy_config = require_proxy(proxy_str)
    except ValueError as exc:
        _safe_print(f"LAUNCH_FAILED: {exc}")
        return 1
    with launch_stage("profile_metadata"):
        extra = _read_profile_extra(profile_name)
    # A long-lived manager may hold an older Profile object after another
    # process applied a core. Persisted binding is authoritative at launch.
    if extra.get("core_version"):
        core_version = extra["core_version"]
    executable = None
    if core_version:
        try:
            from src.services.browser.cores import bound_executable

            with launch_stage("core_resolve"):
                executable = bound_executable("camoufox", core_version)
        except (ValueError, OSError, RuntimeError) as exc:
            _safe_print(f"LAUNCH_FAILED: {_compact_error(exc, 220)}")
            return 1
    profile_dir = os.path.join(os.getcwd(), DATA_DIR, profile_name)
    with launch_stage("fingerprint"):
        fp = load_or_create(profile_dir, os_type)

    launch_config = {
        "headless": HEADLESS,
        "os": os_type,
        "fingerprint": fp,
        "humanize": True,
        "block_images": False,
        "user_data_dir": profile_dir,
        "persistent_context": True,
        **geo_overrides(timezone, locale),
    }

    canvas_mode = extra.get("canvas_mode") or "auto"

    # Apply explicitly pinned hardware concurrency & GPU from the saved fingerprint if customized
    if isinstance(fp, dict) and fp:
        nav = fp.get("navigator") or {}
        scr = fp.get("screen") or {}
        if fp.get("_custom_cores") and isinstance(nav.get("hardwareConcurrency"), int):
            cfg_override = launch_config.setdefault("config", {})
            cfg_override["navigator.hardwareConcurrency"] = int(nav["hardwareConcurrency"])
            if os_type == "linux":
                try:
                    from camoufox.fingerprints import fix_navigator_arch

                    tmp_nav = {
                        "navigator.userAgent": nav.get("userAgent") or "",
                        "navigator.platform": nav.get("platform") or "",
                        "navigator.oscpu": nav.get("oscpu") or "",
                    }
                    fix_navigator_arch(tmp_nav, "lin")
                    if tmp_nav.get("navigator.platform"):
                        cfg_override["navigator.platform"] = tmp_nav["navigator.platform"]
                    if tmp_nav.get("navigator.oscpu"):
                        cfg_override["navigator.oscpu"] = tmp_nav["navigator.oscpu"]
                except Exception:
                    pass
            launch_config["i_know_what_im_doing"] = True

        webgl = fp.get("webgl") or {}
        vendor, renderer = webgl.get("vendor"), webgl.get("renderer")
        if (
            (fp.get("_custom_gpu") or canvas_mode != "auto")
            and vendor
            and renderer
            and (vendor, renderer) in available_gpus(os_type)
        ):
            try:
                from camoufox import coherence

                target_os = {"windows": "win", "macos": "mac", "linux": "lin"}.get(os_type, "win")
                fits = coherence.gpu_fits_machine(
                    renderer,
                    target_os,
                    nav.get("hardwareConcurrency"),
                    scr.get("width"),
                    scr.get("height"),
                )
            except Exception:
                fits = True
            if fits:
                launch_config["webgl_config"] = (vendor, renderer)
                launch_config["i_know_what_im_doing"] = True

    if extra.get("webrtc_mode") == "disable":
        launch_config["block_webrtc"] = True
    if canvas_mode == "noise":
        launch_config["canvas_noise"] = True
        launch_config["i_know_what_im_doing"] = True
    elif canvas_mode == "native":
        launch_config["canvas_noise"] = False
        launch_config["i_know_what_im_doing"] = True

    if proxy_config:
        launch_config["proxy"] = proxy_config

    _safe_print(f"Starting browser for {profile_name}...")

    # Camoufox's own headless="virtual" starts Xvfb before it builds the launch
    # options and stops it only when the browser closes, so every failed launch
    # left an Xvfb behind. Own the display here and stop it however we exit; the
    # record lets the launcher stop it if this process is killed instead.
    display = None
    try:
        if executable:
            launch_config["executable_path"] = str(executable)
        # Blocking (up to ~30 s over a slow proxy), so off the event loop; inside
        # the try so a dead proxy reports LAUNCH_FAILED like any launch error.
        with launch_stage("exit_ip"):
            ip = await asyncio.to_thread(exit_ip, proxy_str)
        launch_config["geoip"] = ip
        with launch_stage("geo_locale"):
            if not locale:
                launch_config["locale"] = await asyncio.to_thread(stable_locale, profile_dir, ip)
            pin_geoip_locale(launch_config["locale"])
        if not core_version:
            with launch_stage("search_policy"):
                await asyncio.to_thread(ensure_google_search)
        with launch_stage("virtual_display"):
            if HEADLESS == "virtual":
                await asyncio.to_thread(xvfb.sweep_stale)
                display = VirtualDisplay()
                launch_config["headless"] = False
                launch_config["virtual_display"] = display.get()
                xvfb.record(display.proc.pid, launch_config["virtual_display"])
        async with contextlib.AsyncExitStack() as stack:
            # Time entry only: the browser context stays open for the session.
            with launch_stage("browser_start"):
                context = await stack.enter_async_context(AsyncCamoufox(**launch_config))
            with launch_stage("restore_tabs"):
                if not context.pages:
                    await context.new_page()
                restored = 0
                if RESTORE_TABS:
                    restored = await restore_tabs(context, session.load(profile_dir))
                    if restored:
                        _safe_print(f"Restored {restored} tab(s)")
                start_url = normalize_start_url(extra.get("start_url"))
                if not restored and start_url:
                    await restore_tabs(context, session.restorable([start_url]))

            close_event = asyncio.Event()

            def on_page_close(_page: object) -> None:
                # The session ends when the last tab closes, not the first one.
                if not [p for p in context.pages if not p.is_closed()]:
                    close_event.set()

            def watch(page: object) -> None:
                page.on("close", on_page_close)

            for page in context.pages:
                watch(page)
            context.on("page", watch)
            context.on("close", lambda: close_event.set())

            with launch_stage("control_ready"):
                control_runner, port, token = await control.serve(context)
            # CONTROL must precede BROWSER_STARTED: the launcher reports the
            # profile ready on BROWSER_STARTED and callers may control it at once.
            _safe_print(f"CONTROL:{port}:{token}")
            report_timing("launch_ready_total", launch_started)
            _safe_print("BROWSER_STARTED")

            snapshot_task = (
                asyncio.create_task(snapshot_tabs(context, profile_dir, close_event))
                if RESTORE_TABS
                else None
            )
            close_task = asyncio.create_task(close_event.wait())
            shutdown_task = asyncio.create_task(_shutdown.wait())

            done, pending = await asyncio.wait(
                [close_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if close_task in done:
                _safe_print("BROWSER_CLOSED")
            elif RESTORE_TABS:
                # Graceful shutdown (signal): the tabs are still open, save them.
                session.save(profile_dir, _open_urls(context))
            if snapshot_task:
                snapshot_task.cancel()
            await control_runner.cleanup()

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            return 0

    except asyncio.CancelledError:
        _safe_print("LAUNCH_CANCELLED")
        return 1
    except Exception as e:
        _safe_print(f"LAUNCH_FAILED: {type(e).__name__}: {_compact_error(e, 220)}")
        return 1
    finally:
        if display:
            display.kill()
            xvfb.clear()


async def _async_main() -> int:
    loop = asyncio.get_running_loop()
    _setup_signals(loop)
    from src.services.browser.cores import launch_binding
    from src.services.browser.gpu_probe import install_headless_gpu_probe
    from src.services.browser.lease import profile_lease

    with profile_lease(os.path.join(os.getcwd(), DATA_DIR, sys.argv[1])):
        args = sys.argv[1:6]
        args += [""] * (5 - len(args))
        try:
            with launch_stage("launch_binding"):
                version = launch_binding(
                    args[0], "camoufox", sys.argv[6] if len(sys.argv) > 6 else ""
                )
        except (ValueError, OSError, RuntimeError) as exc:
            _safe_print(f"LAUNCH_FAILED: {_compact_error(exc, 220)}")
            return 1
        with launch_stage("probe_adapter"):
            install_headless_gpu_probe()
        return await run_browser(*args, version)


if __name__ == "__main__":
    _configure_stdio()

    if len(sys.argv) < 4:
        _safe_print("Usage: python runner.py <name> <proxy> <os> [timezone] [locale]")
        sys.exit(1)

    exit_code = 1
    try:
        exit_code = asyncio.run(_async_main())
    except KeyboardInterrupt:
        _safe_print("Interrupted by user")
        exit_code = 130

    sys.exit(exit_code)
