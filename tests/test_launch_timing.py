import asyncio
import re
from types import SimpleNamespace

import pytest

from src.services.browser import gpu_probe, runner, timing


def test_stage_reports_monotonic_elapsed_without_payload(monkeypatch, capsys):
    ticks = iter([12.0, 14.125])
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    with timing.launch_stage("exit_ip"):
        secret = "not-logged-proxy-password"
    output = capsys.readouterr().out
    assert output == "LAUNCH_TIMING exit_ip 2.125s ok\n"
    assert secret not in output


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_stage_reports_failure_without_suppressing_or_logging_error(monkeypatch, capsys, failure):
    ticks = iter([10.0, 11.25])
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    with pytest.raises(failure), timing.launch_stage("exit_ip"):
        raise failure("private-payload")
    assert capsys.readouterr().out == "LAUNCH_TIMING exit_ip 1.250s failed\n"


def test_runner_times_startup_only_and_preserves_ready_protocol(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_read_profile_extra", lambda _: {})
    monkeypatch.setattr(runner, "load_or_create", lambda *_: {})
    monkeypatch.setattr(runner, "exit_ip", lambda *_: "198.51.100.1")
    monkeypatch.setattr(runner, "pin_geoip_locale", lambda _: None)
    monkeypatch.setattr(runner, "ensure_google_search", lambda: None)
    monkeypatch.setattr(runner, "HEADLESS", True)
    monkeypatch.setattr(runner, "RESTORE_TABS", False)
    page = SimpleNamespace(on=lambda *_: None, is_closed=lambda: False)
    context = SimpleNamespace(pages=[page], on=lambda *_: None)
    closed = []

    class Browser:
        async def __aenter__(self):
            return context

        async def __aexit__(self, *_):
            closed.append(True)

    monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: Browser())

    async def cleanup():
        pass

    async def serve(_):
        runner._shutdown.set()
        return SimpleNamespace(cleanup=cleanup), 1234, "fixture-token"

    monkeypatch.setattr(runner.control, "serve", serve)
    monkeypatch.setattr(runner, "_shutdown", asyncio.Event())
    assert asyncio.run(runner.run_browser("fixture", "None", "windows", locale="en-US")) == 0
    lines = capsys.readouterr().out.splitlines()
    stages = [line.split()[1] for line in lines if line.startswith("LAUNCH_TIMING ")]
    assert stages == [
        "profile_metadata",
        "fingerprint",
        "exit_ip",
        "geo_locale",
        "search_policy",
        "virtual_display",
        "browser_start",
        "restore_tabs",
        "control_ready",
        "launch_ready_total",
    ]
    assert lines.index("CONTROL:1234:fixture-token") < lines.index("BROWSER_STARTED")
    assert all(
        re.fullmatch(r"LAUNCH_TIMING [a-z_]+ \d+\.\d{3}s ok", line)
        for line in lines
        if line.startswith("LAUNCH_TIMING ")
    )
    assert closed == [True]


def test_gpu_substages_do_not_log_binary_env_or_gpu_values(monkeypatch, tmp_path, capsys):
    import hashlib

    binary = tmp_path / "private-core-path"
    binary.write_bytes(b"fixture")
    monkeypatch.setattr(gpu_probe, "_TESTED_BINARY_SHA256", hashlib.sha256(b"fixture").hexdigest())
    monkeypatch.setattr(gpu_probe, "host_gpu", lambda *_: gpu_probe._TESTED_GPU)
    assert (
        gpu_probe._probe(str(binary), False, {"SECRET": "private-env"}, {}) == gpu_probe._TESTED_GPU
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"LAUNCH_TIMING gpu_binary_hash \d+\.\d{3}s ok", lines[0])
    assert re.fullmatch(r"LAUNCH_TIMING gpu_probe_headless \d+\.\d{3}s ok", lines[1])


@pytest.mark.parametrize("failed", [False, True])
def test_chrome_runner_timing_and_protocol(monkeypatch, tmp_path, capsys, failed):
    from chrome_engine import runner as chrome

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chrome, "_read_profile_extra", lambda _: {})
    monkeypatch.setattr(chrome, "RESTORE_TABS", False)
    monkeypatch.setattr(chrome, "_shutdown", asyncio.Event())
    context = SimpleNamespace(
        pages=[SimpleNamespace(on=lambda *_: None, is_closed=lambda: False)],
        on=lambda *_: None,
    )
    stopped = []

    class Engine:
        warnings = []

        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            if failed:
                raise RuntimeError("fixture start failed")
            return context

        async def stop(self):
            stopped.append(True)

    async def noop():
        pass

    async def serve(_):
        chrome._shutdown.set()
        return SimpleNamespace(cleanup=noop), 1234, "fixture-token"

    monkeypatch.setattr(chrome, "ChromeEngine", Engine)
    monkeypatch.setattr(chrome, "ensure_installed", noop)
    monkeypatch.setattr(chrome.control, "serve", serve)
    assert asyncio.run(chrome.run_browser("fixture", "None", "macos")) == int(failed)
    lines = capsys.readouterr().out.splitlines()
    markers = [line.split()[1] for line in lines if line.startswith("LAUNCH_TIMING ")]
    assert markers[:3] == ["profile_metadata", "install_check", "browser_start"]
    if failed:
        assert any(
            re.fullmatch(r"LAUNCH_TIMING browser_start \d+\.\d{3}s failed", line) for line in lines
        )
        assert "BROWSER_STARTED" not in lines
    else:
        assert markers[3:] == ["restore_tabs", "control_ready", "launch_ready_total"]
        assert lines.index("CONTROL:1234:fixture-token") < lines.index("BROWSER_STARTED")
    assert stopped == [True]


def test_chrome_engine_times_geo_and_actual_launch_separately(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    from playwright import async_api

    from chrome_engine import engine as chrome

    persona = SimpleNamespace(timezone=None, locale=None)
    persona.with_geo = lambda *_: persona
    context = SimpleNamespace(pages=[], on=lambda *_: None)
    geo = chrome.ExitGeo(
        ip="198.51.100.1", country="US", timezone="America/New_York", locale="en-US"
    )
    seen = []

    class Bridge:
        url = "http://127.0.0.1:1234"

        def __init__(self, *_):
            pass

        async def start(self):
            seen.append("bridge")
            return self

        async def stop(self):
            seen.append("stop_bridge")

    async def lookup(self):
        seen.append("geo")
        return geo

    async def launch(*args, **kwargs):
        seen.append("browser")
        return context

    driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    async def start_driver():
        return driver

    monkeypatch.setattr(async_api, "async_playwright", lambda: SimpleNamespace(start=start_driver))
    monkeypatch.setattr(chrome, "ProxyBridge", Bridge)
    monkeypatch.setattr(chrome, "remembered_locale", lambda *_: "en-US")
    monkeypatch.setattr(chrome, "build_args", lambda *a, **kw: [])
    monkeypatch.setattr(chrome, "set_webrtc_policy", lambda *_: None)
    from chrome_engine import search

    async def initialize_search(context):
        seen.append("search")

    monkeypatch.setattr(search, "initialize_search", initialize_search)
    engine = chrome.ChromeEngine(tmp_path, proxy="http://fixture:1234", stealth_js=False)
    monkeypatch.setattr(engine, "_resolve_executable", lambda: Path("fixture"))
    monkeypatch.setattr(engine, "_resolve_persona", lambda: persona)
    monkeypatch.setattr(engine, "_lookup_geo", lookup.__get__(engine))
    monkeypatch.setattr(engine, "_ignored_default_args", lambda: [])
    assert asyncio.run(engine.start()) is context
    assert seen == ["bridge", "geo", "browser", "search"]
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[1] for line in lines] == [
        "chrome_core_resolve",
        "chrome_persona",
        "chrome_proxy_bridge",
        "chrome_geo_locale",
        "chrome_driver",
        "chrome_preferences",
        "chrome_context_start",
        "chrome_search",
    ]
    assert all(re.fullmatch(r"LAUNCH_TIMING [a-z_]+ \d+\.\d{3}s ok", line) for line in lines)
