import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chrome_engine import fetch
from chrome_engine.release import Asset, Release
from src.services.browser import cores


@pytest.fixture
def fixture_core(tmp_path, monkeypatch):
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("chrome", b"not a real browser; installer fixture only")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    asset = Asset(archive.as_uri(), digest, archive.stat().st_size, "zip", "chrome")
    release = Release("fixture-1", {"test": asset})
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "staging"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "installed"))
    monkeypatch.setattr(cores, "RELEASES", {"fixture-1": release})
    monkeypatch.setattr(cores, "_host_asset", lambda *_: asset)
    monkeypatch.setattr(
        cores,
        "_camoufox_inventory",
        lambda: {"engine": "camoufox", "selected_version": "old", "installed": True},
    )
    monkeypatch.setattr(cores, "_stack", lambda: dict(cores.PAIRED_STACK))
    return release, asset


def test_inventory_is_readonly_even_with_incomplete_stage(tmp_path, fixture_core, monkeypatch):
    monkeypatch.setattr(fetch, "installed_executable", lambda: None)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    data = cores.inventory()
    assert data["activation_allowed"] is True
    assert data["cores"][0]["selected_version"] == "old"
    assert not any(c["staged"] for c in data["candidates"])
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_stage_is_separate_idempotent_and_never_activates(tmp_path, fixture_core):
    release, _ = fixture_core
    profiles = tmp_path / "production"
    profiles.mkdir()
    cookies = profiles / "cookies.sqlite"
    cookies.write_bytes(b"private fixture")
    before = cookies.read_bytes()
    result = cores.stage("chrome", release.version, confirm_download=True)
    assert result == {
        "engine": "chrome",
        "version": "fixture-1",
        "staged": True,
        "activated": False,
    }
    exe = fetch.installed_release(release, cores.staging_home() / "chrome")
    assert exe.is_file()
    mtime = exe.stat().st_mtime_ns
    assert cores.stage("chrome", release.version, confirm_download=True) == result
    assert exe.stat().st_mtime_ns == mtime
    assert cookies.read_bytes() == before
    assert not (tmp_path / "installed").exists()


@pytest.mark.parametrize(
    "engine,version,confirm",
    [("chrome", "fixture-1", False), ("chrome", "latest", True), ("../escape", "fixture-1", True)],
)
def test_unconfirmed_or_unapproved_rejected(tmp_path, fixture_core, engine, version, confirm):
    with pytest.raises(cores.CoreError):
        cores.stage(engine, version, confirm_download=confirm)
    assert not cores.staging_home().exists()


def test_mismatched_stack_rejected(tmp_path, fixture_core, monkeypatch):
    monkeypatch.setattr(cores, "_stack", lambda: {})
    with pytest.raises(cores.CoreError, match="Runtime libraries"):
        cores.stage("chrome", "fixture-1", confirm_download=True)
    assert not cores.staging_home().exists()


def test_busy_stage_rejected(fixture_core):
    cores._STAGE_LOCK.acquire()
    try:
        with pytest.raises(cores.CoreError, match="already running"):
            cores.stage("chrome", "fixture-1", confirm_download=True)
    finally:
        cores._STAGE_LOCK.release()


def test_profile_directory_cannot_be_staging(fixture_core, monkeypatch):
    from src.core import config

    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(Path(config.DATA_DIR) / "cores"))
    with pytest.raises(cores.CoreError, match="separate"):
        cores.stage("chrome", "fixture-1", confirm_download=True)


def test_checksum_failure_leaves_no_install(fixture_core, monkeypatch):
    release, asset = fixture_core
    bad = Release(
        release.version,
        {"test": Asset(asset.url, "0" * 64, asset.size, asset.kind, asset.executable)},
    )
    monkeypatch.setattr(cores, "RELEASES", {bad.version: bad})
    monkeypatch.setattr(cores, "_host_asset", lambda *_: bad.assets["test"])
    with pytest.raises(fetch.FetchError, match="Checksum mismatch"):
        cores.stage("chrome", bad.version, confirm_download=True)
    assert fetch.installed_release(bad, cores.staging_home() / "chrome") is None
    assert not list(cores.staging_home().rglob("*.part"))


def test_marker_escape_or_wrong_hash_is_not_installed(tmp_path, fixture_core):
    release, asset = fixture_core
    root = tmp_path / release.version
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("no")
    marker = root / fetch.MARKER
    for data in [
        [],
        {"version": release.version, "sha256": asset.sha256, "executable": "../outside"},
        {"version": release.version, "sha256": "wrong", "executable": "../outside"},
    ]:
        marker.write_text(json.dumps(data))
        assert fetch.installed_release(release, tmp_path) is None


def test_camoufox_inventory_does_not_use_mutable_resolver(tmp_path, monkeypatch):
    from camoufox import multiversion, pkgman

    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(multiversion, "CONFIG_FILE", config)
    monkeypatch.setattr(multiversion, "list_installed", lambda: [])
    monkeypatch.setattr(multiversion, "get_active_path", lambda: pytest.fail("mutable resolver"))
    monkeypatch.setattr(
        pkgman, "camoufox_path", lambda *_: pytest.fail("download/cleanup resolver")
    )
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    before = config.stat().st_mtime_ns
    assert cores._camoufox_inventory()["installed"] is False
    assert config.read_text() == "{}"
    assert config.stat().st_mtime_ns == before


def test_api_auth_confirmation_and_activation_block(tmp_path, monkeypatch, fixture_core):
    from src.api import app
    from src.core.container import Container

    monkeypatch.setattr(app, "API_TOKEN", "x" * 32)
    # require_token reads its own module constant.
    from src.api import auth

    monkeypatch.setattr(auth, "API_TOKEN", "x" * 32)
    monkeypatch.setattr(fetch, "installed_executable", lambda: None)
    client = TestClient(app.create_app(Container()))
    assert client.get("/api/v1/cores").status_code == 401
    headers = {"Authorization": "Bearer " + "x" * 32}
    assert client.get("/api/v1/cores", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/v1/cores/stage",
            headers=headers,
            json={"engine": "chrome", "version": "fixture-1"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/cores/stage",
            headers=headers,
            json={"engine": "chrome", "version": "fixture-1", "url": "https://evil"},
        ).status_code
        == 422
    )
    assert client.post("/api/v1/cores/activate", headers=headers).status_code == 422
    assert not cores.staging_home().exists()


@pytest.mark.parametrize("where", ["engine", "version"])
def test_stage_rejects_symlink_into_profiles(tmp_path, fixture_core, monkeypatch, where):
    from src.core import config

    protected = tmp_path / "profiles"
    protected.mkdir()
    sentinel = protected / "session.fixture"
    sentinel.write_bytes(b"unchanged")
    monkeypatch.setattr(config, "DATA_DIR", str(protected))
    home = cores.staging_home()
    home.mkdir()
    if where == "engine":
        (home / "chrome").symlink_to(protected, target_is_directory=True)
    else:
        (home / "chrome").mkdir()
        (home / "chrome" / "fixture-1").symlink_to(protected, target_is_directory=True)
    with pytest.raises(cores.CoreError, match="symbolic links"):
        cores.stage("chrome", "fixture-1", confirm_download=True)
    assert sorted(p.name for p in protected.iterdir()) == ["session.fixture"]
    assert sentinel.read_bytes() == b"unchanged"


def test_stage_cannot_alias_active_cache(tmp_path, fixture_core, monkeypatch):
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path))
    with pytest.raises(cores.CoreError, match="active core caches"):
        cores.stage("chrome", "fixture-1", confirm_download=True)
    assert not (tmp_path / "installed").exists()


def test_api_checksum_failure_is_actionable(tmp_path, fixture_core, monkeypatch):
    from fastapi import FastAPI

    from src.api.routes.cores import router

    release, asset = fixture_core
    bad_asset = Asset(asset.url, "0" * 64, asset.size, asset.kind, asset.executable)
    monkeypatch.setattr(cores, "_host_asset", lambda *_: bad_asset)
    api = FastAPI()
    api.include_router(router)
    response = TestClient(api).post(
        "/cores/stage",
        json={"engine": "chrome", "version": release.version, "confirm_download": True},
    )
    assert response.status_code == 409
    assert "Checksum mismatch" in response.json()["detail"]
    assert fetch.installed_release(release, cores.staging_home() / "chrome") is None


@pytest.fixture
def apply_fixture(tmp_path, monkeypatch):
    from src.core import config, container
    from src.services.browser.launcher import BrowserLauncher
    from src.services.profile import manager

    data = tmp_path / "data"
    monkeypatch.setattr(manager, "DATA_DIR", str(data))
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(config, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(container, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "staging"))
    monkeypatch.setenv("FOXPROFILE_CORE_BACKUP_HOME", str(tmp_path / "backups"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "installed"))
    monkeypatch.setattr(cores, "_stack", lambda: dict(cores.PAIRED_STACK))
    old = tmp_path / "old-browser"
    old.write_bytes(b"old executable fixture")
    monkeypatch.setattr(fetch, "installed_executable", lambda *_: old)
    pm = manager.ProfileManager()
    assert pm.add_profile("qa", "None", "macos", engine="chrome")
    source = data / "qa"
    source.mkdir(exist_ok=True)
    (source / "session.fixture").write_bytes(b"synthetic cookies and session")
    (source / "persona.json").write_text('{"seed":123}')
    archive = tmp_path / "next.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("chrome", b"new executable fixture")
    asset = Asset(
        archive.as_uri(),
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        archive.stat().st_size,
        "zip",
        "chrome",
    )
    release = Release("999.0.0.1", {"test": asset})
    monkeypatch.setattr(cores, "RELEASES", {release.version: release})
    monkeypatch.setattr(cores, "_host_asset", lambda *_: asset)
    cores.stage("chrome", release.version, confirm_download=True)
    return pm, BrowserLauncher(), source, release


def apply_call(fixture, **kwargs):
    pm, bl, source, release = fixture
    return cores.backup_and_apply(
        pm, bl, "qa", "chrome", release.version, confirm_backup=True, confirm_apply=True, **kwargs
    )


def test_backed_up_apply_only_changes_future_binding(apply_fixture, tmp_path):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    result = apply_call(apply_fixture)
    assert (
        result["applied"] and not result["browser_started"] and not result["profile_data_changed"]
    )
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    backup = Path(result["backup_path"])
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    with zipfile.ZipFile(backup) as z:
        assert z.read("data/session.fixture") == before["session.fixture"]
        assert json.loads(z.read("profile.json"))["core_version"] is None
    assert json.loads((backup.parent / "recovery.json").read_text())["previous_executable"]
    assert pm.profiles["qa"].core_version == release.version
    assert manager.ProfileManager().profiles["qa"].core_version == release.version
    from src.services.browser.process import runner_command

    assert runner_command(pm.profiles["qa"])[-1] == release.version
    assert not bl.running_profile_names()


@pytest.mark.parametrize("backup,apply", [(False, False), (True, False), (False, True)])
def test_apply_needs_both_confirmations(apply_fixture, tmp_path, backup, apply):
    pm, bl, source, release = apply_fixture
    with pytest.raises(cores.CoreError, match="Separate"):
        cores.backup_and_apply(
            pm, bl, "qa", "chrome", release.version, confirm_backup=backup, confirm_apply=apply
        )
    assert pm.profiles["qa"].core_version is None
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("artifact", [".parentlock", "SingletonLock"])
def test_apply_refuses_native_browser_lock(apply_fixture, tmp_path, artifact):
    pm, bl, source, release = apply_fixture
    (source / artifact).write_text("held or stale: never remove")
    with pytest.raises((cores.CoreError, RuntimeError), match="[Bb]rowser lock"):
        apply_call(apply_fixture)
    assert (source / artifact).exists()
    assert pm.profiles["qa"].core_version is None
    assert not (tmp_path / "backups").exists()


def test_apply_refuses_running_or_busy_profile(apply_fixture, tmp_path):
    from src.services.browser.launcher import ProfileBusyError

    pm, bl, source, release = apply_fixture
    with bl.exclusive("qa"), pytest.raises(ProfileBusyError):
        apply_call(apply_fixture)
    assert not (tmp_path / "backups").exists()


def test_apply_refuses_shared_process_lease(apply_fixture, tmp_path):
    from src.services.browser.lease import profile_lease

    pm, bl, source, release = apply_fixture
    with profile_lease(source), pytest.raises(OSError):
        apply_call(apply_fixture)
    assert pm.profiles["qa"].core_version is None
    assert not (tmp_path / "backups").exists()


def test_apply_rolls_back_binding_when_persistence_fails(apply_fixture, tmp_path, monkeypatch):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    original = Path(manager.PROFILES_FILE).read_bytes()

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_replace", fail)
    with pytest.raises(OSError, match="disk full"):
        apply_call(apply_fixture)
    assert pm.profiles["qa"].core_version is None
    assert Path(manager.PROFILES_FILE).read_bytes() == original
    assert list((tmp_path / "backups").rglob("*.zip"))  # recovery retained


def test_apply_refuses_changed_metadata_from_other_process(apply_fixture, tmp_path):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    other = manager.ProfileManager()
    other.profiles["qa"].notes = "concurrent edit"
    other.save_profiles(strict=True)
    before = Path(manager.PROFILES_FILE).read_bytes()
    with pytest.raises(RuntimeError, match="another process"):
        apply_call(apply_fixture)
    assert pm.profiles["qa"].core_version is None
    assert Path(manager.PROFILES_FILE).read_bytes() == before


def test_apply_refuses_downgrade_or_different_engine(apply_fixture, tmp_path):
    pm, bl, source, release = apply_fixture
    pm.profiles["qa"].engine = "camoufox"
    with pytest.raises(cores.CoreError, match="Cross-engine"):
        apply_call(apply_fixture)
    pm.profiles["qa"].engine = "chrome"
    pm.profiles["qa"].core_version = "1000.0.0.1"
    # Missing previous bound binary fails closed even before comparing downgrade.
    with pytest.raises(cores.CoreError):
        apply_call(apply_fixture)
    assert not (tmp_path / "backups").exists()


def test_apply_refuses_symlinked_profile_content(apply_fixture, tmp_path):
    pm, bl, source, release = apply_fixture
    (source / "outside").symlink_to(tmp_path / "secret")
    with pytest.raises(cores.CoreError, match="symbolic links"):
        apply_call(apply_fixture)
    assert not (tmp_path / "backups").exists()


def test_binding_survives_edit_rename_and_export_import(apply_fixture, tmp_path):
    from src.services.profile.transfer import export_to_zip, import_from_zip

    pm, bl, source, release = apply_fixture
    apply_call(apply_fixture)
    assert pm.update_profile("qa", "renamed", "None", "macos")
    assert pm.profiles["renamed"].core_version == release.version
    export = tmp_path / "export"
    export.mkdir()
    ok, path = export_to_zip(pm.profiles["renamed"], str(source.parent / "renamed"), str(export))
    assert ok
    ok, profile = import_from_zip(path, str(tmp_path / "imported"))
    assert ok and profile.core_version == release.version


def test_missing_bound_core_does_not_touch_fingerprint_or_download(tmp_path, monkeypatch):
    import asyncio

    from chrome_engine import runner as chrome_runner
    from src.services.browser import runner

    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "missing"))
    monkeypatch.setattr(
        runner, "load_or_create", lambda *_: pytest.fail("identity mutation before validation")
    )
    monkeypatch.setattr(fetch, "install", lambda *_: pytest.fail("silent core fallback/download"))
    assert (
        asyncio.run(runner.run_browser("qa", "None", "macos", core_version=cores.CAMOUFOX_VERSION))
        == 1
    )
    assert (
        asyncio.run(chrome_runner.run_browser("qa", "None", "macos", core_version="unapproved"))
        == 1
    )
    assert not (tmp_path / "missing").exists()


def test_stale_normal_save_cannot_erase_applied_core(apply_fixture):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    stale = manager.ProfileManager()
    old_reference = stale.profiles["qa"]
    apply_call(apply_fixture)
    stale.profiles["qa"].notes = "stale edit"
    with pytest.raises(manager.ProfileStoreChangedError, match="another process"):
        stale.save_profiles()
    assert stale.profiles["qa"].core_version == release.version
    assert manager.ProfileManager().profiles["qa"].core_version == release.version
    assert old_reference.core_version is None
    assert cores.launch_binding("qa", "chrome") == release.version


def test_stale_rename_rejected_before_moving_data(apply_fixture):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    stale = manager.ProfileManager()
    apply_call(apply_fixture)
    with pytest.raises(manager.ProfileStoreChangedError):
        stale.update_profile("qa", "moved", "None", "macos")
    assert source.exists() and not source.with_name("moved").exists()
    assert stale.profiles["qa"].core_version == release.version


def test_corrupt_existing_binding_store_fails_before_browser_mutation(tmp_path, monkeypatch):
    import asyncio

    from chrome_engine import runner as chrome_runner
    from src.core import config
    from src.services.browser import runner

    store = tmp_path / "profiles.json"
    store.write_text("{invalid json")
    monkeypatch.setattr(config, "PROFILES_FILE", str(store))
    monkeypatch.setattr(runner, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(chrome_runner, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(runner, "load_or_create", lambda *_: pytest.fail("identity mutation"))
    monkeypatch.setattr(fetch, "install", lambda *_: pytest.fail("fallback download"))
    monkeypatch.setattr(runner, "_setup_signals", lambda *_: None)
    monkeypatch.setattr("sys.argv", ["runner", "qa", "None", "macos"])
    assert asyncio.run(runner._async_main()) == 1
    assert asyncio.run(chrome_runner._async_main(["qa", "None", "macos"])) == 1
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX record lock test")
def test_firefox_persistent_lock_allows_stopped_and_refuses_held(tmp_path):
    import subprocess
    import sys

    from src.services.browser.lease import native_profile_guard

    lock = tmp_path / ".parentlock"
    lock.write_bytes(b"unchanged lock content")
    before = lock.read_bytes()
    code = 'import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
    with native_profile_guard(tmp_path, "camoufox"):
        child = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
        assert child.returncode != 0
    assert lock.read_bytes() == before
    child = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
    assert child.returncode == 0


def test_backup_excludes_persistent_native_lock_without_removing_it(
    apply_fixture, tmp_path, monkeypatch
):
    pm, bl, source, release = apply_fixture
    pm.profiles["qa"].engine = "camoufox"
    monkeypatch.setattr(cores, "approved", lambda *_: release)
    monkeypatch.setattr(
        cores,
        "inventory",
        lambda: {
            "cores": [
                {
                    "engine": "camoufox",
                    "selected_version": "148.0.0",
                    "executable": str(tmp_path / "old-browser"),
                }
            ]
        },
    )
    original = cores.bound_executable("chrome", release.version)
    monkeypatch.setattr(cores, "bound_executable", lambda *_: original)
    lock = source / ".parentlock"
    lock.write_bytes(b"original native lock")
    result = cores.backup_and_apply(
        pm, bl, "qa", "camoufox", release.version, confirm_backup=True, confirm_apply=True
    )
    assert result["applied"]
    assert lock.read_bytes() == b"original native lock"
    with zipfile.ZipFile(result["backup_path"]) as archive:
        assert "data/.parentlock" not in archive.namelist()
        assert archive.read("data/session.fixture") == b"synthetic cookies and session"


def test_store_lease_spans_filesystem_rename_transaction(apply_fixture, monkeypatch):
    from src.services.browser.lease import profile_lease
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    original_save = pm.save_profiles

    def guarded_save(*args, **kwargs):
        # A different OS-lock holder cannot enter between rename and commit.
        with pytest.raises(OSError), profile_lease(Path(manager.PROFILES_FILE)):
            pytest.fail("store lease missing across filesystem mutation")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(pm, "save_profiles", guarded_save)
    assert pm.update_profile("qa", "renamed", "None", "macos")
    disk = manager.ProfileManager()
    assert "renamed" in disk.profiles
    assert source.with_name("renamed").exists() and not source.exists()


def test_crlf_store_load_edit_and_apply_is_not_stale(apply_fixture):
    from src.services.profile import manager

    pm, bl, source, release = apply_fixture
    path = Path(manager.PROFILES_FILE)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    fresh = manager.ProfileManager()
    assert fresh.update_profile("qa", "qa", "None", "macos", notes="first edit")
    assert fresh.update_profile("qa", "qa", "None", "macos", notes="second edit")
    assert cores.backup_and_apply(
        fresh, bl, "qa", "chrome", release.version, confirm_backup=True, confirm_apply=True
    )["applied"]
    assert manager.ProfileManager().profiles["qa"].core_version == release.version


def test_failed_prepare_never_publishes_ready_marker(tmp_path, fixture_core):
    release, asset = fixture_core

    def fail(*_):
        raise OSError("prepare failed")

    with pytest.raises(OSError, match="prepare failed"):
        fetch.install_release(release, asset, tmp_path / "prepare", prepare=fail)
    assert fetch.installed_release(release, tmp_path / "prepare") is None
    assert not list((tmp_path / "prepare").glob(".*"))


def test_generic_lease_windows_mode_does_not_reject_synthetic_0777(tmp_path, monkeypatch):
    # Exercise the platform-specific mode decision without invoking Win32 locks.
    import ast
    import inspect

    from src.services.browser import lease

    source = inspect.getsource(lease.profile_lease.__wrapped__)
    condition = next(
        n.test
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.If) and "root.is_symlink" in ast.unparse(n.test)
    )
    import types

    fake_root = types.SimpleNamespace(
        is_symlink=lambda: False, stat=lambda: types.SimpleNamespace(st_mode=0o777)
    )
    assert (
        eval(
            compile(ast.Expression(condition), "<lease-mode>", "eval"),
            {"root": fake_root, "os": types.SimpleNamespace(name="nt")},
        )
        is False
    )
    assert eval(
        compile(ast.Expression(condition), "<lease-mode>", "eval"),
        {"root": fake_root, "os": types.SimpleNamespace(name="posix")},
    )


def test_windows_apply_fails_closed_before_backup_or_profile_access(monkeypatch):
    import types

    monkeypatch.setattr(cores, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        cores,
        "bound_executable",
        lambda *_: pytest.fail("must not access core/profile before ACL guard"),
    )
    with pytest.raises(cores.CoreError, match="ACL verification"):
        cores.backup_and_apply(
            None, None, "unused", "chrome", "unused", confirm_backup=True, confirm_apply=True
        )


def test_unsupported_host_disables_candidates_without_losing_inventory(tmp_path, monkeypatch):
    from chrome_engine.release import UnsupportedPlatformError

    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "missing"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "installed"))
    monkeypatch.setattr(
        cores,
        "_camoufox_inventory",
        lambda: {"engine": "camoufox", "selected_version": "existing", "installed": True},
    )

    def unsupported():
        raise UnsupportedPlatformError("unsupported host fixture")

    monkeypatch.setattr(cores, "host_platform", unsupported)
    data = cores.inventory()
    assert data["cores"][0]["installed"]
    assert not any(c["can_stage"] for c in data["candidates"])
    with pytest.raises(cores.CoreError, match="unsupported host"):
        cores.stage("chrome", "148.0.7778.215", confirm_download=True)
    assert not (tmp_path / "missing").exists()


def test_external_storage_root_selected_when_mounted_and_writable(tmp_path, monkeypatch):
    monkeypatch.delenv("FOXPROFILE_CORE_HOME", raising=False)
    monkeypatch.delenv("FOXPROFILE_CORE_BACKUP_HOME", raising=False)
    monkeypatch.delenv("FOXPROFILE_CHROME_HOME", raising=False)
    ext_volume = tmp_path / "KINGSTON"
    ext_volume.mkdir()
    ext_root = ext_volume / "RonC-Profile-Storage"
    monkeypatch.setattr(cores, "EXTERNAL_STORAGE_ROOT", ext_root)
    assert cores.staging_home() == ext_root / "core-staging"
    assert cores.backup_home() == ext_root / "core-backups"

    unmounted = tmp_path / "NO_DRIVE" / "RonC-Profile-Storage"
    monkeypatch.setattr(cores, "EXTERNAL_STORAGE_ROOT", unmounted)
    assert cores.staging_home() == fetch.engine_home().parent / "core-staging"
    assert cores.backup_home() == fetch.engine_home().parent / "core-backups"


def test_prune_unused_removes_superseded_backups_and_unbound_cores(
    apply_fixture, tmp_path, monkeypatch
):
    pm, bl, source, release1 = apply_fixture

    def make_release(ver: str) -> Release:
        zpath = tmp_path / f"archive-{ver}.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("chrome", f"binary-{ver}".encode())
        asset = Asset(
            zpath.as_uri(),
            hashlib.sha256(zpath.read_bytes()).hexdigest(),
            zpath.stat().st_size,
            "zip",
            "chrome",
        )
        return Release(ver, {"test": asset})

    release2 = make_release("999.0.0.2")
    release3 = make_release("999.0.0.3")
    release_unused = make_release("999.0.0.4")
    all_releases = {r.version: r for r in (release1, release2, release3, release_unused)}
    monkeypatch.setattr(cores, "RELEASES", all_releases)
    monkeypatch.setattr(cores, "_host_asset", lambda _eng, rel: next(iter(rel.assets.values())))

    for rel in (release2, release3, release_unused):
        cores.stage("chrome", rel.version, confirm_download=True)

    # Upgrade path: default -> 999.0.0.1 -> 999.0.0.2 -> 999.0.0.3
    for rel in (release1, release2, release3):
        res = cores.backup_and_apply(
            pm, bl, "qa", "chrome", rel.version, confirm_backup=True, confirm_apply=True
        )
        assert res["applied"]

    with pytest.raises(cores.CoreError, match="Explicit prune confirmation"):
        cores.prune_unused(pm, bl, confirm_prune=False)

    with bl.exclusive("qa"), pytest.raises(cores.CoreError, match="Stop all running"):
        cores.prune_unused(pm, bl, confirm_prune=True)

    pruned = cores.prune_unused(pm, bl, confirm_prune=True)
    assert pruned["pruned"] is True
    assert pruned["removed_backups"] == 2
    assert set(pruned["removed_cores"]) == {"chrome:999.0.0.1", "chrome:999.0.0.4"}
    assert pruned["freed_bytes"] > 0

    # Active bound core (999.0.0.3) and rollback core of kept latest backup (999.0.0.2) remain
    assert (cores.staging_home() / "chrome" / "999.0.0.3").is_dir()
    assert (cores.staging_home() / "chrome" / "999.0.0.2").is_dir()
    assert not (cores.staging_home() / "chrome" / "999.0.0.1").exists()
    assert not (cores.staging_home() / "chrome" / "999.0.0.1.lock").exists()
    assert not (cores.staging_home() / "chrome" / "999.0.0.4").exists()
    assert not (cores.staging_home() / "chrome" / "999.0.0.4.lock").exists()
    remaining_backups = [p for p in cores.backup_home().iterdir() if p.name.startswith("core-")]
    assert len(remaining_backups) == 1


def test_prune_refuses_corrupt_backup_without_deleting_anything(apply_fixture, tmp_path):
    pm, bl, source, release = apply_fixture
    apply_call(apply_fixture)
    corrupt = cores.backup_home() / "core-corrupt"
    corrupt.mkdir(parents=True)
    for bad_manifest in ("{bad json", "[]", '{"engine": "chrome"}'):
        (corrupt / "recovery.json").write_text(bad_manifest)
        with pytest.raises(cores.CoreError, match="unreadable snapshot"):
            cores.prune_unused(pm, bl, confirm_prune=True)
    assert (cores.staging_home() / "chrome" / release.version).is_dir()


def test_prune_rejects_corrupt_payload_and_preserves_all_recovery(apply_fixture):
    import shutil
    import struct

    pm, bl, _, release = apply_fixture
    apply_call(apply_fixture)
    latest = next(cores.backup_home().glob("core-*"))
    older = cores.backup_home() / "core-older"
    shutil.copytree(latest, older)
    meta = json.loads((older / "recovery.json").read_text())
    meta["created_at"] = "2000-01-01T00:00:00+00:00"
    (older / "recovery.json").write_text(json.dumps(meta))
    archive = next(latest.glob("*.zip"))
    with zipfile.ZipFile(archive) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    with zipfile.ZipFile(archive) as zf:
        offset = zf.getinfo("data/session.fixture").header_offset
    raw = bytearray(archive.read_bytes())
    name_len, extra_len = struct.unpack_from("<HH", raw, offset + 26)
    raw[offset + 30 + name_len + extra_len] ^= 1
    archive.write_bytes(raw)
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() == "data/session.fixture"
        assert json.loads(zf.read("profile.json"))["name"] == "qa"
    unused = cores.staging_home() / "chrome" / "unused-rollback"
    unused.mkdir()
    (unused / "chrome").write_bytes(b"synthetic rollback core")
    before = {
        p: p.read_bytes()
        for root in (cores.backup_home(), cores.staging_home())
        for p in root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(cores.CoreError, match="unreadable snapshot"):
        cores.prune_unused(pm, bl, confirm_prune=True)
    assert all(p.is_file() and p.read_bytes() == data for p, data in before.items())
    assert (cores.staging_home() / "chrome" / release.version).is_dir()


@pytest.mark.parametrize("operation", ["stage", "prune"])
def test_storage_operations_refuse_other_process_lease(apply_fixture, operation, tmp_path):
    import os
    import subprocess
    import sys

    pm, bl, _, release = apply_fixture
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import sys
from src.services.browser.lease import profile_lease
with profile_lease(sys.argv[1]):
    print('locked', flush=True)
    sys.stdin.read()
""",
            str(cores.staging_home()),
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        before = {p: p.read_bytes() for p in cores.staging_home().rglob("*") if p.is_file()}
        with pytest.raises(BlockingIOError):
            if operation == "stage":
                cores.stage("chrome", release.version, confirm_download=True)
            else:
                cores.prune_unused(pm, bl, confirm_prune=True)
        assert all(p.is_file() and p.read_bytes() == data for p, data in before.items())
    finally:
        child.communicate(timeout=10)
    # Both the process-local and storage leases are released after rejection.
    assert cores.stage("chrome", release.version, confirm_download=True)["staged"]
    assert cores.prune_unused(pm, bl, confirm_prune=True)["pruned"]
