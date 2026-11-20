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
