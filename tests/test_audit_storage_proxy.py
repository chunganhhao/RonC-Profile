import asyncio
import json
import zipfile

import pytest

from src.services.profile import manager as manager_module
from src.services.profile.transfer import import_from_zip
from src.services.proxy.geo_check import requests_proxy_url
from src.utils.proxy_parser import require_proxy


def archive(path, *, name="Existing", engine="chrome", proxy=None, files=None):
    with zipfile.ZipFile(path, "w") as out:
        out.writestr("profile.json", json.dumps({"name": name, "engine": engine, "proxy": proxy}))
        for name, content in (files or {}).items():
            out.writestr(name, content)
    return str(path)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(tmp_path / "data"))
    result = manager_module.ProfileManager()
    assert result.add_profile("Existing", "", "windows")
    old = tmp_path / "data" / "Existing"
    (old / "cookies.sqlite").write_text("keep-session")
    return result, old


def test_bad_import_preserves_session_and_metadata(manager, tmp_path):
    pm, old = manager
    path = archive(tmp_path / "bad.zip", files={"data/../../escape": "bad"})
    assert pm.import_profile(path, overwrite=True)[0] is False
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert pm.profiles["Existing"].engine == "camoufox"


def test_metadata_failure_rolls_back_session_and_name(manager, tmp_path, monkeypatch):
    pm, old = manager
    before = (tmp_path / "profiles.json").read_bytes()
    path = archive(tmp_path / "good.zip", name="existing", files={"data/new": "new"})

    def fail(*, strict=False):
        assert strict
        raise OSError("metadata-write-failed")

    monkeypatch.setattr(pm, "save_profiles", fail)
    with pytest.raises(OSError, match="metadata-write-failed"):
        pm.import_profile(path, overwrite=True)
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert not (old / "new").exists()
    assert list(pm.profiles) == ["Existing"]
    assert (tmp_path / "profiles.json").read_bytes() == before


def test_successful_engine_change_starts_clean(manager, tmp_path):
    pm, old = manager
    path = archive(tmp_path / "good.zip", files={"data/new": "new"})
    assert pm.import_profile(path, overwrite=True) == (True, "Existing")
    assert not (old / "cookies.sqlite").exists()
    assert (old / "new").read_text() == "new"
    assert pm.profiles["Existing"].engine == "chrome"


def test_same_engine_overlay_keeps_unmentioned_files(manager, tmp_path):
    pm, old = manager
    path = archive(tmp_path / "good.zip", engine="camoufox", files={"data/new": "new"})
    assert pm.import_profile(path, overwrite=True)[0]
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert (old / "new").read_text() == "new"


def test_import_does_not_copy_persistent_firefox_lock_inode(manager, tmp_path):
    pm, old = manager
    (old / ".parentlock").write_text("persistent")
    path = archive(tmp_path / "good.zip", engine="camoufox", files={"data/new": "new"})
    assert pm.import_profile(path, overwrite=True)[0]
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert not (old / ".parentlock").exists()


def test_publish_failure_restores_existing_session(manager, tmp_path, monkeypatch):
    from pathlib import Path

    pm, old = manager
    path = archive(tmp_path / "good.zip", files={"data/new": "new"})
    rename = Path.rename

    def fail_stage(self, target):
        if self.parent.name == "incoming":
            raise OSError("publish-failed")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_stage)
    with pytest.raises(OSError, match="publish-failed"):
        pm.import_profile(path, overwrite=True)
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert pm.profiles["Existing"].engine == "camoufox"


def test_failed_rollback_keeps_original_metadata_backup_and_releases_lock(
    manager, tmp_path, monkeypatch
):
    from pathlib import Path

    from src.services.browser.lease import profile_lease

    pm, old = manager
    path = archive(tmp_path / "good.zip", files={"data/new": "new"})
    rename = Path.rename

    def fail_save(*, strict=False):
        raise OSError("metadata-write-failed")

    def fail_restore(self, target):
        if self.name == "previous":
            raise OSError("restore-failed")
        return rename(self, target)

    monkeypatch.setattr(pm, "save_profiles", fail_save)
    monkeypatch.setattr(Path, "rename", fail_restore)
    with pytest.raises(OSError, match="old session retained"):
        pm.import_profile(path, overwrite=True)
    assert pm.profiles["Existing"].engine == "camoufox"
    assert json.loads((tmp_path / "profiles.json").read_text())["Existing"]["engine"] == "camoufox"
    backups = list((tmp_path / "data").glob(".import_*/previous/cookies.sqlite"))
    assert len(backups) == 1 and backups[0].read_text() == "keep-session"
    with profile_lease(old):
        pass


def test_same_engine_late_invalid_entry_does_not_overwrite(manager, tmp_path):
    pm, old = manager
    path = archive(
        tmp_path / "bad.zip",
        engine="camoufox",
        files={"data/cookies.sqlite": "overwritten", "data/../../escape": "bad"},
    )
    assert not pm.import_profile(path, overwrite=True)[0]
    assert (old / "cookies.sqlite").read_text() == "keep-session"


def test_changed_archive_name_cannot_publish_outside_locked_profile(manager, tmp_path, monkeypatch):
    from src.models.profile import Profile

    pm, old = manager
    path = archive(tmp_path / "good.zip")
    monkeypatch.setattr(manager_module, "import_from_zip", lambda *_: (True, Profile("Other")))
    assert not pm.import_profile(path, overwrite=True)[0]
    assert (old / "cookies.sqlite").read_text() == "keep-session"
    assert list(pm.profiles) == ["Existing"]


@pytest.mark.parametrize(
    "value", ["socks5://proxy.example:notaport", "ftp://proxy.example:1234", "bad"]
)
def test_bad_proxy_fails_closed_on_import_and_network(value, tmp_path):
    path = archive(tmp_path / "bad.zip", proxy=value)
    assert import_from_zip(path, str(tmp_path / "out"))[0] is False
    with pytest.raises(ValueError, match="refusing direct"):
        require_proxy(value)
    with pytest.raises(ValueError, match="refusing direct"):
        requests_proxy_url(value)


def test_runner_rejects_bad_proxy_before_network_or_identity(monkeypatch):
    from src.services.browser import runner

    def forbidden(*args):
        pytest.fail("Invalid proxy reached identity/network work")

    monkeypatch.setattr(runner, "load_or_create", forbidden)
    monkeypatch.setattr(runner, "exit_ip", forbidden)
    assert asyncio.run(runner.run_browser("bad", "socks5://proxy.example:notaport", "windows")) == 1


def test_empty_proxy_stays_direct_and_provider_credentials_still_work():
    assert require_proxy("") is None
    assert requests_proxy_url(None) is None
    assert require_proxy("proxy.example:1080:user:p@s:s")["password"] == "p@s:s"
    assert requests_proxy_url("socks5://proxy.example:1080").startswith("socks5h://")


def test_scheme_case_normalization_preserves_credential_case():
    from src.utils.validation import validate_proxy_format

    value = "SOCKS5://User:PaSs@proxy.example:1080"
    assert validate_proxy_format(value)[0]
    assert require_proxy(value) == {
        "server": "socks5://proxy.example:1080",
        "username": "User",
        "password": "PaSs",
    }
    assert requests_proxy_url(value) == "socks5h://User:PaSs@proxy.example:1080"


# --- External /Volumes mount guard & .env test isolation ---------------------


def test_require_data_dir_mounted_rejects_unmounted_and_stale_mount_dir(tmp_path, monkeypatch):
    import errno
    import os
    from pathlib import Path

    from src.core import config

    monkeypatch.setattr(config.sys, "platform", "darwin")
    target = Path("/Volumes/UNMOUNTED_TEST_VOL/RonC-Profile-Storage/camoufox_data")
    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)

    with pytest.raises(config.StorageVolumeUnmountedError) as caught:
        config.ensure_data_dir(target)
    assert caught.value.errno == errno.ENODEV
    assert "/Volumes/UNMOUNTED_TEST_VOL" in str(caught.value)

    # Case-insensitive /volumes/* paths and the /Volumes mount root itself must also fail closed.
    lower_target = Path("/volumes/UNMOUNTED_TEST_VOL/RonC-Profile-Storage/camoufox_data")
    with pytest.raises(config.StorageVolumeUnmountedError) as caught_lower:
        config.ensure_data_dir(lower_target)
    assert caught_lower.value.errno == errno.ENODEV
    for mount_root in (Path("/Volumes"), Path("/volumes")):
        with pytest.raises(config.StorageVolumeUnmountedError, match="mount root"):
            config.require_data_dir_mounted(mount_root)

    # Also catches a local symlink whose realpath targets an unmounted /Volumes/* drive.
    link = tmp_path / "camoufox_data"
    link.symlink_to(target)
    with pytest.raises(config.StorageVolumeUnmountedError):
        config.require_data_dir_mounted(link)


def test_require_data_dir_mounted_allows_mounted_volume_and_root_symlink(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from src.core import config

    monkeypatch.setattr(config.sys, "platform", "darwin")

    # Mounted external volume (simulated via ismount + patched mkdir).
    mounted_vol = Path("/Volumes/MOCK_SSD")
    target = mounted_vol / "RonC-Profile-Storage" / "camoufox_data"
    monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == mounted_vol)
    assert config.require_data_dir_mounted(target) == target

    # Boot volume symlink (/Volumes/Macintosh HD -> /) resolves to mounted "/".
    boot_link = Path("/Volumes/Macintosh HD")
    boot_target = boot_link / "Users" / "demo" / "camoufox_data"
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: Path(p) == Path("/") or real_ismount(p))
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == boot_link)
    orig_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, strict=False: (
            Path("/") if self == boot_link else orig_resolve(self, strict=strict)
        ),
    )
    assert config.require_data_dir_mounted(boot_target) == boot_target

    # Non-macOS hosts bypass the macOS /Volumes check.
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(os.path, "ismount", lambda _p: False)
    assert config.require_data_dir_mounted(target) == target


def test_ignore_dotenv_isolates_tests_and_subprocesses(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(
        "FOXPROFILE_DATA_DIR=/Volumes/FAKE_EXTERNAL/camoufox_data\n", encoding="utf-8"
    )
    code = "from src.core import config; print(config.DATA_DIR)"

    env_base = {k: v for k, v in os.environ.items() if not k.startswith("FOXPROFILE_")}
    env_base["PYTHONPATH"] = str(repo_root)

    loaded = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env_base,
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "/Volumes/FAKE_EXTERNAL/camoufox_data"

    ignored = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**env_base, "FOXPROFILE_IGNORE_DOTENV": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert ignored.stdout.strip() == "camoufox_data"


def test_profile_manager_api_runners_and_cores_fail_closed_on_unmounted_volume(
    tmp_path, monkeypatch, capsys
):
    import errno
    from pathlib import Path
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from chrome_engine import runner as chrome_runner
    from src.api.app import create_app
    from src.core import config
    from src.core.container import Container
    from src.services.browser import cookie_tool, cores, runner
    from src.services.browser.launcher import BrowserLauncher

    profiles_file = tmp_path / "profiles.json"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("Alpha", "", "macos", engine="chrome")
    before_store = profiles_file.read_bytes()

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV,
        "External storage volume '/Volumes/OFFLINE' is not mounted; mount it before accessing profile data",
        "/Volumes/OFFLINE/camoufox_data",
    )

    def fail_mount(_path):
        raise unmounted_err

    monkeypatch.setattr(manager_module, "ensure_data_dir", fail_mount)
    monkeypatch.setattr(manager_module, "require_data_dir_mounted", fail_mount)

    # Startup fails closed.
    with pytest.raises(config.StorageVolumeUnmountedError):
        manager_module.ProfileManager()

    # CRUD/import/export fail closed without mutating profiles.json.
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.add_profile("Beta", "", "macos")
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.update_profile("Alpha", "AlphaRenamed", "", "macos")
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.delete_profile("Alpha")
    ok_exp, msg_exp = pm.export_profile("Alpha", str(tmp_path))
    assert ok_exp is False and "not mounted" in msg_exp
    zpath = archive(tmp_path / "in.zip", name="Imported")
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.import_profile(zpath)
    assert profiles_file.read_bytes() == before_store

    # Metadata-only update still succeeds without touching unmounted DATA_DIR.
    assert pm.update_profile("Alpha", "Alpha", "", "macos", notes="metadata-only")
    assert pm.profiles["Alpha"].notes == "metadata-only"

    # REST API translates StorageVolumeUnmountedError to HTTP 503.
    from src.api.routes import profiles as profiles_route
    from src.services.browser import fingerprint as fp_mod

    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(fp_mod, "require_data_dir_mounted", fail_mount)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    r_create = client.post("/api/v1/profiles", json={"name": "Beta"})
    assert r_create.status_code == 503 and "not mounted" in r_create.json()["detail"]
    r_delete = client.delete("/api/v1/profiles/Alpha")
    assert r_delete.status_code == 503 and "not mounted" in r_delete.json()["detail"]
    r_fp_reset = client.delete("/api/v1/profiles/Alpha/fingerprint")
    assert r_fp_reset.status_code == 503 and "not mounted" in r_fp_reset.json()["detail"]

    # Both browser runners emit LAUNCH_FAILED with mount reason before core resolution
    # and exit 1 without creating profile dirs.
    monkeypatch.setattr(runner, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(runner, "require_data_dir_mounted", fail_mount)
    monkeypatch.setattr(chrome_runner, "require_data_dir_mounted", fail_mount)
    assert (
        asyncio.run(runner.run_browser("Alpha", "", "macos", core_version="158.0.1-beta.41")) == 1
    )
    out_cam = capsys.readouterr().out
    assert "LAUNCH_FAILED:" in out_cam and "not mounted" in out_cam
    assert (
        asyncio.run(chrome_runner.run_browser("Alpha", "", "macos", core_version="148.0.7778.215"))
        == 1
    )
    out_chr = capsys.readouterr().out
    assert "LAUNCH_FAILED:" in out_chr and "not mounted" in out_chr

    # Rejected Chrome core launch must not create a missing DATA_DIR on disk.
    uncreated_dir = tmp_path / "never_created_data"
    monkeypatch.setattr(chrome_runner, "DATA_DIR", str(uncreated_dir))
    monkeypatch.setattr(chrome_runner, "require_data_dir_mounted", config.require_data_dir_mounted)
    assert (
        asyncio.run(chrome_runner.run_browser("Alpha", "", "macos", core_version="unapproved")) == 1
    )
    assert not uncreated_dir.exists()
    capsys.readouterr()

    # Cookie tool fails closed before opening browser.
    monkeypatch.setattr(cookie_tool, "require_data_dir_mounted", fail_mount)
    with pytest.raises(config.StorageVolumeUnmountedError):
        asyncio.run(cookie_tool._run("export", "Alpha", "macos", [], "chrome"))

    # Core backup_and_apply fails closed with CoreError instead of backing up an empty dir.
    monkeypatch.setattr(config, "require_data_dir_mounted", fail_mount)
    monkeypatch.setattr(cores, "bound_executable", Mock(return_value=Path("/fake/chrome")))
    monkeypatch.setattr(
        cores,
        "inventory",
        Mock(return_value={"cores": [{"engine": "chrome", "selected_version": "148.0.0.0"}]}),
    )
    with pytest.raises(cores.CoreError, match="not mounted"):
        cores.backup_and_apply(
            pm,
            BrowserLauncher(),
            "Alpha",
            "chrome",
            "148.0.7778.215",
            confirm_backup=True,
            confirm_apply=True,
        )


def test_sibling_camoufox_data_under_external_storage_root_is_separate_from_cores(
    tmp_path, monkeypatch
):
    from src.core import config
    from src.services.browser import cores

    ext_root = tmp_path / "RonC-Profile-Storage"
    data_dir = ext_root / "camoufox_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(ext_root / "core-staging"))
    monkeypatch.setenv("FOXPROFILE_CORE_BACKUP_HOME", str(ext_root / "core-backups"))

    data_resolved = data_dir.resolve()
    stage_resolved = cores.staging_home().resolve()
    backup_resolved = cores.backup_home().resolve()
    for other in (stage_resolved, backup_resolved):
        assert not other.is_relative_to(data_resolved)
        assert not data_resolved.is_relative_to(other)


def test_require_data_dir_mounted_catches_symlink_dotdot_relative_and_loops(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from src.core import config

    monkeypatch.setattr(config.sys, "platform", "darwin")

    # Symlink + '..' targeting an unmounted external volume (absolute and relative).
    alias = tmp_path / "link"
    alias.symlink_to("/Volumes/UNMOUNTED_ALIAS_VOL/inside")
    abs_dotdot = os.path.join(str(tmp_path), "link", "..", "data")
    with pytest.raises(config.StorageVolumeUnmountedError, match="UNMOUNTED_ALIAS_VOL"):
        config.require_data_dir_mounted(abs_dotdot)

    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.StorageVolumeUnmountedError, match="UNMOUNTED_ALIAS_VOL"):
        config.require_data_dir_mounted(os.path.join("link", "..", "data"))

    # Broken direct symlink into unmounted /Volumes/* also fails closed.
    with pytest.raises(config.StorageVolumeUnmountedError, match="UNMOUNTED_ALIAS_VOL"):
        config.require_data_dir_mounted(alias)

    # Symlink loop fails closed with StorageVolumeUnmountedError instead of bypassing.
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    with pytest.raises(config.StorageVolumeUnmountedError, match="Cannot safely resolve"):
        config.require_data_dir_mounted(loop_a / "data")

    # Local symlink + '..' inside tmp_path (not /Volumes/*) remains allowed.
    local_sub = tmp_path / "local_root" / "inside"
    local_sub.mkdir(parents=True)
    local_link = tmp_path / "local_link"
    local_link.symlink_to(local_sub)
    local_alias = os.path.join(str(tmp_path), "local_link", "..", "data")
    assert config.require_data_dir_mounted(local_alias) == Path(local_alias)

    # Delete through an unmounted symlink/.. alias must refuse and keep profiles.json intact.
    profiles_file = tmp_path / "profiles.json"
    real_data = tmp_path / "real_data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(real_data))
    pm = manager_module.ProfileManager()
    assert pm.add_profile("AliasProf", "", "macos")
    before = profiles_file.read_bytes()

    monkeypatch.setattr(manager_module, "DATA_DIR", abs_dotdot)
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.delete_profile("AliasProf")
    assert profiles_file.read_bytes() == before
    assert "AliasProf" in pm.profiles


def test_mid_operation_disconnect_hooks_delete_export_and_core_backup(tmp_path, monkeypatch):
    import errno
    import os
    from pathlib import Path
    from unittest.mock import Mock

    from src.core import config
    from src.services.browser import cores
    from src.services.browser.launcher import BrowserLauncher
    from src.services.profile import transfer

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(config, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("DelA", "", "macos", engine="chrome")
    (data_root / "DelA" / "marker.txt").write_text("keep-me", encoding="utf-8")

    # 1a. Delete rollback when save_profiles fails on mounted storage restores <name> atomically.
    orig_save = pm.save_profiles
    before_store = profiles_file.read_bytes()

    def fail_save_disk_full(*, strict=False):
        raise OSError("metadata disk full")

    monkeypatch.setattr(pm, "save_profiles", fail_save_disk_full)
    with pytest.raises(OSError, match="metadata disk full"):
        pm.delete_profile("DelA")
    assert profiles_file.read_bytes() == before_store
    assert (data_root / "DelA" / "marker.txt").read_text(encoding="utf-8") == "keep-me"
    assert not list(data_root.glob(".deleting_*"))
    monkeypatch.setattr(pm, "save_profiles", orig_save)

    # 1b. Pre-save disconnect after staging rename returns HTTP 503 with recoverable
    # '.deleting_' path and keeps profiles.json intact byte-for-byte.
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV,
        "External storage volume '/Volumes/DETACHED' is not mounted",
        str(data_root),
    )
    state = {"offline": False}

    def dynamic_mount(path):
        if state["offline"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(transfer, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    assert pm.add_profile("PreSaveDel", "", "macos", engine="chrome")
    (data_root / "PreSaveDel" / "marker.txt").write_text("keep-presave", encoding="utf-8")
    before_presave = profiles_file.read_bytes()
    orig_rename = Path.rename

    def rename_then_disconnect(self, target):
        if self.name == "PreSaveDel" and target.name == "payload":
            res = orig_rename(self, target)
            state["offline"] = True
            return res
        if self.name == "payload" and state["offline"]:
            raise OSError(errno.ENXIO, "Device not configured")
        return orig_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename_then_disconnect)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    r_presave = client.delete("/api/v1/profiles/PreSaveDel")
    assert r_presave.status_code == 503 and ".deleting_" in r_presave.json()["detail"]
    assert profiles_file.read_bytes() == before_presave
    assert "PreSaveDel" in pm.profiles
    state["offline"] = False
    monkeypatch.setattr(Path, "rename", orig_rename)

    # 1c. Delete detach right inside save_profiles stages away <name> and fails loudly
    # with explicit recoverable staging path instead of returning True silently.
    def detach_during_save(*, strict=False):
        state["offline"] = True
        return orig_save(strict=strict)

    monkeypatch.setattr(pm, "save_profiles", detach_during_save)
    with pytest.raises(config.StorageVolumeUnmountedError, match=r"\.deleting_"):
        pm.delete_profile("DelA")
    assert not (data_root / "DelA").exists()
    state["offline"] = False
    staged = config.find_pending_deletes(data_root, "DelA", known_profiles=pm.profiles.keys())
    assert (
        len(staged) == 1
        and (staged[0] / "payload" / "marker.txt").read_text(encoding="utf-8") == "keep-me"
    )
    monkeypatch.setattr(pm, "save_profiles", orig_save)

    # 1d. Mid-rmtree disconnect raises StorageVolumeUnmountedError (HTTP 503), not plain OSError.
    assert pm.add_profile("MidRmtree", "", "macos", engine="chrome")
    (data_root / "MidRmtree" / "marker.txt").write_text("mid", encoding="utf-8")

    def rmtree_disconnect(path, *_args, **_kwargs):
        state["offline"] = True
        raise OSError(errno.EIO, "Input/output error")

    orig_rmtree = manager_module.shutil.rmtree
    monkeypatch.setattr(manager_module.shutil, "rmtree", rmtree_disconnect)
    r_mid = client.delete("/api/v1/profiles/MidRmtree")
    assert r_mid.status_code == 503 and ".deleting_" in r_mid.json()["detail"]
    state["offline"] = False
    monkeypatch.setattr(manager_module.shutil, "rmtree", orig_rmtree)

    # 1e. Deleting a symlinked profile dir unlinks the symlink cleanly without touching target.
    ext_target = tmp_path / "symlink_target"
    ext_target.mkdir()
    (ext_target / "untouched.txt").write_text("safe", encoding="utf-8")
    assert pm.add_profile("SymProf", "", "macos", engine="chrome")
    orig_rmtree(data_root / "SymProf")
    (data_root / "SymProf").symlink_to(ext_target)
    assert pm.delete_profile("SymProf") is True
    assert not (data_root / "SymProf").exists() and not (data_root / "SymProf").is_symlink()
    assert (ext_target / "untouched.txt").read_text(encoding="utf-8") == "safe"

    # 2. Export: disconnect during os.walk AND disconnect right before publish leave 0 ZIPs.
    assert pm.add_profile("ExpA", "", "macos", engine="chrome")
    (data_root / "ExpA" / "session.db").write_text("cookies", encoding="utf-8")
    out_dir = tmp_path / "exports"
    out_dir.mkdir()

    real_walk = os.walk

    def walk_disconnect(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None:
            onerror(OSError(errno.EIO, "Input/output error"))
        return iter(())

    monkeypatch.setattr(os, "walk", walk_disconnect)
    ok_walk, msg_walk = transfer.export_to_zip(
        pm.profiles["ExpA"], str(data_root / "ExpA"), str(out_dir), include_data=True
    )
    assert ok_walk is False and "Input/output error" in msg_walk
    assert list(out_dir.iterdir()) == []
    monkeypatch.setattr(os, "walk", real_walk)

    # Disconnect after walk finishes, right before _publish_zip.
    mount_calls = {"n": 0}

    def disconnect_before_publish(path):
        mount_calls["n"] += 1
        if mount_calls["n"] >= 2:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(transfer, "require_data_dir_mounted", disconnect_before_publish)
    ok_pub, msg_pub = transfer.export_to_zip(
        pm.profiles["ExpA"], str(data_root / "ExpA"), str(out_dir), include_data=True
    )
    assert ok_pub is False and "not mounted" in msg_pub
    assert list(out_dir.iterdir()) == []
    monkeypatch.setattr(transfer, "require_data_dir_mounted", dynamic_mount)

    # 3. Core backup_and_apply: disconnect right before first snapshot() publishes 0 ZIPs
    # and keeps core_version unchanged.
    backup_root = tmp_path / "core_backups"
    backup_root.mkdir(mode=0o700)
    monkeypatch.setenv("FOXPROFILE_CORE_BACKUP_HOME", str(backup_root))
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "core_staging"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "chrome_cache"))
    fake_bin = tmp_path / "chrome_bin"
    fake_bin.write_bytes(b"bin")
    monkeypatch.setattr(cores, "bound_executable", Mock(return_value=fake_bin))
    monkeypatch.setattr(
        cores,
        "inventory",
        Mock(return_value={"cores": [{"engine": "chrome", "selected_version": "148.0.0.0"}]}),
    )
    from chrome_engine import fetch

    monkeypatch.setattr(fetch, "installed_executable", lambda: fake_bin)

    core_mount_calls = {"n": 0}

    def disconnect_at_snapshot(path):
        core_mount_calls["n"] += 1
        if core_mount_calls["n"] >= 2:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(config, "require_data_dir_mounted", disconnect_at_snapshot)
    with pytest.raises(cores.CoreError, match="not mounted"):
        cores.backup_and_apply(
            pm,
            BrowserLauncher(),
            "ExpA",
            "chrome",
            "148.0.7778.215",
            confirm_backup=True,
            confirm_apply=True,
        )
    assert pm.profiles["ExpA"].core_version is None
    assert list(backup_root.rglob("*.zip")) == []


def test_offline_reads_return_503_and_distinct_panel_and_desktop_status(tmp_path, monkeypatch):
    import errno
    from pathlib import Path
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container
    from src.core.strings import get_string
    from src.services.browser import fingerprint as fp_mod
    from src.ui import app as ui_app
    from src.ui.actions import cookies as cookie_actions

    profiles_file = tmp_path / "profiles.json"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(ui_app, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(cookie_actions, "DATA_DIR", str(data_dir))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("WithFp", "", "macos", engine="chrome")
    assert pm.add_profile("Fresh", "", "macos", engine="chrome")
    persona = fp_mod.generate_chrome_persona("macos", seed=12)
    fp_mod.save_chrome_persona(str(data_dir / "WithFp"), persona)

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV,
        "External storage volume '/Volumes/OFFLINE_READ' is not mounted",
        str(data_dir),
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(profiles_route, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(ui_app, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(cookie_actions, "require_data_dir_mounted", dynamic_mount)

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    # Mounted: reads succeed; Fresh reports exists=false while WithFp reports exists=true.
    assert client.get("/api/v1/profiles/WithFp/data-dir").json()["exists"] is True
    assert client.get("/api/v1/profiles/WithFp/fingerprint").json()["exists"] is True
    assert client.get("/api/v1/profiles/Fresh/fingerprint").json()["exists"] is False
    panel_on = client.get("/api/v1/profiles?panel=true").json()
    assert panel_on["storage_offline"] is False
    assert panel_on["storage_error"] is None

    # Detached: explicit data/fingerprint GETs return 503; panel keeps full list with
    # storage_offline=True; metadata-only note PATCH still succeeds.
    offline["value"] = True
    r_dir = client.get("/api/v1/profiles/WithFp/data-dir")
    assert r_dir.status_code == 503 and "not mounted" in r_dir.json()["detail"]
    r_fp = client.get("/api/v1/profiles/WithFp/fingerprint")
    assert r_fp.status_code == 503 and "not mounted" in r_fp.json()["detail"]
    panel_off = client.get("/api/v1/profiles?panel=true").json()
    assert panel_off["total"] == 2
    assert panel_off["storage_offline"] is True
    assert "not mounted" in panel_off["storage_error"]
    assert all(p["fingerprint"] is None for p in panel_off["profiles"])

    r_note = client.patch("/api/v1/profiles/WithFp", json={"notes": "saved-offline"})
    assert r_note.status_code == 200 and r_note.json()["notes"] == "saved-offline"

    # Desktop UI callers surface distinct storage-offline status instead of "no fingerprint".
    assert ui_app.App._device_label("WithFp") == get_string("storage_offline_short")
    captured_lines: list[str] = []
    monkeypatch.setattr(
        cookie_actions,
        "open_cookie_dialog",
        lambda _page, _name, fp_line, *_args: captured_lines.append(fp_line),
    )
    bl = Mock(is_running=Mock(return_value=False))
    cookie_actions.manage_cookies(None, "WithFp", None, pm, bl, lambda _m: None)
    assert captured_lines == [get_string("storage_offline")]

    # Remounted: original fingerprint summary restores and Fresh still shows fingerprint_none.
    offline["value"] = False
    assert client.get("/api/v1/profiles/WithFp/fingerprint").json()["exists"] is True
    panel_back = client.get("/api/v1/profiles?panel=true").json()
    assert panel_back["storage_offline"] is False
    by_name = {p["name"]: p for p in panel_back["profiles"]}
    assert by_name["WithFp"]["fingerprint"]["exists"] is True
    assert by_name["Fresh"]["fingerprint"] is None

    captured_lines.clear()
    cookie_actions.manage_cookies(None, "Fresh", None, pm, bl, lambda _m: None)
    assert captured_lines == [get_string("fingerprint_none")]


def test_root_conftest_overrides_ignore_dotenv_zero_and_scrubs_inherited_storage_env(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    synthetic = tmp_path / "proj"
    (synthetic / "tests").mkdir(parents=True)
    (synthetic / "conftest.py").write_text(
        (repo_root / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (synthetic / ".env").write_text(
        "FOXPROFILE_DATA_DIR=/Volumes/FAKE_DOTENV/data\n"
        "FOXPROFILE_PROFILES_FILE=/Volumes/FAKE_DOTENV/profiles.json\n",
        encoding="utf-8",
    )
    (synthetic / "tests" / "test_check_iso.py").write_text(
        "import os, subprocess, sys\n"
        "from src.core import config\n"
        "def test_iso():\n"
        "    assert os.environ.get('FOXPROFILE_IGNORE_DOTENV') == '1'\n"
        "    assert 'FOXPROFILE_DATA_DIR' not in os.environ\n"
        "    assert 'FOXPROFILE_PROFILES_FILE' not in os.environ\n"
        "    assert config.DATA_DIR == 'camoufox_data'\n"
        "    assert config.PROFILES_FILE == 'profiles.json'\n"
        "    child = subprocess.run(\n"
        "        [sys.executable, '-c', 'from src.core import config; print(config.DATA_DIR)'],\n"
        "        capture_output=True, text=True, check=True,\n"
        "    )\n"
        "    assert child.stdout.strip() == 'camoufox_data'\n",
        encoding="utf-8",
    )

    poisoned_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("FOXPROFILE_")},
        "PYTHONPATH": str(repo_root),
        "FOXPROFILE_IGNORE_DOTENV": "0",
        "FOXPROFILE_DATA_DIR": "/Volumes/FAKE_EXPORTED/data",
        "FOXPROFILE_PROFILES_FILE": "/Volumes/FAKE_EXPORTED/profiles.json",
    }
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_check_iso.py"],
        cwd=synthetic,
        env=poisoned_env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # Verify src.server.main() respects FOXPROFILE_IGNORE_DOTENV=1 vs unset.
    srv_probe = (
        "import os, sys, uvicorn, src.server as srv\n"
        "sys.argv = ['server']\n"
        "uvicorn.run = lambda *a, **k: print(os.environ.get('FOXPROFILE_DATA_DIR', 'DEFAULT'))\n"
        "srv.main()\n"
    )
    srv_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("FOXPROFILE_")},
        "PYTHONPATH": str(repo_root),
    }
    res_srv_on = subprocess.run(
        [sys.executable, "-c", srv_probe],
        cwd=synthetic,
        env=srv_env,
        capture_output=True,
        text=True,
    )
    # With .env loaded, FOXPROFILE_DATA_DIR targets an unmounted /Volumes path so
    # startup fails closed inside ProfileManager (or prints the loaded value).
    assert "/Volumes/FAKE_DOTENV/data" in (res_srv_on.stdout + res_srv_on.stderr)

    # With a local mounted path in .env, src.server.main() loads .env when unset
    # and skips .env when FOXPROFILE_IGNORE_DOTENV=1.
    local_dir = synthetic / "custom_local_data"
    (synthetic / ".env").write_text(f"FOXPROFILE_DATA_DIR={local_dir}\n", encoding="utf-8")
    res_srv_loaded = subprocess.run(
        [sys.executable, "-c", srv_probe],
        cwd=synthetic,
        env=srv_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(local_dir) in res_srv_loaded.stdout
    res_srv_off = subprocess.run(
        [sys.executable, "-c", srv_probe],
        cwd=synthetic,
        env={**srv_env, "FOXPROFILE_IGNORE_DOTENV": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DEFAULT" in res_srv_off.stdout

    # Verify BOTH repo collection orders scrub inherited production storage overrides.
    for order in (["chrome_engine/tests", "tests"], ["tests", "chrome_engine/tests"]):
        chk = (
            "import os, sys, pytest\n"
            "class Hook:\n"
            "    def pytest_collection_modifyitems(self, items):\n"
            "        from src.core import config\n"
            "        assert os.environ.get('FOXPROFILE_IGNORE_DOTENV') == '1'\n"
            "        assert 'FOXPROFILE_DATA_DIR' not in os.environ\n"
            "        assert config.DATA_DIR == 'camoufox_data'\n"
            f"rc = pytest.main(['-o', 'pythonpath=.', '--import-mode=importlib', '--collect-only', '-q', {order[0]!r}, {order[1]!r}], plugins=[Hook()])\n"
            "assert rc == 0\n"
        )
        r_ord = subprocess.run(
            [sys.executable, "-c", chk],
            cwd=repo_root,
            env=poisoned_env,
            capture_output=True,
            text=True,
        )
        assert r_ord.returncode == 0, r_ord.stdout + r_ord.stderr


def test_r2_1_pending_staged_delete_fences_ops_and_cleans_on_retry(tmp_path, monkeypatch):
    import asyncio
    import errno
    from pathlib import Path
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from chrome_engine import fetch
    from chrome_engine import runner as chrome_runner
    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container
    from src.services.browser import cookie_tool, cores
    from src.services.browser import fingerprint as fp_mod
    from src.services.browser import runner as camoufox_runner
    from src.services.browser.launcher import BrowserLauncher
    from src.ui.actions import cookies as cookie_actions

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "DATA_DIR", str(data_root))
    monkeypatch.setattr(chrome_runner, "DATA_DIR", str(data_root))
    monkeypatch.setattr(cookie_tool, "DATA_DIR", str(data_root))
    monkeypatch.setattr(cookie_actions, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "PROFILES_FILE", str(profiles_file))

    pm = manager_module.ProfileManager()

    # Exact matching: `.deleting_<32hex>_foo_a` must NOT match profile `a`, and
    # non-hex prefixes must NOT match either.
    (data_root / ".deleting_0123456789abcdef0123456789abcdef_foo_a").mkdir()
    (data_root / ".deleting_nothex_a").mkdir()
    assert config.find_pending_deletes(data_root, "a") == []
    assert len(config.find_pending_deletes(data_root, "foo_a")) == 1

    # Simulate pre-commit disconnect during delete, then remount on same volume.
    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    assert pm.add_profile("StagedProf", "", "macos", engine="chrome")
    (data_root / "StagedProf" / "marker.txt").write_text("keep-marker", encoding="utf-8")
    before_bytes = profiles_file.read_bytes()
    orig_rename = Path.rename

    def detach_after_stage(self, target):
        if self.name == "StagedProf" and target.name == "payload":
            res = orig_rename(self, target)
            offline["value"] = True
            return res
        if self.name == "payload" and offline["value"]:
            raise OSError(errno.ENXIO, "Device not configured")
        return orig_rename(self, target)

    monkeypatch.setattr(Path, "rename", detach_after_stage)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    r_det = client.delete("/api/v1/profiles/StagedProf")
    assert r_det.status_code == 503 and ".deleting_" in r_det.json()["detail"]
    assert profiles_file.read_bytes() == before_bytes
    assert "StagedProf" in pm.profiles

    # Remount same volume: staged dir is intact and original dir is absent.
    offline["value"] = False
    monkeypatch.setattr(Path, "rename", orig_rename)
    staged_list = config.find_pending_deletes(
        data_root, "StagedProf", known_profiles=pm.profiles.keys()
    )
    assert len(staged_list) == 1
    assert (staged_list[0] / "payload" / "marker.txt").read_text(encoding="utf-8") == "keep-marker"

    # Explicit reads return 409 (NOT 200 exists=false).
    r_dir = client.get("/api/v1/profiles/StagedProf/data-dir")
    assert r_dir.status_code == 409 and ".deleting_" in r_dir.json()["detail"]
    r_fp = client.get("/api/v1/profiles/StagedProf/fingerprint")
    assert r_fp.status_code == 409 and ".deleting_" in r_fp.json()["detail"]

    # Export (both include_data=True and False) fails clearly and publishes 0 ZIPs.
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    for inc in (True, False):
        r_exp = client.post(
            "/api/v1/profiles/StagedProf/export",
            json={"export_dir": str(out_dir), "include_data": inc},
        )
        assert r_exp.status_code == 200
        body = r_exp.json()
        assert body["success"] is False and ".deleting_" in body["error"]
    assert list(out_dir.iterdir()) == []

    # Launches (Camoufox & Chrome), cookie_tool, fingerprint.reset, core backup, and rename
    # all fail closed without creating replacement state under data_root/StagedProf.
    lines: list[str] = []
    monkeypatch.setattr(camoufox_runner, "_safe_print", lines.append)
    monkeypatch.setattr(chrome_runner, "_say", lines.append)
    assert (
        asyncio.run(camoufox_runner.run_browser("StagedProf", "http://127.0.0.1:8080", "macos"))
        == 1
    )
    assert asyncio.run(chrome_runner.run_browser("StagedProf", "", "macos")) == 1
    assert any("LAUNCH_FAILED:" in line and ".deleting_" in line for line in lines)
    assert not (data_root / "StagedProf").exists()

    with pytest.raises(config.PendingProfileDeleteError):
        asyncio.run(cookie_tool._run("export", "StagedProf", "macos", [], "chrome"))
    with pytest.raises(config.PendingProfileDeleteError):
        fp_mod.reset(str(data_root / "StagedProf"))
    with pytest.raises(config.PendingProfileDeleteError):
        pm.update_profile("StagedProf", "StagedRenamed", "", "macos")

    # Metadata-only PATCH (notes) still works while fenced.
    r_note = client.patch("/api/v1/profiles/StagedProf", json={"notes": "note-while-fenced"})
    assert r_note.status_code == 200 and r_note.json()["notes"] == "note-while-fenced"

    # Desktop cookie dialog surfaces pending-delete message instead of fingerprint_none.
    captured_lines: list[str] = []
    monkeypatch.setattr(
        cookie_actions,
        "open_cookie_dialog",
        lambda _page, _name, fp_line, *_args: captured_lines.append(fp_line),
    )
    cookie_actions.manage_cookies(
        None, "StagedProf", None, pm, Mock(is_running=Mock(return_value=False)), lambda _m: None
    )
    assert len(captured_lines) == 1 and ".deleting_" in captured_lines[0]

    # Core backup_and_apply fails with CoreError and publishes 0 ZIPs.
    backup_root = tmp_path / "core_backups"
    backup_root.mkdir(mode=0o700)
    monkeypatch.setenv("FOXPROFILE_CORE_BACKUP_HOME", str(backup_root))
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "core_staging"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "chrome_cache"))
    fake_bin = tmp_path / "chrome_bin"
    fake_bin.write_bytes(b"bin")
    monkeypatch.setattr(cores, "bound_executable", Mock(return_value=fake_bin))
    monkeypatch.setattr(
        cores,
        "inventory",
        Mock(return_value={"cores": [{"engine": "chrome", "selected_version": "1.0.0"}]}),
    )
    monkeypatch.setattr(fetch, "installed_executable", lambda: fake_bin)
    with pytest.raises(cores.CoreError, match=r"\.deleting_"):
        cores.backup_and_apply(
            pm,
            BrowserLauncher(),
            "StagedProf",
            "chrome",
            "1.0.1",
            confirm_backup=True,
            confirm_apply=True,
        )
    assert list(backup_root.rglob("*.zip")) == []

    # Retrying DELETE commits row removal AND deletes staged files cleanly (0 orphans).
    r_retry = client.delete("/api/v1/profiles/StagedProf")
    assert r_retry.status_code == 200
    assert "StagedProf" not in pm.profiles
    assert config.find_pending_deletes(data_root, "StagedProf") == []

    # Post-commit stranded staged dir: creating a new profile with the same name must NOT
    # silently restore old data; it fails closed with PendingProfileDeleteError.
    stranded = data_root / ".deleting_fedcba9876543210fedcba9876543210_GoneProf"
    stranded.mkdir()
    (stranded / "old_secret.txt").write_text("old-data", encoding="utf-8")
    with pytest.raises(config.PendingProfileDeleteError):
        pm.add_profile("GoneProf", "", "macos", engine="chrome")
    assert "GoneProf" not in pm.profiles
    assert not (data_root / "GoneProf").exists()
    assert (stranded / "old_secret.txt").read_text(encoding="utf-8") == "old-data"

    # Coexisting live dir + stranded staged dir refuses ambiguous delete with 409.
    assert pm.add_profile("BothProf", "", "macos", engine="chrome")
    (data_root / ".deleting_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_BothProf").mkdir()
    r_both = client.delete("/api/v1/profiles/BothProf")
    assert r_both.status_code == 409 and ".deleting_" in r_both.json()["detail"]
    assert "BothProf" in pm.profiles


def test_r2_2_volume_root_identity_swap_aborts_core_backup_export_and_delete(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from chrome_engine import fetch
    from src.api.app import create_app
    from src.core import config
    from src.core.container import Container
    from src.services.browser import cores
    from src.services.browser.launcher import BrowserLauncher
    from src.services.profile import transfer

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("SwapCore", "", "macos", engine="chrome")
    pm.profiles["SwapCore"].core_version = "1.0.0"
    pm.save_profiles(strict=True)
    (data_root / "SwapCore" / "marker.txt").write_text("volume-a-data", encoding="utf-8")

    # Also create a genuinely empty on-disk profile to verify original storage still succeeds.
    assert pm.add_profile("EmptyOrig", "", "macos", engine="chrome")
    pm.profiles["EmptyOrig"].core_version = "1.0.0"
    pm.save_profiles(strict=True)
    (data_root / "EmptyOrig").rmdir()

    backup_root = tmp_path / "core_backups"
    backup_root.mkdir(mode=0o700)
    monkeypatch.setenv("FOXPROFILE_CORE_BACKUP_HOME", str(backup_root))
    monkeypatch.setenv("FOXPROFILE_CORE_HOME", str(tmp_path / "core_staging"))
    monkeypatch.setenv("FOXPROFILE_CHROME_HOME", str(tmp_path / "chrome_cache"))
    fake_bin = tmp_path / "chrome_bin"
    fake_bin.write_bytes(b"bin")
    monkeypatch.setattr(cores, "bound_executable", Mock(return_value=fake_bin))
    monkeypatch.setattr(
        cores,
        "inventory",
        Mock(return_value={"cores": [{"engine": "chrome", "selected_version": "1.0.0"}]}),
    )
    monkeypatch.setattr(fetch, "installed_executable", lambda: fake_bin)

    # 1. Core backup_and_apply: same pathname swapped to different APFS VolumeUUID right
    # at the first snapshot boundary (even with empty profile dir on volume B) MUST abort
    # without changing core_version or publishing any archive.
    real_identity = config.storage_root_identity
    vol_state = {"uuid": "AAAAAAAA-1111-4111-8111-111111111111", "calls": 0}

    def swapped_identity(path):
        ident = real_identity(path)
        vol_state["calls"] += 1
        uuid_now = (
            "BBBBBBBB-2222-4222-8222-222222222222" if vol_state["calls"] >= 2 else vol_state["uuid"]
        )
        return config.StorageIdentity(
            real_path=ident.real_path,
            volume_uuid=uuid_now,
            fsid=ident.fsid,
            dev=ident.dev,
            ino=ident.ino,
            birthtime=ident.birthtime,
        )

    monkeypatch.setattr(config, "storage_root_identity", swapped_identity)
    before_store = profiles_file.read_bytes()
    with pytest.raises(cores.CoreError, match="changed during operation"):
        cores.backup_and_apply(
            pm,
            BrowserLauncher(),
            "SwapCore",
            "chrome",
            "1.0.1",
            confirm_backup=True,
            confirm_apply=True,
        )
    assert pm.profiles["SwapCore"].core_version == "1.0.0"
    assert profiles_file.read_bytes() == before_store
    assert list(backup_root.rglob("*.zip")) == []

    # 2. Genuinely empty profile on the ORIGINAL storage volume still succeeds.
    monkeypatch.setattr(config, "storage_root_identity", real_identity)
    res_empty = cores.backup_and_apply(
        pm,
        BrowserLauncher(),
        "EmptyOrig",
        "chrome",
        "1.0.1",
        confirm_backup=True,
        confirm_apply=True,
    )
    assert res_empty["applied"] is True and pm.profiles["EmptyOrig"].core_version == "1.0.1"

    # 3. Export: volume UUID swap right before publish fails and leaves 0 ZIPs.
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()
    exp_calls = {"n": 0}

    def swap_before_export_publish(path):
        ident = real_identity(path)
        exp_calls["n"] += 1
        uuid_now = (
            "BBBBBBBB-2222-4222-8222-222222222222"
            if exp_calls["n"] >= 2
            else "AAAAAAAA-1111-4111-8111-111111111111"
        )
        return config.StorageIdentity(
            real_path=ident.real_path,
            volume_uuid=uuid_now,
            fsid=ident.fsid,
            dev=ident.dev,
            ino=ident.ino,
            birthtime=ident.birthtime,
        )

    monkeypatch.setattr(config, "storage_root_identity", swap_before_export_publish)
    monkeypatch.setattr(transfer, "storage_root_identity", swap_before_export_publish)
    ok_exp, err_exp = transfer.export_to_zip(
        pm.profiles["SwapCore"], str(data_root / "SwapCore"), str(exp_dir), include_data=True
    )
    assert ok_exp is False and "changed during operation" in err_exp
    assert exp_calls["n"] == 2
    assert list(exp_dir.iterdir()) == []

    # 4. Delete: volume UUID swap right before save_profiles returns HTTP 503, preserves
    # profiles.json byte-for-byte, and never attempts rollback rename on the swapped volume.
    monkeypatch.setattr(config, "storage_root_identity", real_identity)
    monkeypatch.setattr(transfer, "storage_root_identity", real_identity)
    assert pm.add_profile("SwapDel", "", "macos", engine="chrome")
    (data_root / "SwapDel" / "marker.txt").write_text("keep", encoding="utf-8")
    before_del = profiles_file.read_bytes()
    del_calls = {"n": 0}

    def swap_before_delete_commit(path):
        ident = real_identity(path)
        del_calls["n"] += 1
        # Calls 1-2: PinnedStorageRoot.__enter__, Call 3: pre-stage verify,
        # Call 4+: pre-save_profiles verify & rollback verify.
        uuid_now = (
            "BBBBBBBB-2222-4222-8222-222222222222"
            if del_calls["n"] >= 4
            else "AAAAAAAA-1111-4111-8111-111111111111"
        )
        return config.StorageIdentity(
            real_path=ident.real_path,
            volume_uuid=uuid_now,
            fsid=ident.fsid,
            dev=ident.dev,
            ino=ident.ino,
            birthtime=ident.birthtime,
        )

    monkeypatch.setattr(config, "storage_root_identity", swap_before_delete_commit)
    monkeypatch.setattr(manager_module, "storage_root_identity", swap_before_delete_commit)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    r_swap_del = client.delete("/api/v1/profiles/SwapDel")
    assert r_swap_del.status_code == 503 and ".deleting_" in r_swap_del.json()["detail"]
    assert profiles_file.read_bytes() == before_del
    assert "SwapDel" in pm.profiles
    assert del_calls["n"] >= 5
    assert not (data_root / "SwapDel").exists()
    assert (
        len(config.find_pending_deletes(data_root, "SwapDel", known_profiles=pm.profiles.keys()))
        == 1
    )

    # 5. update_profile Chrome persona rollback MUST NOT write volume A bytes onto a swapped
    # volume B when storage identity changes before save_profiles (Gatekeeper P1-1).
    from src.services.browser import fingerprint as fp_mod

    monkeypatch.setattr(config, "storage_root_identity", real_identity)
    monkeypatch.setattr(manager_module, "storage_root_identity", real_identity)
    assert pm.add_profile("SwapPersona", "", "macos", engine="chrome")
    persona_file = data_root / "SwapPersona" / "chrome_persona.json"
    persona_file.write_text('{"vol":"A"}', encoding="utf-8")
    upd_calls = {"n": 0}

    def swap_before_update_save(path):
        ident = real_identity(path)
        upd_calls["n"] += 1
        uuid_now = (
            "BBBBBBBB-2222-4222-8222-222222222222"
            if upd_calls["n"] >= 4
            else "AAAAAAAA-1111-4111-8111-111111111111"
        )
        return config.StorageIdentity(
            real_path=ident.real_path,
            volume_uuid=uuid_now,
            fsid=ident.fsid,
            dev=ident.dev,
            ino=ident.ino,
            birthtime=ident.birthtime,
        )

    restore_spy = Mock(wraps=manager_module._restore_chrome_persona_file)
    monkeypatch.setattr(manager_module, "_restore_chrome_persona_file", restore_spy)
    monkeypatch.setattr(config, "storage_root_identity", swap_before_update_save)
    monkeypatch.setattr(manager_module, "storage_root_identity", swap_before_update_save)
    valid_persona = fp_mod.generate_chrome_persona("macos", seed=77)
    with pytest.raises(config.StorageVolumeUnmountedError, match="changed during operation"):
        pm.update_profile(
            "SwapPersona",
            "SwapPersona",
            "",
            "macos",
            initial_fingerprint=valid_persona,
        )
    assert restore_spy.call_count == 0

    # 6. find_pending_deletes re-checks mount on FileNotFoundError so a mid-call detach does
    # not silently return [] (Gatekeeper P2-1).
    import errno
    from pathlib import Path

    detach_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    mount_seq = {"n": 0}

    def detach_on_second_check(path):
        mount_seq["n"] += 1
        if mount_seq["n"] >= 2:
            raise detach_err
        return Path(path)

    monkeypatch.setattr(config, "require_data_dir_mounted", detach_on_second_check)
    with pytest.raises(config.StorageVolumeUnmountedError, match="not mounted"):
        config.find_pending_deletes(tmp_path / "vanished_root", "AnyProf")


def test_r3_1_reserved_names_rejected_and_legacy_profile_never_mistaken_for_stage(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))

    # Pre-seed a legacy profiles.json containing BOTH "Alpha" and a legitimate legacy
    # profile whose name matches the old sibling staging pattern for "Alpha".
    legacy_name = ".deleting_0123456789abcdef0123456789abcdef_Alpha"
    profiles_file.write_text(
        json.dumps(
            {
                "Alpha": {"name": "Alpha", "os_type": "macos", "engine": "chrome"},
                legacy_name: {"name": legacy_name, "os_type": "macos", "engine": "chrome"},
                "LegacyOrphan": {"name": "LegacyOrphan", "os_type": "macos", "engine": "chrome"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (data_root / "Alpha").mkdir()
    (data_root / "Alpha" / "a.txt").write_text("alpha-data", encoding="utf-8")
    (data_root / legacy_name).mkdir()
    (data_root / legacy_name / "keep.txt").write_text("legacy-secret", encoding="utf-8")

    # 1. Existing legacy profiles are NOT hidden on load.
    pm = manager_module.ProfileManager()
    assert "Alpha" in pm.profiles and legacy_name in pm.profiles

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    # 2. Reserved names are rejected on create, rename, and import.
    for bad in (
        ".deleting_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_Beta",
        ".import_1234",
        config.STAGING_DIR_NAME,
    ):
        r_create = client.post(
            "/api/v1/profiles", json={"name": bad, "os_type": "macos", "engine": "chrome"}
        )
        assert r_create.status_code == 400
        r_ren = client.patch("/api/v1/profiles/Alpha", json={"name": bad})
        assert r_ren.status_code == 400
        with pytest.raises(ValueError):
            pm.add_profile(bad, "", "macos", engine="chrome")
        zpath = archive(tmp_path / f"imp_{len(bad)}.zip", name=bad)
        ok_imp, _ = pm.import_profile(zpath)
        assert ok_imp is False

    # Editing notes on the existing legacy profile still works (without changing its name).
    r_leg_note = client.patch(f"/api/v1/profiles/{legacy_name}", json={"notes": "kept"})
    assert r_leg_note.status_code == 200 and r_leg_note.json()["notes"] == "kept"

    # 3. Alpha's data read is 200 (never 409) even though legacy_name exists alongside it.
    r_dir = client.get("/api/v1/profiles/Alpha/data-dir")
    assert r_dir.status_code == 200 and r_dir.json()["exists"] is True

    # 4. Even when Alpha's own dir is absent, deleting Alpha MUST keep legacy_name row AND bytes!
    (data_root / "Alpha" / "a.txt").unlink()
    (data_root / "Alpha").rmdir()
    r_del = client.delete("/api/v1/profiles/Alpha")
    assert r_del.status_code == 200
    assert "Alpha" not in pm.profiles
    assert legacy_name in pm.profiles
    assert (data_root / legacy_name / "keep.txt").read_text(encoding="utf-8") == "legacy-secret"

    # 5. Unclaimed legacy sibling is fenced as manual recovery only — NEVER adopted on DELETE.
    orphan_stage = data_root / ".deleting_fedcba9876543210fedcba9876543210_LegacyOrphan"
    orphan_stage.mkdir()
    (orphan_stage / "manual.txt").write_text("do-not-delete", encoding="utf-8")
    assert client.get("/api/v1/profiles/LegacyOrphan/data-dir").status_code == 409
    r_del_leg = client.delete("/api/v1/profiles/LegacyOrphan")
    assert r_del_leg.status_code == 409
    assert "LegacyOrphan" in pm.profiles
    assert (orphan_stage / "manual.txt").read_text(encoding="utf-8") == "do-not-delete"


def test_r3_2_pinned_dir_fd_prevents_any_write_on_swapped_root(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core import config
    from src.core.container import Container

    vol_a = tmp_path / "vol_a_data"
    vol_b = tmp_path / "vol_b_data"
    vol_a.mkdir()
    vol_b.mkdir()
    mount_link = tmp_path / "mounted_data"
    mount_link.symlink_to(vol_a)
    profiles_file = tmp_path / "profiles.json"

    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(mount_link))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("Target", "", "macos", engine="chrome")
    (vol_a / "Target" / "marker.txt").write_text("vol-a-bytes", encoding="utf-8")
    # Prepare replacement disk B with same profile directory name and distinct marker bytes.
    (vol_b / "Target").mkdir()
    (vol_b / "Target" / "marker.txt").write_text("vol-b-bytes", encoding="utf-8")

    # 1. Delete: swap symlink A -> B immediately after pending scan.
    #    MUST return 503, keep profiles.json identical, and perform ZERO writes on vol_b!
    orig_scan = manager_module.find_pending_delete_entries

    def swap_right_after_scan(*args, **kwargs):
        res = orig_scan(*args, **kwargs)
        mount_link.unlink()
        mount_link.symlink_to(vol_b)
        return res

    before_store = profiles_file.read_bytes()
    monkeypatch.setattr(manager_module, "find_pending_delete_entries", swap_right_after_scan)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    r_del = client.delete("/api/v1/profiles/Target")
    assert r_del.status_code == 503
    assert profiles_file.read_bytes() == before_store
    assert "Target" in pm.profiles
    # Zero writes on replacement volume B:
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert (vol_b / "Target" / "marker.txt").read_text(encoding="utf-8") == "vol-b-bytes"
    # Original volume A untouched as well:
    assert (vol_a / "Target" / "marker.txt").read_text(encoding="utf-8") == "vol-a-bytes"
    monkeypatch.setattr(manager_module, "find_pending_delete_entries", orig_scan)

    # 2. Rename (update_profile): swap symlink A -> B right after pending check -> 503 & 0 writes on B!
    mount_link.unlink()
    mount_link.symlink_to(vol_a)
    orig_req_no_pending = manager_module.require_no_pending_delete

    def swap_after_rename_check(*args, **kwargs):
        res = orig_req_no_pending(*args, **kwargs)
        if mount_link.resolve() == vol_a.resolve():
            mount_link.unlink()
            mount_link.symlink_to(vol_b)
        return res

    monkeypatch.setattr(manager_module, "require_no_pending_delete", swap_after_rename_check)
    r_ren = client.patch("/api/v1/profiles/Target", json={"name": "TargetRenamed"})
    assert r_ren.status_code == 503
    assert profiles_file.read_bytes() == before_store
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert (vol_b / "Target" / "marker.txt").read_text(encoding="utf-8") == "vol-b-bytes"
    monkeypatch.setattr(manager_module, "require_no_pending_delete", orig_req_no_pending)

    # 3. Import (overwrite): swap symlink A -> B right after pending check -> 503 & 0 writes on B!
    mount_link.unlink()
    mount_link.symlink_to(vol_a)
    zpath = archive(tmp_path / "imp_target.zip", name="Target", engine="chrome")
    monkeypatch.setattr(manager_module, "require_no_pending_delete", swap_after_rename_check)
    with pytest.raises(config.StorageVolumeUnmountedError):
        pm.import_profile(zpath, overwrite=True)
    assert profiles_file.read_bytes() == before_store
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert (vol_b / "Target" / "marker.txt").read_text(encoding="utf-8") == "vol-b-bytes"


def test_r3_3_panel_and_desktop_distinguish_pending_delete_from_fresh(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container
    from src.core.strings import get_string
    from src.services.browser import fingerprint as fp_mod
    from src.ui import app as ui_app

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))
    monkeypatch.setattr(ui_app, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("PendingProf", "", "macos", engine="chrome")
    assert pm.add_profile("FreshProf", "", "macos", engine="chrome")
    assert pm.add_profile("WithFpProf", "", "macos", engine="chrome")
    fp_mod.save_chrome_persona(
        str(data_root / "PendingProf"), fp_mod.generate_chrome_persona("macos", seed=11)
    )
    fp_mod.save_chrome_persona(
        str(data_root / "WithFpProf"), fp_mod.generate_chrome_persona("macos", seed=22)
    )

    # Stage PendingProf into the verified internal staging namespace (simulating pre-commit detach).
    with config.PinnedStorageRoot(data_root) as pinned:
        staged = pinned.stage_profile_for_delete("PendingProf")

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    panel = client.get("/api/v1/profiles?panel=true").json()
    assert panel["storage_offline"] is False
    by_name = {row["name"]: row for row in panel["profiles"]}
    assert by_name["PendingProf"]["pending_delete"] is True
    assert ".deleting_" in (by_name["PendingProf"]["pending_delete_error"] or "")
    assert by_name["PendingProf"]["fingerprint"] is None
    assert by_name["FreshProf"]["pending_delete"] is False
    assert by_name["FreshProf"]["fingerprint"] is None
    assert by_name["WithFpProf"]["pending_delete"] is False
    assert by_name["WithFpProf"]["fingerprint"]["exists"] is True

    # Notes PATCH still succeeds while pending recovery.
    r_note = client.patch("/api/v1/profiles/PendingProf", json={"notes": "note-ok"})
    assert r_note.status_code == 200 and r_note.json()["notes"] == "note-ok"

    # Desktop row label distinguishes pending_delete_short from fresh "".
    assert ui_app.App._device_label("PendingProf") == get_string("pending_delete_short")
    assert ui_app.App._device_label("FreshProf") == ""

    # Restoring staged payload transitions panel back to healthy fingerprint state.
    with config.PinnedStorageRoot(data_root) as pinned:
        pinned.restore_staged_delete(staged, "PendingProf")
    panel_after = client.get("/api/v1/profiles?panel=true").json()
    by_name_after = {row["name"]: row for row in panel_after["profiles"]}
    assert by_name_after["PendingProf"]["pending_delete"] is False
    assert by_name_after["PendingProf"]["fingerprint"]["exists"] is True


def test_r4_1_dir_fd_writers_zero_writes_on_swapped_disk_for_persona_import_reset(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core import config
    from src.core.container import Container
    from src.services.browser import fingerprint as fp_mod
    from src.services.profile import transfer

    vol_a = tmp_path / "vol_a_data"
    vol_b = tmp_path / "vol_b_data"
    vol_a.mkdir()
    vol_b.mkdir()
    mount_link = tmp_path / "mounted_data"
    mount_link.symlink_to(vol_a)
    profiles_file = tmp_path / "profiles.json"

    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(mount_link))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("Target", "", "macos", engine="chrome")
    persona_a = fp_mod.generate_chrome_persona("macos", seed=10)
    fp_mod.save_chrome_persona(str(vol_a / "Target"), persona_a)
    orig_a_bytes = (vol_a / "Target" / fp_mod.CHROME_PERSONA_FILE).read_bytes()

    # Pre-seed replacement volume B with a distinct existing persona.
    (vol_b / "Target").mkdir()
    b_persona_bytes = b'{"vol":"B","must_stay_untouched":true}'
    (vol_b / "Target" / fp_mod.CHROME_PERSONA_FILE).write_bytes(b_persona_bytes)

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    before_store = profiles_file.read_bytes()

    # 1. PATCH Chrome persona edit: swap mount A -> B RIGHT AT _write_chrome_persona entry,
    #    then run the UNMODIFIED writer -> HTTP 503, store identical, ZERO writes on B!
    orig_write = fp_mod._write_chrome_persona

    def swap_at_persona_writer(*args, **kwargs):
        if mount_link.resolve() == vol_a.resolve():
            mount_link.unlink()
            mount_link.symlink_to(vol_b)
        return orig_write(*args, **kwargs)

    monkeypatch.setattr(fp_mod, "_write_chrome_persona", swap_at_persona_writer)
    new_persona = fp_mod.generate_chrome_persona("macos", seed=99)
    r_patch = client.patch("/api/v1/profiles/Target", json={"initial_fingerprint": new_persona})
    assert r_patch.status_code == 503
    assert profiles_file.read_bytes() == before_store
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert sorted(p.name for p in (vol_b / "Target").iterdir()) == [fp_mod.CHROME_PERSONA_FILE]
    assert (vol_b / "Target" / fp_mod.CHROME_PERSONA_FILE).read_bytes() == b_persona_bytes
    assert (vol_a / "Target" / fp_mod.CHROME_PERSONA_FILE).read_bytes() == orig_a_bytes
    monkeypatch.setattr(fp_mod, "_write_chrome_persona", orig_write)

    # 2. ZIP import extraction: swap mount A -> B RIGHT AT import_from_zip entry,
    #    then run UNMODIFIED extractor -> HTTP 503, store identical, ZERO writes/temps on B!
    mount_link.unlink()
    mount_link.symlink_to(vol_a)
    zpath = archive(
        tmp_path / "imp_swap.zip",
        name="FreshImp",
        engine="chrome",
        files={"data/fixture.txt": "should-never-touch-b"},
    )
    orig_imp = transfer.import_from_zip

    def swap_at_zip_extract(*args, **kwargs):
        if mount_link.resolve() == vol_a.resolve():
            mount_link.unlink()
            mount_link.symlink_to(vol_b)
        return orig_imp(*args, **kwargs)

    monkeypatch.setattr(manager_module, "import_from_zip", swap_at_zip_extract)
    monkeypatch.setattr(manager_module, "_ORIG_IMPORT_FROM_ZIP", swap_at_zip_extract)
    r_imp = client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": False})
    assert r_imp.status_code == 503
    assert profiles_file.read_bytes() == before_store
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert (vol_b / "Target" / fp_mod.CHROME_PERSONA_FILE).read_bytes() == b_persona_bytes
    monkeypatch.setattr(manager_module, "import_from_zip", orig_imp)
    monkeypatch.setattr(manager_module, "_ORIG_IMPORT_FROM_ZIP", orig_imp)

    # 3. Direct dir_fd copy_child_tree & fingerprint reset fail closed on swapped root with 0 writes on B.
    mount_link.unlink()
    mount_link.symlink_to(vol_a)
    with config.PinnedStorageRoot(mount_link) as pinned:
        mount_link.unlink()
        mount_link.symlink_to(vol_b)
        with pytest.raises(config.StorageVolumeUnmountedError):
            pinned.copy_child_tree("Target", (".import_test", "Target"))
        with pytest.raises(config.StorageVolumeUnmountedError):
            fp_mod.reset(str(mount_link / "Target"), pinned=pinned, rel_dir=("Target",))
    assert sorted(p.name for p in vol_b.iterdir()) == ["Target"]
    assert (vol_b / "Target" / fp_mod.CHROME_PERSONA_FILE).read_bytes() == b_persona_bytes


def test_r4_2_interrupted_rename_fences_both_names_until_restored(tmp_path, monkeypatch):
    import asyncio
    import errno
    from pathlib import Path

    from fastapi.testclient import TestClient

    from chrome_engine import runner as chrome_runner
    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container
    from src.services.browser import runner as camoufox_runner

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(chrome_runner, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("OldName", "", "macos", engine="chrome")
    (data_root / "OldName" / "marker.txt").write_text("rename-marker-bytes", encoding="utf-8")
    before_store = profiles_file.read_bytes()

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    orig_rename_child = config.PinnedStorageRoot.rename_child

    def detach_right_after_rename(self, src_name, dst_name):
        orig_rename_child(self, src_name, dst_name)
        if src_name == "OldName" and dst_name == "NewName":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", detach_right_after_rename)
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")

    # Interrupted rename: PATCH returns 503 and preserves profiles.json byte-for-byte.
    r_ren = client.patch("/api/v1/profiles/OldName", json={"name": "NewName"})
    assert r_ren.status_code == 503
    assert profiles_file.read_bytes() == before_store
    assert "OldName" in pm.profiles and "NewName" not in pm.profiles

    # Remount same volume: data sits under NewName with original marker intact.
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", orig_rename_child)
    assert not (data_root / "OldName").exists()
    assert (data_root / "NewName" / "marker.txt").read_text(
        encoding="utf-8"
    ) == "rename-marker-bytes"

    # Explicit reads & export refuse with recovery status (NEVER 200/exists=false or metadata-only ZIP!).
    r_dir = client.get("/api/v1/profiles/OldName/data-dir")
    assert r_dir.status_code == 409 and "interrupted rename" in r_dir.json()["detail"]
    r_fp = client.get("/api/v1/profiles/OldName/fingerprint")
    assert r_fp.status_code == 409 and "interrupted rename" in r_fp.json()["detail"]

    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()
    for inc in (True, False):
        r_exp = client.post(
            "/api/v1/profiles/OldName/export",
            json={"export_dir": str(exp_dir), "include_data": inc},
        )
        assert r_exp.status_code == 200
        assert r_exp.json()["success"] is False
        assert "interrupted rename" in r_exp.json()["error"]
    assert list(exp_dir.iterdir()) == []

    # Both affected names are fenced: creating NewName returns 409, deleting OldName returns 409,
    # launching OldName fails without creating replacement dir.
    r_new = client.post(
        "/api/v1/profiles", json={"name": "NewName", "os_type": "macos", "engine": "chrome"}
    )
    assert r_new.status_code == 409 and "interrupted rename" in r_new.json()["detail"]
    r_del = client.delete("/api/v1/profiles/OldName")
    assert r_del.status_code == 409 and "interrupted rename" in r_del.json()["detail"]

    msgs: list[str] = []
    monkeypatch.setattr(camoufox_runner, "_safe_print", msgs.append)
    monkeypatch.setattr(chrome_runner, "_say", msgs.append)
    assert (
        asyncio.run(camoufox_runner.run_browser("OldName", "http://127.0.0.1:8080", "macos")) == 1
    )
    assert asyncio.run(chrome_runner.run_browser("OldName", "", "macos")) == 1
    assert not (data_root / "OldName").exists()

    # Panel reports pending_delete=True for OldName; metadata-only notes PATCH stays usable!
    panel = client.get("/api/v1/profiles?panel=true").json()
    by_name = {row["name"]: row for row in panel["profiles"]}
    assert by_name["OldName"]["pending_delete"] is True
    assert "interrupted rename" in (by_name["OldName"]["pending_delete_error"] or "")
    r_note = client.patch("/api/v1/profiles/OldName", json={"notes": "usable-while-fenced"})
    assert r_note.status_code == 200 and r_note.json()["notes"] == "usable-while-fenced"

    # Restoring NewName -> OldName resolves the fence cleanly and export includes the original marker!
    (data_root / "NewName").rename(data_root / "OldName")
    r_dir_ok = client.get("/api/v1/profiles/OldName/data-dir")
    assert r_dir_ok.status_code == 200 and r_dir_ok.json()["exists"] is True
    r_exp_ok = client.post(
        "/api/v1/profiles/OldName/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_exp_ok.json()["success"] is True
    with zipfile.ZipFile(r_exp_ok.json()["zip_path"]) as zf:
        assert zf.read("data/marker.txt").decode("utf-8") == "rename-marker-bytes"


def test_r5_1_interrupted_overwrite_import_fences_both_detach_points_until_restored(
    tmp_path, monkeypatch
):
    import asyncio
    import errno
    from pathlib import Path

    from fastapi.testclient import TestClient

    from chrome_engine import runner as chrome_runner
    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container
    from src.services.browser import runner as camoufox_runner

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "DATA_DIR", str(data_root))
    monkeypatch.setattr(camoufox_runner, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(chrome_runner, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("OverProf", "", "macos", engine="camoufox")
    (data_root / "OverProf" / "marker.txt").write_text("orig-session", encoding="utf-8")
    before_store = profiles_file.read_bytes()

    zpath = archive(
        tmp_path / "incoming.zip",
        name="OverProf",
        engine="chrome",
        files={"data/marker.txt": "incoming-zip"},
    )
    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()
    msgs: list[str] = []
    monkeypatch.setattr(camoufox_runner, "_safe_print", msgs.append)
    monkeypatch.setattr(chrome_runner, "_say", msgs.append)

    def assert_fenced_and_preserved(expect_published_incoming: bool):
        assert profiles_file.read_bytes() == before_store
        assert pm.profiles["OverProf"].engine == "camoufox"
        prev_markers = list(data_root.glob(".import_*/previous/marker.txt"))
        assert len(prev_markers) == 1 and prev_markers[0].read_text() == "orig-session"
        if expect_published_incoming:
            assert (data_root / "OverProf" / "marker.txt").read_text() == "incoming-zip"
        else:
            assert not (data_root / "OverProf").exists()
            inc_markers = list(data_root.glob(".import_*/incoming/OverProf/marker.txt"))
            assert len(inc_markers) == 1 and inc_markers[0].read_text() == "incoming-zip"

        r_dir = client.get("/api/v1/profiles/OverProf/data-dir")
        assert r_dir.status_code == 409 and "interrupted import" in r_dir.json()["detail"]
        r_fp = client.get("/api/v1/profiles/OverProf/fingerprint")
        assert r_fp.status_code == 409 and "interrupted import" in r_fp.json()["detail"]

        for inc in (True, False):
            r_exp = client.post(
                "/api/v1/profiles/OverProf/export",
                json={"export_dir": str(exp_dir), "include_data": inc},
            )
            assert r_exp.status_code == 200 and r_exp.json()["success"] is False
            assert "interrupted import" in r_exp.json()["error"]
        assert list(exp_dir.iterdir()) == []

        assert client.delete("/api/v1/profiles/OverProf").status_code == 409
        assert (
            client.post(
                "/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True}
            ).status_code
            == 409
        )
        r_note = client.patch("/api/v1/profiles/OverProf", json={"notes": "ok-while-fenced"})
        assert r_note.status_code == 200
        # Restore baseline store bytes after metadata-only edit check.
        assert client.patch("/api/v1/profiles/OverProf", json={"notes": ""}).status_code == 200

        panel = client.get("/api/v1/profiles?panel=true").json()
        by_name = {row["name"]: row for row in panel["profiles"]}
        assert by_name["OverProf"]["pending_delete"] is True
        assert "interrupted import" in (by_name["OverProf"]["pending_delete_error"] or "")

        assert (
            asyncio.run(camoufox_runner.run_browser("OverProf", "http://127.0.0.1:8080", "macos"))
            == 1
        )
        assert asyncio.run(chrome_runner.run_browser("OverProf", "", "macos")) == 1
        return prev_markers[0].parent

    # --- Detach point 1: right after moving old dir -> .import_*/previous ---
    orig_into = config.PinnedStorageRoot.rename_child_into_subdir

    def detach_after_move_old(self, src_name, subdir_name, dst_name):
        orig_into(self, src_name, subdir_name, dst_name)
        if src_name == "OverProf" and dst_name == "previous":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child_into_subdir", detach_after_move_old)
    r_imp1 = client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True})
    assert r_imp1.status_code == 503
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child_into_subdir", orig_into)

    prev_dir1 = assert_fenced_and_preserved(expect_published_incoming=False)
    # Restore original dir -> fence clears and export recovers original session.
    prev_dir1.rename(data_root / "OverProf")
    assert client.get("/api/v1/profiles/OverProf/data-dir").json()["exists"] is True
    r_ok1 = client.post(
        "/api/v1/profiles/OverProf/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_ok1.json()["success"] is True
    with zipfile.ZipFile(r_ok1.json()["zip_path"]) as zf:
        assert zf.read("data/marker.txt").decode("utf-8") == "orig-session"
    Path(r_ok1.json()["zip_path"]).unlink()

    # --- Detach point 2: right after publishing incoming -> OverProf ---
    before_store = profiles_file.read_bytes()
    orig_from = config.PinnedStorageRoot.rename_from_subdir

    def detach_after_publish(self, subdir_parts, src_name, dst_name):
        orig_from(self, subdir_parts, src_name, dst_name)
        if dst_name == "OverProf" and len(subdir_parts) == 2 and subdir_parts[1] == "incoming":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", detach_after_publish)
    r_imp2 = client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True})
    assert r_imp2.status_code == 503
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", orig_from)

    prev_dir2 = assert_fenced_and_preserved(expect_published_incoming=True)
    # Partial recovery (moving incoming away without restoring previous yet) stays fenced.
    preserved_incoming = tmp_path / "saved_incoming"
    (data_root / "OverProf").rename(preserved_incoming)
    assert (preserved_incoming / "marker.txt").read_text() == "incoming-zip"
    assert client.get("/api/v1/profiles/OverProf/data-dir").status_code == 409

    # Restoring previous -> OverProf clears the fence and exports original session.
    prev_dir2.rename(data_root / "OverProf")
    r_dir_ok = client.get("/api/v1/profiles/OverProf/data-dir")
    assert r_dir_ok.status_code == 200 and r_dir_ok.json()["exists"] is True
    r_ok2 = client.post(
        "/api/v1/profiles/OverProf/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_ok2.json()["success"] is True
    with zipfile.ZipFile(r_ok2.json()["zip_path"]) as zf:
        assert zf.read("data/marker.txt").decode("utf-8") == "orig-session"
    Path(r_ok2.json()["zip_path"]).unlink()

    # --- Detach point 3: right after save_profiles returns, before clear_pending_import ---
    before_store = profiles_file.read_bytes()
    orig_save = pm.save_profiles
    tripped = {"done": False}

    def detach_after_save(*args, **kwargs):
        orig_save(*args, **kwargs)
        if not tripped["done"]:
            tripped["done"] = True
            offline["value"] = True

    monkeypatch.setattr(pm, "save_profiles", detach_after_save)
    r_imp3 = client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True})
    assert r_imp3.status_code == 503
    offline["value"] = False
    monkeypatch.setattr(pm, "save_profiles", orig_save)

    # Local profiles.json was reverted cleanly to before_store, and remount stays fenced until restored.
    prev_dir3 = assert_fenced_and_preserved(expect_published_incoming=True)
    preserved_incoming3 = tmp_path / "saved_incoming_3"
    (data_root / "OverProf").rename(preserved_incoming3)
    prev_dir3.rename(data_root / "OverProf")
    r_ok3 = client.post(
        "/api/v1/profiles/OverProf/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_ok3.json()["success"] is True
    with zipfile.ZipFile(r_ok3.json()["zip_path"]) as zf:
        assert zf.read("data/marker.txt").decode("utf-8") == "orig-session"


def test_r5_2_case_only_rename_recovery_and_case_insensitive_control(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("CaseOnly", "", "macos", engine="chrome")
    (data_root / "CaseOnly" / "marker.txt").write_text("case-marker", encoding="utf-8")

    # 1. Journal writer MUST NOT skip case-only moves solely on folded spelling.
    with config.PinnedStorageRoot(data_root) as pinned:
        journal = pinned.record_pending_rename("CaseOnly", "caseonly")
        assert journal is not None
        assert (data_root / config.STAGING_DIR_NAME / journal).is_file()
        assert pinned.record_pending_rename("CaseOnly", "CaseOnly") is None

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()

    # 2. Case-sensitive view: 'CaseOnly' is inaccessible (None), 'caseonly' holds expected inode -> FENCED.
    orig_lstat_ino = config._lstat_ino
    expected_ino = orig_lstat_ino(data_root / "CaseOnly")
    assert expected_ino is not None

    def case_sensitive_lstat(path):
        if path.parent == data_root and path.name == "CaseOnly":
            return None
        if path.parent == data_root and path.name == "caseonly":
            return expected_ino
        return orig_lstat_ino(path)

    monkeypatch.setattr(config, "_lstat_ino", case_sensitive_lstat)
    r_dir = client.get("/api/v1/profiles/CaseOnly/data-dir")
    assert r_dir.status_code == 409 and "interrupted rename" in r_dir.json()["detail"]
    r_fp = client.get("/api/v1/profiles/CaseOnly/fingerprint")
    assert r_fp.status_code == 409 and "interrupted rename" in r_fp.json()["detail"]
    r_exp = client.post(
        "/api/v1/profiles/CaseOnly/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_exp.json()["success"] is False and "interrupted rename" in r_exp.json()["error"]
    assert list(exp_dir.iterdir()) == []
    assert client.delete("/api/v1/profiles/CaseOnly").status_code == 409
    assert client.patch("/api/v1/profiles/CaseOnly", json={"notes": "ok"}).status_code == 200
    panel = client.get("/api/v1/profiles?panel=true").json()
    by_name = {row["name"]: row for row in panel["profiles"]}
    assert by_name["CaseOnly"]["pending_delete"] is True
    assert "interrupted rename" in (by_name["CaseOnly"]["pending_delete_error"] or "")

    # 3. Case-insensitive control: both spellings resolve to expected_ino -> NO false pending status.
    def case_insensitive_lstat(path):
        if path.parent == data_root and path.name in ("CaseOnly", "caseonly"):
            return expected_ino
        return orig_lstat_ino(path)

    monkeypatch.setattr(config, "_lstat_ino", case_insensitive_lstat)
    assert client.get("/api/v1/profiles/CaseOnly/data-dir").status_code == 200
    panel_ci = client.get("/api/v1/profiles?panel=true").json()
    assert {row["name"]: row for row in panel_ci["profiles"]}["CaseOnly"]["pending_delete"] is False

    # 4. Healthy case-only rename commits cleanly and clears journal without false pending status.
    monkeypatch.setattr(config, "_lstat_ino", orig_lstat_ino)
    with config.PinnedStorageRoot(data_root) as pinned:
        pinned.clear_pending_rename(journal)
    r_ren = client.patch("/api/v1/profiles/CaseOnly", json={"name": "caseonly"})
    assert r_ren.status_code == 200 and r_ren.json()["name"] == "caseonly"
    panel_ok = client.get("/api/v1/profiles?panel=true").json()
    by_name_ok = {row["name"]: row for row in panel_ok["profiles"]}
    assert by_name_ok["caseonly"]["pending_delete"] is False
    assert client.get("/api/v1/profiles/caseonly/data-dir").json()["exists"] is True
    r_exp_ok = client.post(
        "/api/v1/profiles/caseonly/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_exp_ok.json()["success"] is True
    with zipfile.ZipFile(r_exp_ok.json()["zip_path"]) as zf:
        assert zf.read("data/marker.txt").decode("utf-8") == "case-marker"


def test_r6_1_failed_metadata_rollback_preserves_both_versions_and_accurate_409(
    tmp_path, monkeypatch
):
    import errno
    from pathlib import Path

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))

    pm = manager_module.ProfileManager()
    assert pm.add_profile("OverProf", "", "macos", engine="camoufox")
    (data_root / "OverProf" / "marker.txt").write_text("orig-session", encoding="utf-8")

    zpath = archive(
        tmp_path / "incoming.zip",
        name="OverProf",
        engine="chrome",
        files={"data/marker.txt": "incoming-zip"},
    )

    container = Container()
    container._instances["pm"] = pm
    client = TestClient(
        create_app(container), base_url="http://127.0.0.1", raise_server_exceptions=False
    )
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()

    # Inject EIO only on journal unlink and ENOSPC only on the rollback store replacement.
    orig_unlink = config.PinnedStorageRoot.unlink_file
    orig_replace = manager_module._replace
    replace_calls = {"count": 0}

    def fail_journal_unlink(self, rel_dir_parts, filename):
        if rel_dir_parts == (config.STAGING_DIR_NAME,) and filename.startswith(".importing_"):
            raise OSError(errno.EIO, "simulated journal unlink I/O error", filename)
        return orig_unlink(self, rel_dir_parts, filename)

    def fail_rollback_replace(src, dst, attempts=10):
        replace_calls["count"] += 1
        if replace_calls["count"] == 2:
            raise OSError(errno.ENOSPC, "simulated rollback store ENOSPC", str(dst))
        return orig_replace(src, dst, attempts)

    monkeypatch.setattr(config.PinnedStorageRoot, "unlink_file", fail_journal_unlink)
    monkeypatch.setattr(manager_module, "_replace", fail_rollback_replace)

    r_imp = client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True})
    assert r_imp.status_code == 500
    assert replace_calls["count"] == 2

    monkeypatch.setattr(config.PinnedStorageRoot, "unlink_file", orig_unlink)
    monkeypatch.setattr(manager_module, "_replace", orig_replace)

    # Both versions survived on disk, and in-memory + persisted store agree on committed Chrome.
    assert (data_root / "OverProf" / "marker.txt").read_text(encoding="utf-8") == "incoming-zip"
    prev_markers = list(data_root.glob(".import_*/previous/marker.txt"))
    assert len(prev_markers) == 1 and prev_markers[0].read_text(encoding="utf-8") == "orig-session"
    prev_dir = prev_markers[0].parent
    assert prev_dir.is_dir()

    store_data = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert store_data["OverProf"]["engine"] == "chrome"
    assert config.is_valid_import_tx(store_data["OverProf"].get("_import_tx"))
    assert pm.profiles["OverProf"].engine == "chrome"

    # Next read returns 409 citing the existing backup and warning not to overwrite OverProf.
    r_dir = client.get("/api/v1/profiles/OverProf/data-dir")
    assert r_dir.status_code == 409
    detail = r_dir.json()["detail"]
    assert "metadata committed" in detail and str(prev_dir) in detail
    assert "do not overwrite" in detail
    assert client.get("/api/v1/profiles/OverProf/fingerprint").status_code == 409
    assert client.delete("/api/v1/profiles/OverProf").status_code == 409
    assert (
        client.post(
            "/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True}
        ).status_code
        == 409
    )
    for inc in (True, False):
        r_exp = client.post(
            "/api/v1/profiles/OverProf/export",
            json={"export_dir": str(exp_dir), "include_data": inc},
        )
        assert r_exp.status_code == 200 and r_exp.json()["success"] is False
    assert list(exp_dir.iterdir()) == []

    # Metadata-only notes edit succeeds and preserves commit transaction evidence.
    r_note = client.patch("/api/v1/profiles/OverProf", json={"notes": "r6-1-note"})
    assert r_note.status_code == 200
    assert (
        json.loads(profiles_file.read_text(encoding="utf-8"))["OverProf"]["_import_tx"]
        == store_data["OverProf"]["_import_tx"]
    )
    assert client.get("/api/v1/profiles/OverProf/data-dir").status_code == 409

    # Following instructions (archive/move previous away while keeping incoming) clears the fence.
    archived_old = tmp_path / "archived_camoufox"
    prev_dir.rename(archived_old)
    assert (archived_old / "marker.txt").read_text(encoding="utf-8") == "orig-session"

    r_ok = client.get("/api/v1/profiles/OverProf/data-dir")
    assert r_ok.status_code == 200 and r_ok.json()["exists"] is True
    r_exp_ok = client.post(
        "/api/v1/profiles/OverProf/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_exp_ok.json()["success"] is True
    with zipfile.ZipFile(r_exp_ok.json()["zip_path"]) as zf:
        exported_meta = json.loads(zf.read("profile.json").decode("utf-8"))
        assert exported_meta["engine"] == "chrome"
        assert exported_meta["notes"] == "r6-1-note"
        assert "_import_tx" not in exported_meta
        assert zf.read("data/marker.txt").decode("utf-8") == "incoming-zip"
    Path(r_exp_ok.json()["zip_path"]).unlink()


def test_r6_2_fresh_process_crash_after_metadata_save_blocks_mismatched_recovery(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()
    zpath = archive(
        tmp_path / "incoming.zip",
        name="OverProf",
        engine="chrome",
        files={"data/marker.txt": "incoming-zip"},
    )
    env = {
        **os.environ,
        "FOXPROFILE_IGNORE_DOTENV": "1",
        "FOXPROFILE_PROFILES_FILE": str(profiles_file),
        "FOXPROFILE_DATA_DIR": str(data_root),
        "PYTHONPATH": str(repo_root),
    }

    # Process 1: create Camoufox profile, then crash hard right after real profiles.json replace.
    crash_script = """
import os, sys
from pathlib import Path
from src.services.profile import manager as manager_module

pm = manager_module.ProfileManager()
assert pm.add_profile("OverProf", "", "macos", engine="camoufox")
(Path(manager_module.DATA_DIR) / "OverProf" / "marker.txt").write_text("orig-session", encoding="utf-8")

orig_replace = manager_module._replace
def crash_right_after_commit(src, dst, attempts=10):
    orig_replace(src, dst, attempts)
    os._exit(99)

manager_module._replace = crash_right_after_commit
pm.import_profile(sys.argv[1], overwrite=True)
"""
    p1 = subprocess.run(
        [sys.executable, "-c", crash_script, zpath],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert p1.returncode == 99, f"Expected hard exit 99, got {p1.returncode}: {p1.stderr}"

    # Verify post-crash disk state before fresh-process restart.
    assert (data_root / "OverProf" / "marker.txt").read_text(encoding="utf-8") == "incoming-zip"
    prev_markers = list(data_root.glob(".import_*/previous/marker.txt"))
    assert len(prev_markers) == 1 and prev_markers[0].read_text(encoding="utf-8") == "orig-session"
    prev_dir = prev_markers[0].parent
    assert json.loads(profiles_file.read_text(encoding="utf-8"))["OverProf"]["engine"] == "chrome"

    # Process 2: fresh API process verifies fence, blocks mismatched swap, and exports matching pair.
    verify_script = """
import json, sys, zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.core.container import Container

data_root = Path(sys.argv[1])
exp_dir = Path(sys.argv[2])
zpath = sys.argv[3]
prev_dir = Path(sys.argv[4])
tmp_work = Path(sys.argv[5])

client = TestClient(create_app(Container()), base_url="http://127.0.0.1")

# 1. Immediately after restart: 409 committed_cleanup citing existing previous/ backup.
r_dir = client.get("/api/v1/profiles/OverProf/data-dir")
assert r_dir.status_code == 409, r_dir.text
detail = r_dir.json()["detail"]
assert "metadata committed" in detail and str(prev_dir) in detail and "do not overwrite" in detail
assert client.get("/api/v1/profiles/OverProf/fingerprint").status_code == 409
assert client.delete("/api/v1/profiles/OverProf").status_code == 409
assert client.post("/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True}).status_code == 409
for inc in (True, False):
    r_exp = client.post("/api/v1/profiles/OverProf/export", json={"export_dir": str(exp_dir), "include_data": inc})
    assert r_exp.status_code == 200 and r_exp.json()["success"] is False
assert list(exp_dir.iterdir()) == []

panel = client.get("/api/v1/profiles?panel=true").json()
row = {r["name"]: r for r in panel["profiles"]}["OverProf"]
assert row["engine"] == "chrome" and row["pending_delete"] is True

# 2. Notes edit stays supported while fenced and preserves commit marker.
r_note = client.patch("/api/v1/profiles/OverProf", json={"notes": "post-restart-note"})
assert r_note.status_code == 200
assert client.get("/api/v1/profiles/OverProf/data-dir").status_code == 409

# 3. Mismatched recovery (moving incoming aside and putting old Camoufox at OverProf) MUST stay fenced!
saved_incoming = tmp_work / "saved_incoming"
(data_root / "OverProf").rename(saved_incoming)
prev_dir.rename(data_root / "OverProf")
assert (data_root / "OverProf" / "marker.txt").read_text(encoding="utf-8") == "orig-session"

r_mismatch = client.get("/api/v1/profiles/OverProf/data-dir")
assert r_mismatch.status_code == 409, f"Mismatched recovery must stay fenced, got {r_mismatch.status_code}"
mismatch_detail = r_mismatch.json()["detail"]
assert "does not match the committed incoming session" in mismatch_detail
assert str(prev_dir) not in mismatch_detail  # Backup was moved; must not cite nonexistent path.
assert client.get("/api/v1/profiles/OverProf/fingerprint").status_code == 409
assert client.delete("/api/v1/profiles/OverProf").status_code == 409
for inc in (True, False):
    r_exp_bad = client.post("/api/v1/profiles/OverProf/export", json={"export_dir": str(exp_dir), "include_data": inc})
    assert r_exp_bad.status_code == 200 and r_exp_bad.json()["success"] is False
assert list(exp_dir.iterdir()) == []

# 4. Valid recovery (archive old Camoufox, restore matching incoming Chrome dir) releases fence.
archived_old = tmp_work / "archived_old"
(data_root / "OverProf").rename(archived_old)
saved_incoming.rename(data_root / "OverProf")

r_ok = client.get("/api/v1/profiles/OverProf/data-dir")
assert r_ok.status_code == 200 and r_ok.json()["exists"] is True
panel_ok = client.get("/api/v1/profiles?panel=true").json()
assert {r["name"]: r for r in panel_ok["profiles"]}["OverProf"]["pending_delete"] is False

r_exp_ok = client.post("/api/v1/profiles/OverProf/export", json={"export_dir": str(exp_dir), "include_data": True})
assert r_exp_ok.status_code == 200 and r_exp_ok.json()["success"] is True
with zipfile.ZipFile(r_exp_ok.json()["zip_path"]) as zf:
    exported_meta = json.loads(zf.read("profile.json").decode("utf-8"))
    assert exported_meta["engine"] == "chrome"
    assert exported_meta["notes"] == "post-restart-note"
    assert "_import_tx" not in exported_meta
    assert zf.read("data/marker.txt").decode("utf-8") == "incoming-zip"
assert (archived_old / "marker.txt").read_text(encoding="utf-8") == "orig-session"
print("FRESH_PROCESS_R6_2_OK")
"""
    p2 = subprocess.run(
        [
            sys.executable,
            "-c",
            verify_script,
            str(data_root),
            str(exp_dir),
            zpath,
            str(prev_dir),
            str(tmp_path),
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert p2.returncode == 0, f"Fresh-process R6-2 verify failed:\n{p2.stdout}\n{p2.stderr}"
    assert "FRESH_PROCESS_R6_2_OK" in p2.stdout


def test_r6_post_recovery_resolved_import_journal_does_not_resurrect_on_delete_or_rename(
    tmp_path, monkeypatch
):
    import errno
    from pathlib import Path

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    def setup_recovered_profile(name: str):
        pm = manager_module.ProfileManager()
        assert pm.add_profile(name, "", "macos", engine="camoufox")
        (data_root / name / "marker.txt").write_text("orig", encoding="utf-8")
        zpath = archive(
            tmp_path / f"{name}_in.zip",
            name=name,
            engine="chrome",
            files={"data/marker.txt": "committed-incoming"},
        )
        orig_unlink = config.PinnedStorageRoot.unlink_file
        orig_replace = manager_module._replace
        calls = {"n": 0}

        def fail_unlink(self, rel_dir_parts, filename):
            if rel_dir_parts == (config.STAGING_DIR_NAME,) and filename.startswith(".importing_"):
                raise OSError(errno.EIO, "sim EIO", filename)
            return orig_unlink(self, rel_dir_parts, filename)

        def fail_second_replace(src, dst, attempts=10):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "sim ENOSPC", str(dst))
            return orig_replace(src, dst, attempts)

        monkeypatch.setattr(config.PinnedStorageRoot, "unlink_file", fail_unlink)
        monkeypatch.setattr(manager_module, "_replace", fail_second_replace)
        with pytest.raises(OSError):
            pm.import_profile(zpath, overwrite=True)
        monkeypatch.setattr(config.PinnedStorageRoot, "unlink_file", orig_unlink)
        monkeypatch.setattr(manager_module, "_replace", orig_replace)

        # Operator resolves by archiving previous/, leaving resolved .importing_<tx>.json on disk.
        prevs = list(data_root.glob(".import_*/previous"))
        assert len(prevs) == 1
        prevs[0].rename(tmp_path / f"archived_{name}")
        journals = list((data_root / config.STAGING_DIR_NAME).glob(".importing_*.json"))
        assert len(journals) == 1
        return pm

    # 1. Interrupted DELETE after R6 recovery must report delete (not ghost import) and complete on retry.
    pm_del = setup_recovered_profile("RecDel")
    container = Container()
    container._instances["pm"] = pm_del
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.get("/api/v1/profiles/RecDel/data-dir").status_code == 200

    orig_stage = config.PinnedStorageRoot.stage_profile_for_delete

    def detach_after_stage(self, name):
        entry = orig_stage(self, name)
        offline["value"] = True
        return entry

    monkeypatch.setattr(config.PinnedStorageRoot, "stage_profile_for_delete", detach_after_stage)
    assert client.delete("/api/v1/profiles/RecDel").status_code == 503
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "stage_profile_for_delete", orig_stage)

    r_del_dir = client.get("/api/v1/profiles/RecDel/data-dir")
    assert r_del_dir.status_code == 409
    assert "interrupted delete" in r_del_dir.json()["detail"]
    assert "interrupted import" not in r_del_dir.json()["detail"]
    assert client.delete("/api/v1/profiles/RecDel").status_code == 200

    # 2. Interrupted PATCH rename after R6 recovery must report rename (not ghost import).
    pm_ren = setup_recovered_profile("RecRen")
    container._instances["pm"] = pm_ren
    orig_ren = config.PinnedStorageRoot.rename_child

    def detach_after_rename(self, src_name, dst_name):
        orig_ren(self, src_name, dst_name)
        if src_name == "RecRen" and dst_name == "RecRenNew":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", detach_after_rename)
    assert client.patch("/api/v1/profiles/RecRen", json={"name": "RecRenNew"}).status_code == 503
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", orig_ren)

    r_ren_dir = client.get("/api/v1/profiles/RecRen/data-dir")
    assert r_ren_dir.status_code == 409
    assert "interrupted rename" in r_ren_dir.json()["detail"]
    assert "interrupted import" not in r_ren_dir.json()["detail"]


def test_r7_1_pre_commit_recovery_requires_original_inode_and_retires_journal(
    tmp_path, monkeypatch
):
    import errno
    import shutil
    from pathlib import Path

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.routes import profiles as profiles_route
    from src.core import config
    from src.core.container import Container

    profiles_file = tmp_path / "profiles.json"
    data_root = tmp_path / "data"
    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(data_root))
    monkeypatch.setattr(config, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(profiles_route, "DATA_DIR", str(data_root))

    unmounted_err = config.StorageVolumeUnmountedError(
        errno.ENODEV, "External storage volume '/Volumes/DETACHED' is not mounted", str(data_root)
    )
    offline = {"value": False}

    def dynamic_mount(path):
        if offline["value"]:
            raise unmounted_err
        return Path(path)

    monkeypatch.setattr(manager_module, "require_data_dir_mounted", dynamic_mount)
    monkeypatch.setattr(config, "require_data_dir_mounted", dynamic_mount)

    pm = manager_module.ProfileManager()
    assert pm.add_profile("PreCheck", "", "macos", engine="camoufox")
    (data_root / "PreCheck" / "marker.txt").write_text("genuine-orig", encoding="utf-8")
    before_store = profiles_file.read_bytes()

    zpath = archive(
        tmp_path / "incoming_chrome.zip",
        name="PreCheck",
        engine="chrome",
        files={"data/marker.txt": "uncommitted-incoming"},
    )
    container = Container()
    container._instances["pm"] = pm
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()

    # Detach right after publishing incoming -> 503 with unchanged Camoufox store.
    orig_from = config.PinnedStorageRoot.rename_from_subdir

    def detach_after_publish(self, subdir_parts, src_name, dst_name):
        orig_from(self, subdir_parts, src_name, dst_name)
        if dst_name == "PreCheck" and len(subdir_parts) == 2 and subdir_parts[1] == "incoming":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", detach_after_publish)
    assert (
        client.post(
            "/api/v1/profiles/import", json={"zip_path": zpath, "overwrite": True}
        ).status_code
        == 503
    )
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", orig_from)
    assert profiles_file.read_bytes() == before_store

    # Edit notes while fenced; notes must stay supported and appear in final verified export.
    assert (
        client.patch("/api/v1/profiles/PreCheck", json={"notes": "r7-old-note"}).status_code == 200
    )

    prev_markers = list(data_root.glob(".import_*/previous/marker.txt"))
    assert len(prev_markers) == 1 and prev_markers[0].read_text(encoding="utf-8") == "genuine-orig"
    prev_dir = prev_markers[0].parent

    # 1. Park BOTH incoming and original backup elsewhere (public path is now EMPTY).
    parked_incoming = tmp_path / "parked_incoming"
    parked_original = tmp_path / "parked_original"
    (data_root / "PreCheck").rename(parked_incoming)
    prev_dir.rename(parked_original)
    assert not (data_root / "PreCheck").exists()
    assert not prev_dir.exists()

    r_empty = client.get("/api/v1/profiles/PreCheck/data-dir")
    assert r_empty.status_code == 409, f"Empty public path must stay fenced: {r_empty.text}"
    empty_detail = r_empty.json()["detail"]
    assert "does not match the original session directory" in empty_detail
    assert str(prev_dir) not in empty_detail  # Must not claim a missing backup exists.
    assert client.get("/api/v1/profiles/PreCheck/fingerprint").status_code == 409
    assert client.delete("/api/v1/profiles/PreCheck").status_code == 409
    for inc in (True, False):
        r_exp = client.post(
            "/api/v1/profiles/PreCheck/export",
            json={"export_dir": str(exp_dir), "include_data": inc},
        )
        assert r_exp.status_code == 200 and r_exp.json()["success"] is False
    assert list(exp_dir.iterdir()) == []

    # 2. Copy parked incoming bytes to public path (new inode != incoming_ino and != prev_ino).
    shutil.copytree(parked_incoming, data_root / "PreCheck")
    assert (data_root / "PreCheck" / "marker.txt").read_text(
        encoding="utf-8"
    ) == "uncommitted-incoming"

    r_copy = client.get("/api/v1/profiles/PreCheck/data-dir")
    assert r_copy.status_code == 409, f"Different-inode copy must stay fenced: {r_copy.text}"
    assert "does not match the original session directory" in r_copy.json()["detail"]
    assert str(prev_dir) not in r_copy.json()["detail"]
    for inc in (True, False):
        r_exp_copy = client.post(
            "/api/v1/profiles/PreCheck/export",
            json={"export_dir": str(exp_dir), "include_data": inc},
        )
        assert r_exp_copy.status_code == 200 and r_exp_copy.json()["success"] is False
    assert list(exp_dir.iterdir()) == []

    # 3. Remove bogus copy and restore actual original directory (verified prev_ino) -> unlocks!
    shutil.rmtree(data_root / "PreCheck")
    parked_original.rename(data_root / "PreCheck")

    r_ok = client.get("/api/v1/profiles/PreCheck/data-dir")
    assert r_ok.status_code == 200 and r_ok.json()["exists"] is True
    r_exp_ok = client.post(
        "/api/v1/profiles/PreCheck/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_exp_ok.json()["success"] is True
    with zipfile.ZipFile(r_exp_ok.json()["zip_path"]) as zf:
        meta = json.loads(zf.read("profile.json").decode("utf-8"))
        assert meta["engine"] == "camoufox"
        assert meta["notes"] == "r7-old-note"
        assert zf.read("data/marker.txt").decode("utf-8") == "genuine-orig"
    Path(r_exp_ok.json()["zip_path"]).unlink()

    # 4. Resolved pre-commit journal must be retired before later legitimate rename/delete/recreate.
    assert len(list((data_root / config.STAGING_DIR_NAME).glob(".importing_*.json"))) == 1
    orig_ren = config.PinnedStorageRoot.rename_child

    def detach_after_ren(self, src_name, dst_name):
        orig_ren(self, src_name, dst_name)
        if src_name == "PreCheck" and dst_name == "PreCheckMoved":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", detach_after_ren)
    assert (
        client.patch("/api/v1/profiles/PreCheck", json={"name": "PreCheckMoved"}).status_code == 503
    )
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_child", orig_ren)

    r_after_ren = client.get("/api/v1/profiles/PreCheck/data-dir")
    assert r_after_ren.status_code == 409
    assert "interrupted rename" in r_after_ren.json()["detail"]
    assert "interrupted import" not in r_after_ren.json()["detail"]
    # Restore rename, delete cleanly, and recreate same name with a brand-new inode -> zero ghost 409.
    (data_root / "PreCheckMoved").rename(data_root / "PreCheck")
    assert client.delete("/api/v1/profiles/PreCheck").status_code == 200
    assert pm.add_profile("PreCheck", "", "macos", engine="camoufox")
    assert client.get("/api/v1/profiles/PreCheck/data-dir").status_code == 200

    # 5. Preserve no-prior-directory case: profile row had no data dir before overwrite.
    assert pm.add_profile("NoDirProf", "", "macos", engine="camoufox")
    shutil.rmtree(data_root / "NoDirProf")
    assert not (data_root / "NoDirProf").exists()
    z_nodir = archive(
        tmp_path / "nodir_in.zip",
        name="NoDirProf",
        engine="chrome",
        files={"data/marker.txt": "nodir-incoming"},
    )

    def detach_nodir(self, subdir_parts, src_name, dst_name):
        orig_from(self, subdir_parts, src_name, dst_name)
        if dst_name == "NoDirProf" and len(subdir_parts) == 2 and subdir_parts[1] == "incoming":
            offline["value"] = True

    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", detach_nodir)
    assert (
        client.post(
            "/api/v1/profiles/import", json={"zip_path": z_nodir, "overwrite": True}
        ).status_code
        == 503
    )
    offline["value"] = False
    monkeypatch.setattr(config.PinnedStorageRoot, "rename_from_subdir", orig_from)

    r_nodir_fenced = client.get("/api/v1/profiles/NoDirProf/data-dir")
    assert r_nodir_fenced.status_code == 409
    assert "previous" not in r_nodir_fenced.json()["detail"]
    parked_nodir_inc = tmp_path / "parked_nodir_inc"
    (data_root / "NoDirProf").rename(parked_nodir_inc)
    r_nodir_ok = client.get("/api/v1/profiles/NoDirProf/data-dir")
    assert r_nodir_ok.status_code == 200 and r_nodir_ok.json()["exists"] is False

    # R8-1: copying parked incoming bytes back to NoDirProf (different inode) MUST re-fence!
    shutil.copytree(parked_nodir_inc, data_root / "NoDirProf")
    assert (data_root / "NoDirProf" / "marker.txt").read_text(encoding="utf-8") == "nodir-incoming"
    r_nodir_copy = client.get("/api/v1/profiles/NoDirProf/data-dir")
    assert r_nodir_copy.status_code == 409, (
        f"Copied incoming dir must stay fenced: {r_nodir_copy.text}"
    )
    assert "previous" not in r_nodir_copy.json()["detail"]
    assert client.get("/api/v1/profiles/NoDirProf/fingerprint").status_code == 409
    assert client.delete("/api/v1/profiles/NoDirProf").status_code == 409
    for inc in (True, False):
        r_nd_exp = client.post(
            "/api/v1/profiles/NoDirProf/export",
            json={"export_dir": str(exp_dir), "include_data": inc},
        )
        assert r_nd_exp.status_code == 200 and r_nd_exp.json()["success"] is False
    assert list(exp_dir.iterdir()) == []

    # Removing the copied directory restores the healthy 200 / exists=false state.
    shutil.rmtree(data_root / "NoDirProf")
    r_nodir_restored = client.get("/api/v1/profiles/NoDirProf/data-dir")
    assert r_nodir_restored.status_code == 200 and r_nodir_restored.json()["exists"] is False
    r_nd_exp_ok = client.post(
        "/api/v1/profiles/NoDirProf/export",
        json={"export_dir": str(exp_dir), "include_data": True},
    )
    assert r_nd_exp_ok.json()["success"] is True
    with zipfile.ZipFile(r_nd_exp_ok.json()["zip_path"]) as zf:
        nd_meta = json.loads(zf.read("profile.json").decode("utf-8"))
        assert nd_meta["engine"] == "camoufox"
        assert not [n for n in zf.namelist() if n.startswith("data/")]
    Path(r_nd_exp_ok.json()["zip_path"]).unlink()

    # Running cookie_tool on a recovered no-prior-dir profile retires the resolved journal
    # before creating DATA_DIR/NoDirProf so subsequent reads return 200/exists=true (not 409).
    from src.services.browser import cookie_tool

    monkeypatch.setattr(cookie_tool, "DATA_DIR", str(data_root))
    monkeypatch.setattr(cookie_tool, "require_data_dir_mounted", dynamic_mount)

    class _FakeCookieCtx:
        def __init__(self, pdir):
            Path(pdir).mkdir(parents=True, exist_ok=True)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def cookies(self):
            return []

    monkeypatch.setattr(cookie_tool, "_open", lambda pdir, os_type, eng: _FakeCookieCtx(pdir))
    assert asyncio.run(cookie_tool._run("export", "NoDirProf", "macos", [], "camoufox")) == {
        "cookies": []
    }
    r_nd_after_cookie = client.get("/api/v1/profiles/NoDirProf/data-dir")
    assert r_nd_after_cookie.status_code == 200 and r_nd_after_cookie.json()["exists"] is True
    assert client.delete("/api/v1/profiles/NoDirProf").status_code == 200
