"""The browser engine a profile runs on: stored, validated, and routed to its runner."""

import json
import sys

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.container import Container
from src.models.profile import Profile
from src.services.browser import fingerprint
from src.services.browser.process import runner_command
from src.services.profile.manager import ProfileManager


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    # profiles.json, camoufox_data/ and logs/ are relative to the working directory.
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def client(workdir, monkeypatch):
    from src.services.browser import chrome_prefetch

    # Creating a Chrome profile must not start a real ~150 MB download in tests.
    monkeypatch.setattr(chrome_prefetch, "start", lambda log=None: False)
    # The token-less API only answers loopback Host headers.
    return TestClient(create_app(Container()), base_url="http://127.0.0.1")


def test_engine_defaults_to_camoufox_for_old_profiles(workdir):
    (workdir / "profiles.json").write_text(
        json.dumps({"old": {"name": "old", "proxy": None, "os_type": "macos"}}), encoding="utf-8"
    )
    assert ProfileManager().profiles["old"].engine == "camoufox"


def test_unknown_engine_in_profiles_file_falls_back(workdir):
    (workdir / "profiles.json").write_text(
        json.dumps({"x": {"name": "x", "os_type": "linux", "engine": "netscape"}}),
        encoding="utf-8",
    )
    assert ProfileManager().profiles["x"].engine == "camoufox"


def test_engine_survives_save_and_reload(workdir):
    ProfileManager().add_profile("c", "", "macos", engine="chrome")
    assert ProfileManager().profiles["c"].engine == "chrome"


def test_runner_command_follows_the_engine():
    camoufox = runner_command(Profile(name="a", os_type="windows"))
    chrome = runner_command(Profile(name="b", proxy="1.2.3.4:80", os_type="macos", engine="chrome"))
    assert camoufox[0] == chrome[0] == sys.executable
    assert camoufox[1].endswith("runner.py")
    assert chrome[1:3] == ["-m", "chrome_engine.runner"]
    # Same positional protocol for both runners.
    assert chrome[3:] == ["b", "1.2.3.4:80", "macos", "", ""]


def test_api_creates_and_reports_the_engine(client):
    r = client.post("/api/v1/profiles", json={"name": "c1", "os_type": "macos", "engine": "chrome"})
    assert r.status_code == 201
    assert r.json()["engine"] == "chrome"
    assert client.get("/api/v1/profiles/c1").json()["engine"] == "chrome"
    assert client.post("/api/v1/profiles", json={"name": "f1"}).json()["engine"] == "camoufox"


def test_chrome_defaults_to_host_os(client):
    from chrome_engine.release import host_os

    assert Profile(name="model", engine="chrome").os_type == host_os()
    response = client.post("/api/v1/profiles", json={"name": "host", "engine": "chrome"})
    assert response.status_code == 201
    assert response.json()["os_type"] == host_os()
    assert client.get("/api/v1/info").json()["host_os"] == host_os()
    assert Profile(name="firefox").os_type == "windows"


def test_chrome_api_default_os(client):
    from chrome_engine.release import host_os

    assert (
        client.post("/api/v1/profiles", json={"name": "host", "engine": "chrome"}).json()["os_type"]
        == host_os()
    )
    assert client.post("/api/v1/profiles", json={"name": "firefox"}).json()["os_type"] == "windows"


@pytest.mark.parametrize("os_type", ["windows", "macos", "linux"])
def test_chrome_explicit_os_is_preserved(client, os_type):
    response = client.post(
        "/api/v1/profiles", json={"name": "explicit", "engine": "chrome", "os_type": os_type}
    )
    assert response.status_code == 201
    assert response.json()["os_type"] == os_type


@pytest.mark.parametrize("host", ["windows", "macos", "linux"])
def test_model_and_schema_default_to_server_host(monkeypatch, host):
    from src.api.schemas import profiles as schema
    from src.models import profile as model

    monkeypatch.setattr(schema, "host_os", lambda: host)
    monkeypatch.setattr(model, "host_os", lambda: host)
    assert schema.ProfileCreate(name="demo", engine="chrome").os_type == host
    assert model.Profile(name="demo", engine="chrome").os_type == host
    assert schema.ProfileCreate(name="demo").os_type == "windows"
    assert model.Profile(name="demo").os_type == "windows"
    assert schema.ProfileCreate(name="demo", engine="chrome", os_type="macos").os_type == "macos"


def test_mcp_omits_unspecified_os(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from src.mcp_server import server

    request = AsyncMock(return_value={})
    monkeypatch.setattr(server, "_arequest", request)
    asyncio.run(server.create_profile(name="demo", engine="chrome"))
    assert "os_type" not in request.call_args.args[2]
    asyncio.run(server.create_profile(name="demo", engine="chrome", os_type="macos"))
    assert request.call_args.args[2]["os_type"] == "macos"


def test_api_rejects_unknown_engine(client):
    r = client.post("/api/v1/profiles", json={"name": "x", "engine": "netscape"})
    assert r.status_code == 400


def test_api_refuses_to_change_the_engine(client):
    client.post("/api/v1/profiles", json={"name": "c2", "engine": "chrome"})
    r = client.patch("/api/v1/profiles/c2", json={"engine": "camoufox"})
    assert r.status_code == 400
    # Sending the current value along with other edits is fine.
    r = client.patch("/api/v1/profiles/c2", json={"engine": "chrome", "proxy": "1.2.3.4:8080"})
    assert r.status_code == 200
    assert r.json()["engine"] == "chrome"


def test_fingerprint_summary_reads_the_chrome_persona(tmp_path):
    (tmp_path / fingerprint.CHROME_PERSONA_FILE).write_text(
        json.dumps({"seed": 7, "platform": "macos", "hardware_concurrency": 12}), encoding="utf-8"
    )
    assert fingerprint.summary(str(tmp_path)) == {
        "os": "macos",
        "platform": "MacIntel",
        "screen": None,
        "hardware_concurrency": 12,
        "seed": 7,
    }
    assert fingerprint.reset(str(tmp_path))
    assert not (tmp_path / fingerprint.CHROME_PERSONA_FILE).exists()
    assert fingerprint.summary(str(tmp_path)) is None


def test_coherent_macos_chrome_persona_summary_and_details(tmp_path):
    p = fingerprint.generate_chrome_persona("macos", seed=12, coherence_026=True)
    fingerprint.save_chrome_persona(str(tmp_path), p, coherence_026=True)
    summary = fingerprint.summary(str(tmp_path))
    assert summary["os"] == "macos"
    assert summary["screen"] == f"{p['screen_width']}x{p['screen_height']}"
    details = fingerprint.extract_chrome_details(p, "macos")
    assert details["device_model"] == p["device_model"]
    assert str(p["screen_width"]) in details["screen"]
    assert details["color_depth"] == p["color_depth"]

    # Partial update changing to an M4 seed (39 % 13 == 0) must not keep old geometry or 14.x OS.
    m4_caller = {"platform": "macos", "seed": 39, "platform_version": "14.7.6"}
    caller_snapshot = dict(m4_caller)
    fingerprint.save_chrome_persona(str(tmp_path), m4_caller, coherence_026=True)
    assert m4_caller == caller_snapshot
    saved_m4 = fingerprint.load_saved_chrome_persona(str(tmp_path))
    assert saved_m4["seed"] == 39
    assert int(saved_m4["platform_version"].split(".")[0]) >= 15
    assert saved_m4["device_model"] is not None
    if p["seed"] % 13 != 0:
        assert saved_m4["device_model"] != p["device_model"]


def test_save_chrome_persona_auto_reset_preserves_identity_and_partial_updates(client, tmp_path):
    from chrome_engine import persona as chrome_persona

    prof_dir = str(tmp_path / "p205")
    base = fingerprint.generate_chrome_persona("macos", seed=3, cpu_cores=16, coherence_026=True)
    base["brand"] = "Edge"
    base["timezone"] = "Asia/Tokyo"
    base["locale"] = "ja-JP"
    fingerprint.save_chrome_persona(prof_dir, base, coherence_026=True)

    # Partial update omitting hardware_concurrency preserves explicit 16 cores & other identity fields.
    fingerprint.save_chrome_persona(prof_dir, {"seed": 3, "platform": "macos", "timezone": "UTC"})
    kept = fingerprint.load_saved_chrome_persona(prof_dir)
    assert kept["hardware_concurrency"] == 16
    assert kept["device_model"] == base["device_model"]
    assert kept["brand"] == "Edge"
    assert kept["locale"] == "ja-JP"
    assert kept["timezone"] == "UTC"

    # Explicit Auto reset (`hardware_concurrency=None`) recalculates only CPU while preserving identity.
    fingerprint.save_chrome_persona(
        prof_dir, {"seed": 3, "platform": "macos", "hardware_concurrency": None}
    )
    after_auto = fingerprint.load_saved_chrome_persona(prof_dir)
    assert after_auto["seed"] == 3
    assert after_auto["device_model"] == base["device_model"]
    assert after_auto["screen_width"] == base["screen_width"]
    assert after_auto["brand"] == "Edge"
    assert after_auto["locale"] == "ja-JP"
    assert after_auto["timezone"] == "UTC"
    assert (
        after_auto["hardware_concurrency"]
        == chrome_persona.generate("macos", seed=3).hardware_concurrency
    )

    # Full API PATCH round-trip: override 16 -> metadata-only PATCH -> Auto reset -> verify /fingerprint.
    created = client.post(
        "/api/v1/profiles",
        json={
            "name": "p205-api",
            "os_type": "macos",
            "engine": "chrome",
            "initial_fingerprint": {
                "seed": 100000020,
                "platform": "macos",
                "hardware_concurrency": 16,
            },
        },
    )
    assert created.status_code == 201
    assert client.get("/api/v1/profiles/p205-api/fingerprint").json()["hardware_concurrency"] == 16

    meta_only = client.patch("/api/v1/profiles/p205-api", json={"notes": "keep 16 cores"})
    assert meta_only.status_code == 200
    assert client.get("/api/v1/profiles/p205-api/fingerprint").json()["hardware_concurrency"] == 16

    reset_res = client.patch(
        "/api/v1/profiles/p205-api",
        json={
            "initial_fingerprint": {
                "seed": 100000020,
                "platform": "macos",
                "hardware_concurrency": None,
            }
        },
    )
    assert reset_res.status_code == 200
    fp_after = client.get("/api/v1/profiles/p205-api/fingerprint").json()
    use_026 = fingerprint._selected_chrome_supports_026()
    assert (
        fp_after["hardware_concurrency"]
        == chrome_persona.generate(
            "macos", seed=100000020, coherence_026=use_026
        ).hardware_concurrency
    )
    assert fp_after["hardware_concurrency"] != 16


def test_prefetch_skips_an_installed_engine(monkeypatch):
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda: True)
    assert chrome_prefetch.start() is False


def test_prefetch_runs_once_at_a_time(monkeypatch):
    import threading

    from chrome_engine import fetch
    from src.services.browser import chrome_prefetch

    release = threading.Event()
    calls = []

    def fake_install():
        calls.append(1)
        release.wait(5)

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda: False)
    monkeypatch.setattr(fetch, "install", fake_install)
    logs = []
    assert chrome_prefetch.start(logs.append) is True
    assert chrome_prefetch.start(logs.append) is False
    release.set()
    chrome_prefetch._thread.join(5)
    assert calls == [1]
    assert len(logs) == 2  # downloading..., ready


def test_api_refuses_chrome_on_an_unsupported_host(client, monkeypatch):
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "unsupported_reason", lambda: "no build for darwin/x86_64")
    r = client.post("/api/v1/profiles", json={"name": "c3", "engine": "chrome"})
    assert r.status_code == 400
    assert "darwin/x86_64" in r.json()["detail"]


def test_p1_run19_post_and_legacy_auto_reset_stay_launchable(client, workdir, monkeypatch):
    import asyncio
    import hashlib

    import playwright.async_api as pw

    from chrome_engine import engine as engine_mod
    from chrome_engine import persona as chrome_persona
    from chrome_engine.engine import ChromeEngine, EngineError
    from chrome_engine.native_bundle import bundle_sha256

    app = workdir / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"run19-binary")
    bin_sha = hashlib.sha256(b"run19-binary").hexdigest()
    b_sha = bundle_sha256(app)
    (workdir / "native-acceptance.json").write_text(
        json.dumps(
            {
                "binary_sha256": bin_sha,
                "bundle_sha256": b_sha,
                "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
                "cases": {"macos:2": {}, "macos:3": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", {b_sha})
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", frozenset())
    monkeypatch.setenv("FOXPROFILE_CHROME_EXECUTABLE", str(exe))

    class StopAtBrowser:
        async def start(self):
            raise RuntimeError("stop-before-browser")

    monkeypatch.setattr(pw, "async_playwright", lambda: StopAtBrowser())

    # (a) Web-shaped POST -> Run #19 baseline persona (no 026 fields) -> launchable.
    post_res = client.post(
        "/api/v1/profiles",
        json={
            "name": "web-mac",
            "os_type": "macos",
            "engine": "chrome",
            "initial_fingerprint": {"seed": 3, "platform": "macos"},
        },
    )
    assert post_res.status_code == 201
    saved_web = chrome_persona.load(workdir / "camoufox_data/web-mac")
    assert saved_web is not None
    assert not saved_web.requires_apple_silicon_026
    assert saved_web.device_model is None
    with pytest.raises(RuntimeError, match="stop-before-browser"):
        asyncio.run(
            ChromeEngine(
                workdir / "camoufox_data/web-mac", platform="macos", executable=exe
            ).start()
        )

    # (b) Legacy baseline saved persona (`16` -> `Auto`) keeps legacy baseline shape -> launchable.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "legacy-mac", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    legacy_dir = workdir / "camoufox_data/legacy-mac"
    legacy_p = chrome_persona.Persona(
        seed=45,
        platform="macos",
        platform_version="14.6.1",
        brand="Chrome",
        hardware_concurrency=16,
    )
    chrome_persona.save(legacy_dir, legacy_p)
    patch_auto = client.patch(
        "/api/v1/profiles/legacy-mac",
        json={
            "initial_fingerprint": {
                "seed": 45,
                "platform": "macos",
                "hardware_concurrency": None,
            }
        },
    )
    assert patch_auto.status_code == 200
    loaded_legacy = chrome_persona.load(legacy_dir)
    assert loaded_legacy is not None
    assert not loaded_legacy.requires_apple_silicon_026
    assert loaded_legacy.device_model is None
    assert loaded_legacy.hardware_concurrency == chrome_persona.default_cpu_for_persona(
        "macos", 45, coherence_026=False
    )
    with pytest.raises(RuntimeError, match="stop-before-browser"):
        asyncio.run(ChromeEngine(legacy_dir, platform="macos", executable=exe).start())

    # (c) Metadata-only edit keeps saved persona byte-identical; explicit 026 still fails closed on Run #19.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "explicit-026", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    p026_dir = workdir / "camoufox_data/explicit-026"
    p026 = fingerprint.generate_chrome_persona("macos", seed=3, coherence_026=True)
    fingerprint.save_chrome_persona(str(p026_dir), p026, coherence_026=True)
    bytes_before = chrome_persona.persona_path(p026_dir).read_bytes()
    assert (
        client.patch(
            "/api/v1/profiles/explicit-026",
            json={"notes": "metadata-only"},
        ).status_code
        == 200
    )
    assert chrome_persona.persona_path(p026_dir).read_bytes() == bytes_before
    with pytest.raises(EngineError, match="requires a verified 026\\+"):
        asyncio.run(ChromeEngine(p026_dir, platform="macos", executable=exe).start())


def test_p2_seedless_cpu_edit_and_flet_metadata_edit_preserve_identity(
    client, workdir, monkeypatch
):
    import flet as ft

    from chrome_engine import persona as chrome_persona
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(fingerprint, "_selected_chrome_supports_026", lambda **_: False)

    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "p2-mac", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    p2_dir = workdir / "camoufox_data/p2-mac"
    p026 = fingerprint.generate_chrome_persona("macos", seed=3, coherence_026=True)
    p026["brand"] = "Edge"
    fingerprint.save_chrome_persona(str(p2_dir), p026, coherence_026=True)
    expected_model = p026["device_model"]
    expected_width = p026["screen_width"]

    # Simulate Web UI after failed GET /fingerprint submitting seedless CPU-only PATCH.
    patch_cpu = client.patch(
        "/api/v1/profiles/p2-mac",
        json={"initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12}},
    )
    assert patch_cpu.status_code == 200
    saved = fingerprint.load_saved_chrome_persona(str(p2_dir))
    assert saved["seed"] == 3
    assert saved["device_model"] == expected_model
    assert saved["screen_width"] == expected_width
    assert saved["brand"] == "Edge"
    assert saved["hardware_concurrency"] == 12

    # Seedless Auto reset also preserves identity.
    assert (
        client.patch(
            "/api/v1/profiles/p2-mac",
            json={"initial_fingerprint": {"platform": "macos", "hardware_concurrency": None}},
        ).status_code
        == 200
    )
    saved_auto = fingerprint.load_saved_chrome_persona(str(p2_dir))
    assert saved_auto["seed"] == 3
    assert saved_auto["device_model"] == expected_model
    assert saved_auto["brand"] == "Edge"
    assert saved_auto["hardware_concurrency"] == chrome_persona.default_cpu_for_persona(
        "macos", 3, device_model=expected_model
    )

    # Flet edit dialog metadata-only submit omits initial_fingerprint and keeps chrome_persona.json byte-identical.
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

    page = DummyPage()
    captured = {}

    def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
        captured.update(kwargs)
        return None

    open_profile_dialog(
        page,
        None,
        on_save,
        Profile(name="p2-mac", os_type="macos", engine="chrome"),
    )
    controls = list(walk(page.dialog))
    save_btn = page.dialog.actions[-1]
    save_btn.on_click(None)
    assert captured.get("initial_fingerprint") is None

    # Explicit Roll inside Flet edit dialog marks hw_changed and submits updated persona.
    roll_btn = next(c for c in controls if getattr(c, "icon", None) == ft.Icons.CASINO_OUTLINED)
    roll_btn.on_click(None)
    captured.clear()
    save_btn.on_click(None)
    assert isinstance(captured.get("initial_fingerprint"), dict)


def test_generate_chrome_persona_capability_gating(monkeypatch):
    monkeypatch.setattr(fingerprint, "_selected_chrome_supports_026", lambda **_: False)
    legacy = {
        "seed": 45,
        "platform": "macos",
        "platform_version": "14.6.1",
        "brand": "Chrome",
        "hardware_concurrency": 16,
    }
    kept = fingerprint.generate_chrome_persona("macos", seed=45, base_persona=legacy)
    assert kept.get("device_model") is None
    assert kept["hardware_concurrency"] == 16

    rolled = fingerprint.generate_chrome_persona("macos", roll_new=True)
    assert rolled.get("device_model") is None

    explicit = fingerprint.generate_chrome_persona("macos", seed=3, coherence_026=True)
    assert explicit["device_model"] is not None


def _make_temp_bundle(root, name, *, verify_026=False, monkeypatch=None):
    import hashlib

    from chrome_engine import engine as engine_mod
    from chrome_engine.native_bundle import bundle_sha256

    base = root / name
    app = base / "Chromium.app"
    exe = app / "Contents/MacOS/Chromium"
    exe.parent.mkdir(parents=True)
    payload = f"{name}-binary".encode()
    exe.write_bytes(payload)
    bin_sha = hashlib.sha256(payload).hexdigest()
    b_sha = bundle_sha256(app)
    cases = {"macos:2": {}, "macos:3": {}}
    if verify_026:
        cases.update({"apple_silicon_m3_laptop": {}, "apple_silicon_m2_max_desktop": {}})
    (base / "native-acceptance.json").write_text(
        json.dumps(
            {
                "binary_sha256": bin_sha,
                "bundle_sha256": b_sha,
                "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    if monkeypatch is not None:
        monkeypatch.setattr(
            engine_mod, "NATIVE_REALM_BUNDLES", engine_mod.NATIVE_REALM_BUNDLES | {b_sha}
        )
        if verify_026:
            monkeypatch.setattr(
                engine_mod,
                "APPLE_SILICON_COHERENCE_BUNDLES",
                engine_mod.APPLE_SILICON_COHERENCE_BUNDLES | {b_sha},
            )
    return exe


def test_mixed_core_flet_roll_respects_bound_core_both_directions(client, workdir, monkeypatch):
    import asyncio

    import flet as ft
    import playwright.async_api as pw

    from chrome_engine import engine as engine_mod
    from chrome_engine import fetch
    from chrome_engine import persona as chrome_persona
    from chrome_engine.engine import ChromeEngine, EngineError
    from src.services.browser import cores
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", frozenset())
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", frozenset())
    monkeypatch.delenv("FOXPROFILE_CHROME_EXECUTABLE", raising=False)

    run19_exe = _make_temp_bundle(workdir, "run19", verify_026=False, monkeypatch=monkeypatch)
    c026_exe = _make_temp_bundle(workdir, "cand026", verify_026=True, monkeypatch=monkeypatch)

    class StopAtBrowser:
        async def start(self):
            raise RuntimeError("stop-before-browser")

    monkeypatch.setattr(pw, "async_playwright", lambda: StopAtBrowser())

    def walk(ctrl):
        yield ctrl
        for attr in ("content", "controls", "actions", "title"):
            children = getattr(ctrl, attr, None)
            for child in children if isinstance(children, list) else [children]:
                if isinstance(child, ft.Control):
                    yield from walk(child)

    class DummyPage:
        dialog = None

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            pass

    pm = ProfileManager()

    # Direction 1: default core is 026, profile is bound to Run #19.
    monkeypatch.setattr(fetch, "installed_executable", lambda *_: c026_exe)
    monkeypatch.setattr(
        cores,
        "bound_executable",
        lambda engine_name, ver: run19_exe if ver == "148.0.7778.215" else c026_exe,
    )
    assert pm.add_profile("bound-run19", "", "macos", engine="chrome")
    pdir1 = workdir / "camoufox_data/bound-run19"
    chrome_persona.save(
        pdir1,
        chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Chrome",
            hardware_concurrency=16,
        ),
    )
    pm.profiles["bound-run19"].core_version = "148.0.7778.215"
    pm.save_profiles()

    page = DummyPage()
    captured = {}

    def on_save1(name, proxy, os_type, timezone, locale, engine, **kwargs):
        captured.update(kwargs)
        pm.update_profile(
            "bound-run19",
            name,
            proxy,
            os_type,
            timezone,
            locale,
            **{
                k: kwargs[k]
                for k in (
                    "webrtc_mode",
                    "canvas_mode",
                    "initial_fingerprint",
                    "extension_ids",
                )
            },
        )
        return None

    open_profile_dialog(page, None, on_save1, pm.profiles["bound-run19"], profile_manager=pm)
    controls = list(walk(page.dialog))
    next(c for c in controls if getattr(c, "icon", None) == ft.Icons.CASINO_OUTLINED).on_click(None)
    page.dialog.actions[-1].on_click(None)

    assert isinstance(captured["initial_fingerprint"], dict)
    saved1 = chrome_persona.load(pdir1)
    assert saved1 is not None
    assert saved1.device_model is None
    assert not saved1.requires_apple_silicon_026
    with pytest.raises(RuntimeError, match="stop-before-browser"):
        asyncio.run(ChromeEngine(pdir1, platform="macos", executable=run19_exe).start())

    # Existing explicit 026 persona bytes still refuse cleanly on Run #19 without downgrade.
    assert pm.add_profile("explicit-026-bound", "", "macos", engine="chrome")
    pdir_exp = workdir / "camoufox_data/explicit-026-bound"
    p_exp = chrome_persona.generate("macos", seed=3, coherence_026=True)
    chrome_persona.save(pdir_exp, p_exp)
    exp_bytes = chrome_persona.persona_path(pdir_exp).read_bytes()
    with pytest.raises(EngineError, match="requires a verified 026\\+"):
        asyncio.run(ChromeEngine(pdir_exp, platform="macos", executable=run19_exe).start())
    assert chrome_persona.persona_path(pdir_exp).read_bytes() == exp_bytes

    # Direction 2 (inverse): default core is Run #19, profile is bound to 026.
    monkeypatch.setattr(fetch, "installed_executable", lambda *_: run19_exe)
    assert pm.add_profile("bound-026", "", "macos", engine="chrome")
    pdir2 = workdir / "camoufox_data/bound-026"
    chrome_persona.save(
        pdir2,
        chrome_persona.Persona(
            seed=45,
            platform="macos",
            platform_version="14.6.1",
            brand="Chrome",
            hardware_concurrency=8,
        ),
    )
    pm.profiles["bound-026"].core_version = "148.0.7778.215-026"
    pm.save_profiles()

    page2 = DummyPage()
    open_profile_dialog(
        page2,
        None,
        lambda name, proxy, os_type, timezone, locale, engine, **kw: (
            pm.update_profile(
                "bound-026",
                name,
                proxy,
                os_type,
                timezone,
                locale,
                initial_fingerprint=kw["initial_fingerprint"],
            )
            and None
        ),
        pm.profiles["bound-026"],
        profile_manager=pm,
    )
    controls2 = list(walk(page2.dialog))
    next(c for c in controls2 if getattr(c, "icon", None) == ft.Icons.CASINO_OUTLINED).on_click(
        None
    )
    page2.dialog.actions[-1].on_click(None)

    saved2 = chrome_persona.load(pdir2)
    assert saved2 is not None
    assert saved2.device_model is not None
    assert saved2.requires_apple_silicon_026
    with pytest.raises(RuntimeError, match="stop-before-browser"):
        asyncio.run(ChromeEngine(pdir2, platform="macos", executable=c026_exe).start())


def test_legacy_auto_cpu_preserves_identity_and_stale_web_seed(client, workdir, monkeypatch):
    import asyncio

    import playwright.async_api as pw

    from chrome_engine import engine as engine_mod
    from chrome_engine import fetch
    from chrome_engine import persona as chrome_persona
    from chrome_engine.engine import ChromeEngine

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", frozenset())
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", frozenset())

    run19_exe = _make_temp_bundle(workdir, "run19_auto", verify_026=False, monkeypatch=monkeypatch)
    c026_exe = _make_temp_bundle(workdir, "cand026_auto", verify_026=True, monkeypatch=monkeypatch)

    class StopAtBrowser:
        async def start(self):
            raise RuntimeError("stop-before-browser")

    monkeypatch.setattr(pw, "async_playwright", lambda: StopAtBrowser())

    for label, exe in (("run19", run19_exe), ("pinned026", c026_exe)):
        monkeypatch.setattr(fetch, "installed_executable", lambda *_, e=exe: e)
        monkeypatch.delenv("FOXPROFILE_CHROME_EXECUTABLE", raising=False)
        name = f"legacy-auto-{label}"
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": name, "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        pdir = workdir / "camoufox_data" / name
        orig = chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Chrome",
            hardware_concurrency=16,
        )
        chrome_persona.save(pdir, orig)
        orig_json = orig.to_json()

        res = client.patch(
            f"/api/v1/profiles/{name}",
            json={"initial_fingerprint": {"platform": "macos", "hardware_concurrency": None}},
        )
        assert res.status_code == 200
        updated_json = chrome_persona.load(pdir).to_json()
        assert updated_json["hardware_concurrency"] == chrome_persona.default_cpu_for_persona(
            "macos", 39, coherence_026=False
        )
        assert {k: v for k, v in updated_json.items() if k != "hardware_concurrency"} == {
            k: v for k, v in orig_json.items() if k != "hardware_concurrency"
        }
        with pytest.raises(RuntimeError, match="stop-before-browser"):
            asyncio.run(ChromeEngine(pdir, platform="macos", executable=run19_exe).start())

    # Stale Web Editor A (`seed=3`) vs newer Roll by Editor B (`seed=45`) + CPU-only save from A.
    monkeypatch.setattr(fetch, "installed_executable", lambda *_: c026_exe)
    assert (
        client.post(
            "/api/v1/profiles",
            json={
                "name": "stale-seed-race",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 3, "platform": "macos"},
            },
        ).status_code
        == 201
    )
    assert (
        client.patch(
            "/api/v1/profiles/stale-seed-race",
            json={"initial_fingerprint": {"seed": 45, "platform": "macos"}},
        ).status_code
        == 200
    )
    rolled_fp = client.get("/api/v1/profiles/stale-seed-race/fingerprint").json()
    assert rolled_fp["seed"] == 45
    rolled_saved = fingerprint.load_saved_chrome_persona(
        str(workdir / "camoufox_data/stale-seed-race")
    )
    assert rolled_saved["device_model"] is not None

    assert (
        client.patch(
            "/api/v1/profiles/stale-seed-race",
            json={"initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12}},
        ).status_code
        == 200
    )
    after_cpu = fingerprint.load_saved_chrome_persona(
        str(workdir / "camoufox_data/stale-seed-race")
    )
    assert after_cpu["seed"] == 45
    assert after_cpu["device_model"] == rolled_saved["device_model"]
    assert after_cpu["brand"] == rolled_saved["brand"]
    assert after_cpu["screen_width"] == rolled_saved["screen_width"]
    assert after_cpu["hardware_concurrency"] == 12

    # Same-seed legacy persona preserves legacy mode on partial CPU/Auto edit even when bound core is 026,
    # while explicit same-seed 026 migration succeeds (full & partial) unless blocked by bound Run #19.
    from src.services.browser import cores

    monkeypatch.setattr(
        cores,
        "bound_executable",
        lambda engine_name, ver: run19_exe if ver == "148.0.7778.215" else c026_exe,
    )
    leg_dir = workdir / "camoufox_data/same-seed-mig"
    leg_orig = chrome_persona.Persona(
        seed=39,
        platform="macos",
        platform_version="14.6.1",
        brand="Chrome",
        hardware_concurrency=16,
    )
    chrome_persona.save(leg_dir, leg_orig)

    # Partial Auto edit against bound-026 preserves legacy identity (no silent upgrade).
    fingerprint.save_chrome_persona(
        str(leg_dir),
        {"seed": 39, "platform": "macos", "hardware_concurrency": None},
        core_version="148.0.7778.215-026",
    )
    leg_after_bound_auto = chrome_persona.load(leg_dir)
    assert leg_after_bound_auto is not None
    assert not leg_after_bound_auto.requires_apple_silicon_026
    assert leg_after_bound_auto.platform_version == "14.6.1"
    assert leg_after_bound_auto.hardware_concurrency == 11

    # Full 026 payload against bound-Run19 is refused atomically with file bytes unchanged.
    m026_full = fingerprint.generate_chrome_persona("macos", seed=39, coherence_026=True)
    bytes_before_refusal = chrome_persona.persona_path(leg_dir).read_bytes()
    with pytest.raises(chrome_persona.PersonaError, match="require a verified 026\\+"):
        fingerprint.save_chrome_persona(str(leg_dir), m026_full, core_version="148.0.7778.215")
    assert chrome_persona.persona_path(leg_dir).read_bytes() == bytes_before_refusal

    # Explicit partial 026 migration on same-seed legacy fills full geometry and M4 >= 15 OS clamp.
    fingerprint.save_chrome_persona(
        str(leg_dir),
        {"seed": 39, "platform": "macos", "device_model": "MacBook Pro 14 (M4)"},
        coherence_026=True,
    )
    migrated_partial = chrome_persona.load(leg_dir)
    assert migrated_partial is not None
    assert migrated_partial.requires_apple_silicon_026
    assert migrated_partial.device_model == "MacBook Pro 14 (M4)"
    assert migrated_partial.screen_width == 1512
    assert migrated_partial.screen_height == 982
    assert migrated_partial.is_internal_display is True
    assert int(migrated_partial.platform_version.split(".")[0]) >= 15

    # Explicit full same-seed 026 migration succeeds even without coherence_026 kwarg.
    chrome_persona.save(leg_dir, leg_orig)
    fingerprint.save_chrome_persona(str(leg_dir), m026_full)
    migrated_full = chrome_persona.load(leg_dir)
    assert migrated_full is not None
    assert migrated_full.requires_apple_silicon_026
    assert migrated_full.device_model == m026_full["device_model"]


def test_save_contract_flet_cpu_patch_api_no_platform_and_atomic_refusal(
    client, workdir, monkeypatch
):
    import flet as ft

    from chrome_engine import engine as engine_mod
    from chrome_engine import persona as chrome_persona
    from src.services.browser import cores
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", frozenset())
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", frozenset())
    run19_exe = _make_temp_bundle(workdir, "run19_sc", verify_026=False, monkeypatch=monkeypatch)
    monkeypatch.setenv("FOXPROFILE_CHROME_EXECUTABLE", str(run19_exe))
    monkeypatch.setattr(cores, "bound_executable", lambda *_: run19_exe)

    # 1. [P2] Desktop Flet CPU-only edit sends partial CPU edit and preserves newer Roll B.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "flet-cpu-race", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    race_dir = workdir / "camoufox_data/flet-cpu-race"
    chrome_persona.save(
        race_dir,
        chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Edge",
            hardware_concurrency=16,
        ),
    )

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

    pm = client.app.state.container.profile_manager
    page = DummyPage()
    captured = {}

    def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
        captured.update(kwargs)
        pm.update_profile(
            "flet-cpu-race",
            name,
            proxy,
            os_type,
            timezone,
            locale,
            initial_fingerprint=kwargs["initial_fingerprint"],
        )
        return None

    open_profile_dialog(page, None, on_save, pm.profiles["flet-cpu-race"], profile_manager=pm)

    # While Flet Editor A is open, Editor B rolls to seed 45 on disk via PATCH.
    assert (
        client.patch(
            "/api/v1/profiles/flet-cpu-race",
            json={"initial_fingerprint": {"seed": 45, "platform": "macos"}},
        ).status_code
        == 200
    )
    rolled_b = chrome_persona.load(race_dir)
    assert rolled_b is not None and rolled_b.seed == 45

    # In Flet Editor A, select 12 Cores and Save without clicking Roll or changing OS.
    ctrls = list(walk(page.dialog))
    cores_dd = next(
        c
        for c in ctrls
        if isinstance(c, ft.Dropdown)
        and any(o.key == "12" for o in (c.options or []))
        and any(o.key == "16" for o in (c.options or []))
    )
    cores_dd.value = "12"
    cores_dd.on_select(None)
    page.dialog.actions[-1].on_click(None)

    assert captured["initial_fingerprint"] == {"platform": "macos", "hardware_concurrency": 12}
    after_flet = chrome_persona.load(race_dir)
    assert after_flet is not None
    assert after_flet.seed == 45
    assert after_flet.brand == rolled_b.brand
    assert after_flet.platform_version == rolled_b.platform_version
    assert after_flet.hardware_concurrency == 12

    # 1b. [P2] Two-editor race WITHOUT persona present at open + uninitialized save & Roll.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "flet-uninit-race", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    uninit_dir = workdir / "camoufox_data/flet-uninit-race"
    assert not chrome_persona.persona_path(uninit_dir).exists()

    uninit_captured: dict = {}

    def on_save_uninit(name, proxy, os_type, timezone, locale, engine, **kwargs):
        uninit_captured.update(kwargs)
        if kwargs["initial_fingerprint"] is not None:
            pm.update_profile(
                "flet-uninit-race",
                name,
                proxy,
                os_type,
                timezone,
                locale,
                initial_fingerprint=kwargs["initial_fingerprint"],
            )
        return None

    with monkeypatch.context() as mc:
        mc.setattr(chrome_persona.secrets, "randbelow", lambda _: 38)
        page_meta = DummyPage()
        open_profile_dialog(
            page_meta, None, on_save_uninit, pm.profiles["flet-uninit-race"], profile_manager=pm
        )
        page_a = DummyPage()
        open_profile_dialog(
            page_a, None, on_save_uninit, pm.profiles["flet-uninit-race"], profile_manager=pm
        )

    assert (
        client.patch(
            "/api/v1/profiles/flet-uninit-race",
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
    rolled_b_uninit = chrome_persona.load(uninit_dir)
    assert rolled_b_uninit is not None and rolled_b_uninit.seed == 45
    assert rolled_b_uninit.brand == "Edge" and rolled_b_uninit.hardware_concurrency == 16

    # Metadata-only save in uninitialized Editor A_meta must not emit preview or clobber disk.
    uninit_captured.clear()
    page_meta.dialog.actions[-1].on_click(None)
    assert uninit_captured["initial_fingerprint"] is None
    after_meta = chrome_persona.load(uninit_dir)
    assert after_meta == rolled_b_uninit

    # CPU-only save in uninitialized Editor A must emit seedless patch and preserve 45 / Edge.
    uninit_captured.clear()
    ctrls_a = list(walk(page_a.dialog))
    cores_a = next(
        c
        for c in ctrls_a
        if isinstance(c, ft.Dropdown)
        and any(o.key == "12" for o in (c.options or []))
        and any(o.key == "16" for o in (c.options or []))
    )
    cores_a.value = "12"
    cores_a.on_select(None)
    page_a.dialog.actions[-1].on_click(None)
    assert uninit_captured["initial_fingerprint"] == {
        "platform": "macos",
        "hardware_concurrency": 12,
    }
    after_uninit_cpu = chrome_persona.load(uninit_dir)
    assert after_uninit_cpu is not None
    assert after_uninit_cpu.seed == 45
    assert after_uninit_cpu.brand == "Edge"
    assert after_uninit_cpu.hardware_concurrency == 12

    # Uninitialized CPU-only save when disk is still empty initializes with requested CPU.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "flet-uninit-empty", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    empty_captured: dict = {}
    page_empty = DummyPage()
    open_profile_dialog(
        page_empty,
        None,
        lambda name, proxy, os_type, tz, loc, eng, **kw: (
            empty_captured.update(kw),
            pm.update_profile(
                "flet-uninit-empty",
                name,
                proxy,
                os_type,
                tz,
                loc,
                initial_fingerprint=kw["initial_fingerprint"],
            ),
            None,
        )[2],
        pm.profiles["flet-uninit-empty"],
        profile_manager=pm,
    )
    cores_empty = next(
        c
        for c in walk(page_empty.dialog)
        if isinstance(c, ft.Dropdown)
        and any(o.key == "12" for o in (c.options or []))
        and any(o.key == "16" for o in (c.options or []))
    )
    cores_empty.value = "12"
    cores_empty.on_select(None)
    page_empty.dialog.actions[-1].on_click(None)
    assert empty_captured["initial_fingerprint"] == {
        "platform": "macos",
        "hardware_concurrency": 12,
    }
    empty_saved = chrome_persona.load(workdir / "camoufox_data/flet-uninit-empty")
    assert (
        empty_saved is not None
        and empty_saved.platform == "macos"
        and empty_saved.hardware_concurrency == 12
    )

    # Explicit Roll on uninitialized profile still emits full persona payload with integer seed.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "flet-uninit-roll", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    roll_captured: dict = {}
    page_roll = DummyPage()
    open_profile_dialog(
        page_roll,
        None,
        lambda name, proxy, os_type, tz, loc, eng, **kw: (
            roll_captured.update(kw),
            pm.update_profile(
                "flet-uninit-roll",
                name,
                proxy,
                os_type,
                tz,
                loc,
                initial_fingerprint=kw["initial_fingerprint"],
            ),
            None,
        )[2],
        pm.profiles["flet-uninit-roll"],
        profile_manager=pm,
    )
    ctrls_roll = list(walk(page_roll.dialog))
    next(c for c in ctrls_roll if getattr(c, "icon", None) == ft.Icons.CASINO_OUTLINED).on_click(
        None
    )
    page_roll.dialog.actions[-1].on_click(None)
    assert isinstance(roll_captured["initial_fingerprint"].get("seed"), int)
    roll_saved = chrome_persona.load(workdir / "camoufox_data/flet-uninit-roll")
    assert (
        roll_saved is not None and roll_saved.seed == roll_captured["initial_fingerprint"]["seed"]
    )

    # 2. [P2] API PATCH without inner `platform` works for saved & first-launch macOS + rejects invalid writer payloads.
    chrome_persona.save(
        race_dir,
        chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Edge",
            hardware_concurrency=16,
        ),
    )
    assert (
        client.patch(
            "/api/v1/profiles/flet-cpu-race",
            json={"initial_fingerprint": {"hardware_concurrency": 12}},
        ).status_code
        == 200
    )
    saved_12 = chrome_persona.load(race_dir)
    assert saved_12 is not None
    assert (
        saved_12.seed == 39 and saved_12.platform == "macos" and saved_12.hardware_concurrency == 12
    )

    assert (
        client.patch(
            "/api/v1/profiles/flet-cpu-race",
            json={"initial_fingerprint": {"hardware_concurrency": None}},
        ).status_code
        == 200
    )
    saved_auto = chrome_persona.load(race_dir)
    assert saved_auto is not None
    assert (
        saved_auto.seed == 39
        and saved_auto.platform == "macos"
        and saved_auto.hardware_concurrency
        == chrome_persona.default_cpu_for_persona("macos", 39, coherence_026=False)
    )

    for pname, cpu_val in (("fresh-mac-12", 12), ("fresh-mac-auto", None)):
        assert (
            client.post(
                "/api/v1/profiles",
                json={"name": pname, "os_type": "macos", "engine": "chrome"},
            ).status_code
            == 201
        )
        assert (
            client.patch(
                f"/api/v1/profiles/{pname}",
                json={"initial_fingerprint": {"hardware_concurrency": cpu_val}},
            ).status_code
            == 200
        )
        created_p = chrome_persona.load(workdir / "camoufox_data" / pname)
        assert created_p is not None and created_p.platform == "macos"
        if cpu_val is not None:
            assert created_p.hardware_concurrency == cpu_val

    # Invalid writer requests surface HTTP 400 and leave chrome_persona.json bytes untouched.
    snap_invalid = chrome_persona.persona_path(race_dir).read_bytes()
    for bad_fp in (
        {"hardware_concurrency": 999},
        {"platform": "windows", "hardware_concurrency": 12},
    ):
        bad_res = client.patch(
            "/api/v1/profiles/flet-cpu-race",
            json={"initial_fingerprint": bad_fp},
        )
        assert bad_res.status_code == 400
        assert chrome_persona.persona_path(race_dir).read_bytes() == snap_invalid

    # 3. [P2] Blocked 026 migration against bound Run #19 surfaces HTTP 400 & leaves file bytes identical.
    assert (
        client.post(
            "/api/v1/profiles",
            json={"name": "bound-run19-api", "os_type": "macos", "engine": "chrome"},
        ).status_code
        == 201
    )
    pm.profiles["bound-run19-api"].core_version = "148.0.7778.215"
    pm.save_profiles()
    b_dir = workdir / "camoufox_data/bound-run19-api"
    chrome_persona.save(
        b_dir,
        chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Edge",
            hardware_concurrency=16,
        ),
    )
    snap_before = chrome_persona.persona_path(b_dir).read_bytes()
    mig_payload = chrome_persona.generate("macos", seed=39, coherence_026=True).to_json()
    ref_res = client.patch(
        "/api/v1/profiles/bound-run19-api",
        json={"initial_fingerprint": mig_payload},
    )
    assert ref_res.status_code == 400
    assert chrome_persona.persona_path(b_dir).read_bytes() == snap_before


def test_persistence_rollback_unbound_026_and_malformed_field_validation(workdir, monkeypatch):
    import pathlib

    from chrome_engine import engine as engine_mod
    from chrome_engine import fetch
    from chrome_engine import persona as chrome_persona
    from src.api.app import create_app
    from src.core.container import Container
    from src.services.browser import chrome_prefetch
    from src.services.profile import manager as pm_mod

    monkeypatch.setattr(chrome_prefetch, "start", lambda log=None: False)
    monkeypatch.setattr(engine_mod, "host_os", lambda: "macos")
    monkeypatch.setattr(engine_mod, "NATIVE_REALM_BUNDLES", frozenset())
    monkeypatch.setattr(engine_mod, "APPLE_SILICON_COHERENCE_BUNDLES", frozenset())
    monkeypatch.delenv("FOXPROFILE_CHROME_EXECUTABLE", raising=False)

    run19_exe = _make_temp_bundle(workdir, "run19_pr", verify_026=False, monkeypatch=monkeypatch)
    c026_exe = _make_temp_bundle(workdir, "c026_pr", verify_026=True, monkeypatch=monkeypatch)
    monkeypatch.setattr(fetch, "installed_executable", lambda *_: run19_exe)

    app = create_app(Container())
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    pm = app.state.container.profile_manager

    # Seed an existing profile so PATCH rollback & malformed-field tests have a baseline.
    assert (
        client.post(
            "/api/v1/profiles",
            json={
                "name": "before",
                "os_type": "macos",
                "engine": "chrome",
                "notes": "orig-notes",
                "initial_fingerprint": {
                    "seed": 39,
                    "platform": "macos",
                    "hardware_concurrency": 16,
                },
            },
        ).status_code
        == 201
    )
    reg_path = workdir / "profiles.json"
    before_dir = workdir / "camoufox_data/before"
    before_persona_path = chrome_persona.persona_path(before_dir)
    reg_snap = reg_path.read_bytes()
    persona_snap = before_persona_path.read_bytes()

    # 1a. [P1] POST with _write_chrome_persona raising OSError -> 500, no registry/folder/persona left behind.
    def fail_io(*_a, **_kw):
        raise OSError("injected-persona-io")

    with monkeypatch.context() as mc:
        mc.setattr(fingerprint, "_write_chrome_persona", fail_io)
        r_post_fail = client.post(
            "/api/v1/profiles",
            json={
                "name": "fail-post-io",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 39, "platform": "macos"},
            },
        )
    assert r_post_fail.status_code == 500
    assert reg_path.read_bytes() == reg_snap
    assert "fail-post-io" not in pm.profiles
    assert "fail-post-io" not in ProfileManager().profiles
    assert not (workdir / "camoufox_data/fail-post-io").exists()

    # 1b. [P1] PATCH rename before->after, notes, CPU 16->12 with writer OSError -> 500, full rollback.
    with monkeypatch.context() as mc:
        mc.setattr(fingerprint, "_write_chrome_persona", fail_io)
        r_patch_fail = client.patch(
            "/api/v1/profiles/before",
            json={
                "name": "after",
                "notes": "mutated-notes",
                "initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12},
            },
        )
    assert r_patch_fail.status_code == 500
    assert reg_path.read_bytes() == reg_snap
    assert before_dir.exists()
    assert not (workdir / "camoufox_data/after").exists()
    assert before_persona_path.read_bytes() == persona_snap
    assert pm.profiles["before"].notes == "orig-notes"
    reloaded = ProfileManager()
    assert "after" not in reloaded.profiles
    assert reloaded.profiles["before"].notes == "orig-notes"
    assert chrome_persona.load(before_dir).hardware_concurrency == 16

    # 1c. [P1] POST with manager._replace raising OSError -> 500, GET 404, no disk registry/persona left.
    pre_existing_dir = workdir / "camoufox_data/fail-post-preexist"
    pre_existing_dir.mkdir(parents=True)
    keep_marker = pre_existing_dir / "keep.txt"
    keep_marker.write_text("do-not-delete")
    with monkeypatch.context() as mc:
        mc.setattr(pm_mod, "_replace", fail_io)
        r_rep_fail = client.post(
            "/api/v1/profiles",
            json={
                "name": "fail-post-replace",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 39, "platform": "macos"},
            },
        )
        r_rep_preexist = client.post(
            "/api/v1/profiles",
            json={
                "name": "fail-post-preexist",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 39, "platform": "macos"},
            },
        )
    assert r_rep_fail.status_code == 500
    assert client.get("/api/v1/profiles/fail-post-replace").status_code == 404
    assert "fail-post-replace" not in pm.profiles
    assert "fail-post-replace" not in ProfileManager().profiles
    assert not (workdir / "camoufox_data/fail-post-replace").exists()
    assert r_rep_preexist.status_code == 500
    assert keep_marker.read_text() == "do-not-delete"
    assert not chrome_persona.persona_path(pre_existing_dir).exists()
    assert reg_path.read_bytes() == reg_snap

    # 1d. [P2] Publish-boundary Path.replace OSError cleans only own temp file & orphan new dir.
    pre_pub_dir = workdir / "camoufox_data/fail-post-pub-pre"
    pre_pub_dir.mkdir(parents=True)
    (pre_pub_dir / "keep.txt").write_text("keep-pub")
    (pre_pub_dir / "unrelated.tmp").write_text("unrelated-pub")
    (before_dir / "unrelated.tmp").write_text("keep-before-tmp")
    orig_path_replace = pathlib.Path.replace
    saw_pub_bytes = []

    def fail_publish_replace(self, target):
        if self.suffix == ".tmp" and pathlib.Path(target).name == chrome_persona.PERSONA_FILE:
            saw_pub_bytes.append(len(self.read_bytes()))
            raise OSError("injected-publish-replace")
        return orig_path_replace(self, target)

    with monkeypatch.context() as mc:
        mc.setattr(pathlib.Path, "replace", fail_publish_replace)
        r_pub_post = client.post(
            "/api/v1/profiles",
            json={
                "name": "fail-post-publish",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {
                    "seed": 39,
                    "platform": "macos",
                    "hardware_concurrency": 12,
                },
            },
        )
        r_pub_pre = client.post(
            "/api/v1/profiles",
            json={
                "name": "fail-post-pub-pre",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {
                    "seed": 39,
                    "platform": "macos",
                    "hardware_concurrency": 12,
                },
            },
        )
        r_pub_patch = client.patch(
            "/api/v1/profiles/before",
            json={
                "name": "after",
                "notes": "mutated-pub",
                "initial_fingerprint": {"platform": "macos", "hardware_concurrency": 12},
            },
        )
    assert len(saw_pub_bytes) == 3 and all(n > 0 for n in saw_pub_bytes)
    assert r_pub_post.status_code == 500
    assert r_pub_pre.status_code == 500
    assert r_pub_patch.status_code == 500
    assert reg_path.read_bytes() == reg_snap
    assert "fail-post-publish" not in pm.profiles
    assert "fail-post-publish" not in ProfileManager().profiles
    assert not (workdir / "camoufox_data/fail-post-publish").exists()
    assert sorted(p.name for p in pre_pub_dir.iterdir()) == ["keep.txt", "unrelated.tmp"]
    assert before_dir.exists() and not (workdir / "camoufox_data/after").exists()
    assert before_persona_path.read_bytes() == persona_snap
    assert sorted(p.name for p in before_dir.iterdir()) == ["chrome_persona.json", "unrelated.tmp"]
    assert pm.profiles["before"].notes == "orig-notes"
    assert ProfileManager().profiles["before"].notes == "orig-notes"
    (before_dir / "unrelated.tmp").unlink()

    # 2. [P2] Unbound explicit 026 POST validates actual default core (400 on Run #19, 201 on 026 candidate).
    p026_payload = chrome_persona.generate("macos", seed=39, coherence_026=True).to_json()
    r_unbound_ref = client.post(
        "/api/v1/profiles",
        json={
            "name": "unbound-026-on-run19",
            "os_type": "macos",
            "engine": "chrome",
            "initial_fingerprint": p026_payload,
        },
    )
    assert r_unbound_ref.status_code == 400
    assert reg_path.read_bytes() == reg_snap
    assert not (workdir / "camoufox_data/unbound-026-on-run19").exists()

    # Normal baseline create on Run #19 still succeeds.
    assert (
        client.post(
            "/api/v1/profiles",
            json={
                "name": "baseline-run19-ok",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {"seed": 39, "platform": "macos"},
            },
        ).status_code
        == 201
    )
    base_p = chrome_persona.load(workdir / "camoufox_data/baseline-run19-ok")
    assert base_p is not None and not base_p.requires_apple_silicon_026

    # Verified candidate fixture as default accepts unbound explicit 026 POST.
    with monkeypatch.context() as mc:
        mc.setattr(fetch, "installed_executable", lambda *_: c026_exe)
        r_unbound_ok = client.post(
            "/api/v1/profiles",
            json={
                "name": "unbound-026-on-cand",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": p026_payload,
            },
        )
    assert r_unbound_ok.status_code == 201
    cand_p = chrome_persona.load(workdir / "camoufox_data/unbound-026-on-cand")
    assert cand_p is not None and cand_p.requires_apple_silicon_026

    # 3. [P2] Malformed persona fields return HTTP 400 (never 500 or 200) on BOTH Run #19 and 026 candidate.
    reg_snap_now = reg_path.read_bytes()
    bad_payloads = (
        {"locale": 7, "hardware_concurrency": 12},
        {"platform_version": 9, "hardware_concurrency": 12},
        {"locale": "bad@x"},
        {"screen_width": "bad", "hardware_concurrency": 12},
    )
    for label_exe, target_exe in (("run19", run19_exe), ("cand026", c026_exe)):
        with monkeypatch.context() as mc:
            mc.setattr(fetch, "installed_executable", lambda *_, e=target_exe: e)
            for bad_fp in bad_payloads:
                res = client.patch(
                    "/api/v1/profiles/before",
                    json={"initial_fingerprint": bad_fp},
                )
                assert res.status_code == 400, (label_exe, bad_fp, res.status_code, res.text)
                assert before_persona_path.read_bytes() == persona_snap
                assert reg_path.read_bytes() == reg_snap_now

    # 4. [P2] Valid partial & full 026 display migrations + strict rejection of incompatible fields.
    assert (
        client.post(
            "/api/v1/profiles",
            json={
                "name": "partial-026-mig",
                "os_type": "macos",
                "engine": "chrome",
                "initial_fingerprint": {
                    "seed": 39,
                    "platform": "macos",
                    "hardware_concurrency": 16,
                },
            },
        ).status_code
        == 201
    )
    mig_dir = workdir / "camoufox_data/partial-026-mig"
    chrome_persona.save(
        mig_dir,
        chrome_persona.Persona(
            seed=39,
            platform="macos",
            platform_version="14.6.1",
            brand="Edge",
            hardware_concurrency=16,
        ),
    )
    with monkeypatch.context() as mc:
        mc.setattr(fetch, "installed_executable", lambda *_: c026_exe)
        r_part_ok = client.patch(
            "/api/v1/profiles/partial-026-mig",
            json={
                "initial_fingerprint": {
                    "screen_width": 1600,
                    "screen_height": 1000,
                    "hardware_concurrency": 12,
                }
            },
        )
        assert r_part_ok.status_code == 200, r_part_ok.text
        part_saved = chrome_persona.load(mig_dir)
        assert part_saved is not None
        assert part_saved.seed == 39
        assert part_saved.brand == "Edge"
        assert part_saved.hardware_concurrency == 12
        assert part_saved.screen_width == 1600
        assert part_saved.screen_height == 1000
        assert part_saved.avail_height is not None and part_saved.avail_height >= 240
        assert (part_saved.avail_top or 0) + part_saved.avail_height <= 1000
        assert int(part_saved.platform_version.split(".")[0]) >= 15
        assert part_saved.requires_apple_silicon_026 is True

        # Reject invalid/incompatible display & metadata combinations on BOTH migrated 026 and legacy profiles.
        mig_snap = chrome_persona.persona_path(mig_dir).read_bytes()
        reg_mig_snap = reg_path.read_bytes()
        common_bad_mix = (
            {"screen_width": "bad", "screen_height": 1000, "hardware_concurrency": 12},
            {"screen_width": 1600, "hardware_concurrency": 12},
            {"screen_width": 1600, "screen_height": 1000, "avail_top": 50, "avail_height": 980},
            {"screen_width": 1600, "screen_height": 260, "avail_top": 30},
            {"platform_version": None},
            {"brand": None},
        )
        for target_prof, p_file, p_snap in (
            ("before", before_persona_path, persona_snap),
            ("partial-026-mig", chrome_persona.persona_path(mig_dir), mig_snap),
        ):
            for bad_mix in common_bad_mix:
                r_bad_mig = client.patch(
                    f"/api/v1/profiles/{target_prof}",
                    json={"initial_fingerprint": bad_mix},
                )
                assert r_bad_mig.status_code == 400, (
                    target_prof,
                    bad_mix,
                    r_bad_mig.status_code,
                    r_bad_mig.text,
                )
                assert p_file.read_bytes() == p_snap
                assert reg_path.read_bytes() == reg_mig_snap
        r_bad_null026 = client.patch(
            "/api/v1/profiles/partial-026-mig",
            json={"initial_fingerprint": {"screen_width": None, "screen_height": None}},
        )
        assert r_bad_null026.status_code == 400
        assert chrome_persona.persona_path(mig_dir).read_bytes() == mig_snap
        assert reg_path.read_bytes() == reg_mig_snap

        # Valid full explicit 026 migration succeeds with exact payload values.
        full_mig = chrome_persona.generate("macos", seed=39, coherence_026=True).to_json()
        full_mig.update(
            {"screen_width": 1440, "screen_height": 900, "avail_top": 25, "avail_height": 807}
        )
        r_full_ok = client.patch(
            "/api/v1/profiles/partial-026-mig",
            json={"initial_fingerprint": full_mig},
        )
        assert r_full_ok.status_code == 200, r_full_ok.text
        full_saved = chrome_persona.load(mig_dir)
        assert full_saved is not None
        assert (full_saved.screen_width, full_saved.screen_height) == (1440, 900)
        assert (full_saved.avail_top, full_saved.avail_height) == (25, 807)
