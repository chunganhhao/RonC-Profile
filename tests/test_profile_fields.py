"""Ordering and multi-tag metadata use fixtures only, never browser identity files."""

import json
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from src.api.routes.profiles import router
from src.models.profile import (
    Profile,
    extract_backup_code_tokens,
    normalize_backup_codes,
    normalize_tags,
)
from src.services.profile import manager as pm_mod
from src.services.profile import transfer
from src.services.profile.transfer import export_to_zip, import_from_zip


@pytest.fixture
def pm(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    return pm_mod.ProfileManager()


def update(pm, name, **extra):
    p = pm.profiles[name]
    return pm.update_profile(name, name, p.proxy or "", p.os_type, p.timezone, p.locale, **extra)


def test_tags_normalize_and_legacy_group_survives_reload(pm):
    assert pm.add_profile("old", "", "macos", group="Gmail")
    assert pm.profiles["old"].effective_tags == ["Gmail"]
    assert update(pm, "old", tags=[" Gmail ", "AGY", "gmail", ""])
    assert pm.profiles["old"].effective_tags == ["Gmail", "AGY"]
    assert update(pm, "old", group="Gemini")
    assert pm_mod.ProfileManager().profiles["old"].effective_tags == ["Gemini", "AGY"]
    assert update(pm, "old", notes="only this field")
    assert pm.profiles["old"].group == "Gemini"
    assert pm.profiles["old"].tags == ["Gemini", "AGY"]
    assert update(pm, "old", tags=[])
    loaded = pm_mod.ProfileManager().profiles["old"]
    assert loaded.effective_tags == [] and loaded.group is None


@pytest.mark.parametrize("tags", ["Gmail", ["a"] * 21, ["a" * 65], [3]])
def test_invalid_tags_do_not_mutate_or_rename(pm, tags):
    assert pm.add_profile("a", "", "macos", group="Gmail")
    before = Path(pm_mod.PROFILES_FILE).read_bytes()
    assert not pm.update_profile("a", "renamed", "", "macos", tags=tags)
    assert Path(pm_mod.PROFILES_FILE).read_bytes() == before
    assert set(pm.profiles) == {"a"}


def test_reorder_moves_position_preserves_identity_and_core(pm, tmp_path):
    for name in ["a", "b", "c"]:
        assert pm.add_profile(name, "", "macos")
        (tmp_path / "data" / name / "session.fixture").write_text(name)
    pm.profiles["b"].core_version = "fixture-core"
    pm.save_profiles()
    assert update(pm, "c", display_order=1)
    assert [p.name for p in pm.list_profiles()] == ["c", "a", "b"]
    assert [p.display_order for p in pm.list_profiles()] == [1, 2, 3]
    assert update(pm, "a", display_order=1)
    assert [p.name for p in pm_mod.ProfileManager().list_profiles()] == ["a", "c", "b"]
    assert update(pm, "a", display_order=1_000_000)
    assert [p.name for p in pm.list_profiles()] == ["c", "b", "a"]
    assert pm.profiles["b"].core_version == "fixture-core"
    for name in ["a", "b", "c"]:
        assert (tmp_path / "data" / name / "session.fixture").read_text() == name
    assert pm.add_profile("d", "", "macos")
    assert [p.name for p in pm.list_profiles()] == ["c", "b", "a", "d"]
    assert pm.delete_profile("b")
    assert [p.name for p in pm.list_profiles()] == ["c", "a", "d"]
    assert [p.display_order for p in pm.list_profiles()] == [1, 2, 3]


@pytest.mark.parametrize("position", [0, -1, True, 1.2, "1", 1_000_001])
def test_invalid_order_rejected(pm, position):
    assert not pm.add_profile("bad", "", "macos", display_order=position)
    assert not pm.profiles


def test_old_and_malformed_optional_metadata_loads_without_loss(pm):
    with open(pm_mod.PROFILES_FILE, "w") as f:
        json.dump(
            {
                "legacy": {"name": "legacy", "group": "Gmail"},
                "bad-optional": {"name": "bad-optional", "tags": 12, "display_order": "oops"},
            },
            f,
        )
    loaded = pm_mod.ProfileManager()
    assert [p.name for p in loaded.list_profiles()] == ["legacy", "bad-optional"]
    assert loaded.profiles["legacy"].effective_tags == ["Gmail"]
    assert loaded.profiles["bad-optional"].display_order is None


def test_metadata_round_trip_export_import(tmp_path):
    p = Profile("fixture", tags=["Gmail", "AGY"], group="Gmail", display_order=3)
    ok, archive = export_to_zip(p, str(tmp_path / "absent"), str(tmp_path), include_data=False)
    assert ok
    ok, loaded = import_from_zip(archive, str(tmp_path / "imported"))
    assert ok and loaded.tags == p.tags and loaded.display_order == 3


def test_exports_in_the_same_second_keep_both_archives(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(transfer, "datetime", FixedDatetime)
    profile = Profile(name="alice", engine="chrome")
    data = tmp_path / "data"
    preferences = data / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True)

    preferences.write_text('{"backup": 1}', encoding="utf-8")
    ok, first = export_to_zip(profile, str(data), str(tmp_path))
    assert ok, first
    preferences.write_text('{"backup": 2}', encoding="utf-8")
    ok, second = export_to_zip(profile, str(data), str(tmp_path))
    assert ok, second

    assert first != second
    with zipfile.ZipFile(first) as archive:
        assert archive.read("data/Default/Preferences") == b'{"backup": 1}'
    with zipfile.ZipFile(second) as archive:
        assert archive.read("data/Default/Preferences") == b'{"backup": 2}'


def test_patch_metadata_api_validates_and_preserves_credentials(pm):
    assert pm.add_profile(
        "a", "", "macos", account_uid="fixture@example.test", account_password="secret"
    )
    assert pm.add_profile("b", "", "macos")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_profile_manager] = lambda: pm
    app.dependency_overrides[get_browser_launcher] = lambda: SimpleNamespace(
        is_running=lambda _: False
    )
    app.dependency_overrides[get_event_bus] = lambda: SimpleNamespace(emit=lambda: None)
    client = TestClient(app)
    r = client.patch("/profiles/a", json={"tags": ["Gmail", "AGY"], "display_order": 2})
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["Gmail", "AGY"]
    assert r.json()["group"] == "Gmail"
    assert r.json()["display_order"] == 2
    assert pm.profiles["a"].account_uid == "fixture@example.test"
    assert pm.profiles["a"].account_password == "secret"
    assert client.patch("/profiles/a", json={"tags": ["a" * 65]}).status_code == 400
    assert client.patch("/profiles/a", json={"display_order": True}).status_code == 422
    assert client.patch("/profiles/a", json={"display_order": 0}).status_code == 422
    assert normalize_tags([" AGY ", "agy"]) == ["AGY"]


def test_stale_reorder_cannot_erase_core_binding(pm):
    assert pm.add_profile("a", "", "macos")
    assert pm.add_profile("b", "", "macos")
    other = pm_mod.ProfileManager()
    other.profiles["a"].core_version = "fixture-pinned"
    other.save_profiles()
    with pytest.raises(pm_mod.ProfileStoreChangedError):
        update(pm, "a", display_order=2)
    assert pm_mod.ProfileManager().profiles["a"].core_version == "fixture-pinned"


def test_group_update_deduplicates_against_secondary(pm):
    assert pm.add_profile("a", "", "macos", tags=["Gmail", "AGY"])
    assert update(pm, "a", group="agy")
    assert pm_mod.ProfileManager().profiles["a"].effective_tags == ["agy"]


def test_invalid_import_metadata_never_deletes_existing_data(pm, tmp_path):
    assert pm.add_profile("a", "", "macos")
    marker = tmp_path / "data" / "a" / "session.fixture"
    marker.write_text("preserve")
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as f:
        f.writestr(
            "profile.json", json.dumps({"name": "a", "engine": "chrome", "tags": ["x" * 65]})
        )
    ok, _ = pm.import_profile(str(archive), overwrite=True)
    assert not ok
    assert marker.read_text() == "preserve"


def test_duplicate_imported_ranks_are_stable_until_explicit_move(pm):
    for name in ["a", "b", "c"]:
        assert pm.add_profile(name, "", "macos")
        pm.profiles[name].display_order = 1
    pm.save_profiles()
    assert [p.name for p in pm_mod.ProfileManager().list_profiles()] == ["a", "b", "c"]
    assert update(pm, "c", display_order=1)
    assert [(p.name, p.display_order) for p in pm.list_profiles()] == [("c", 1), ("a", 2), ("b", 3)]


@pytest.mark.parametrize("extra", [{"notes": "new"}, {"display_order": 1}, {"tags": ["AGY"]}])
def test_update_disk_failure_raises_and_restores_metadata(pm, monkeypatch, extra):
    assert pm.add_profile("a", "", "macos", tags=["Gmail"])
    assert pm.add_profile("b", "", "macos")
    before = {name: p.to_dict() for name, p in pm.profiles.items()}
    external_reference = pm.profiles["b"]
    disk = Path(pm_mod.PROFILES_FILE).read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(pm_mod, "_replace", fail_replace)
    with pytest.raises(OSError, match="fixture disk failure"):
        update(pm, "b", **extra)
    assert {name: p.to_dict() for name, p in pm.profiles.items()} == before
    assert external_reference is pm.profiles["b"]
    assert external_reference.to_dict() == before["b"]
    assert Path(pm_mod.PROFILES_FILE).read_bytes() == disk


def test_delete_disk_failure_preserves_metadata_and_browser_data(pm, tmp_path, monkeypatch):
    for order, name in enumerate(["a", "b", "c"], 1):
        assert pm.add_profile(name, "", "macos", display_order=order)
    data_dir = tmp_path / "data" / "b"
    marker = data_dir / "session.fixture"
    marker.write_text("unchanged")
    profiles = pm.profiles
    original = profiles.copy()
    disk = Path(pm_mod.PROFILES_FILE).read_bytes()

    def fail_replace(*_args):
        raise OSError("fixture disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(pm_mod, "_replace", fail_replace)
        with pytest.raises(OSError, match="fixture disk failure"):
            pm.delete_profile("b")

    assert marker.read_text() == "unchanged"
    assert Path(pm_mod.PROFILES_FILE).read_bytes() == disk
    assert pm.profiles is profiles
    assert list(profiles) == ["a", "b", "c"]
    assert all(profiles[name] is profile for name, profile in original.items())
    assert [p.display_order for p in profiles.values()] == [1, 2, 3]

    assert pm.delete_profile("b")
    assert not data_dir.exists()
    assert [(p.name, p.display_order) for p in pm_mod.ProfileManager().list_profiles()] == [
        ("a", 1),
        ("c", 2),
    ]


def test_failed_rename_commit_restores_directory_and_skips_identity_edit(pm, tmp_path, monkeypatch):
    assert pm.add_profile("old", "", "macos", account_uid="fixed@example.test")
    assert pm.add_profile("second", "", "macos")
    old = tmp_path / "data" / "old"
    marker = old / "session.fixture"
    marker.write_text("unchanged")
    disk = Path(pm_mod.PROFILES_FILE).read_bytes()
    identity_edits = []
    monkeypatch.setattr(pm_mod.fp_service, "reset", lambda *_: identity_edits.append(True))

    def fail_replace(*_args, **_kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(pm_mod, "_replace", fail_replace)
    with pytest.raises(OSError, match="fixture disk failure"):
        pm.update_profile("old", "new", "", "linux")
    assert marker.read_text() == "unchanged"
    assert not (tmp_path / "data" / "new").exists()
    assert list(pm.profiles) == ["old", "second"]
    assert pm.profiles["old"].account_uid == "fixed@example.test"
    assert pm.profiles["old"].os_type == "macos"
    assert Path(pm_mod.PROFILES_FILE).read_bytes() == disk
    assert identity_edits == []


def test_rename_keeps_legacy_row_position_and_identity(pm, tmp_path):
    assert pm.add_profile("first", "", "macos", account_uid="fixed@example.test")
    assert pm.add_profile("second", "", "macos")
    pm.profiles["first"].core_version = "fixture-core"
    pm.save_profiles()
    (tmp_path / "data" / "first" / "session.fixture").write_text("unchanged")
    assert pm.update_profile("first", "renamed", "", "macos")
    assert [p.name for p in pm.list_profiles()] == ["renamed", "second"]
    assert [p.name for p in pm_mod.ProfileManager().list_profiles()] == ["renamed", "second"]
    assert pm.profiles["renamed"].account_uid == "fixed@example.test"
    assert pm.profiles["renamed"].core_version == "fixture-core"
    assert (tmp_path / "data" / "renamed" / "session.fixture").read_text() == "unchanged"


def test_backup_codes_parse_persist_api_and_zip_round_trip(pm, tmp_path, monkeypatch):
    import asyncio

    from src.api.app import create_app
    from src.core import config as app_config
    from src.core.container import Container
    from src.mcp_server import server as mcp_srv

    monkeypatch.setattr(app_config, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(app_config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr("src.core.container.LOG_DIR", str(tmp_path / "logs"))

    sheet = (
        "FIXTURE BACKUP CODES\n"
        "Synthetic verification sheet for automated tests.\n\n"
        "1. 1111 0001        6. 1111 0006\n"
        "2. 1111 0002        7. 1111 0007\n"
        "3. 1111 0003        8. 1111 0008\n"
        "4. 1111 0004        9. 1111 0009\n"
        "5. 1111 0005       10. 1111 0010\n\n"
        "(fixture-user@example.test)"
    )
    canary = "1111 0001"
    tokens = extract_backup_code_tokens(sheet)
    assert tokens == [
        "1111 0001",
        "1111 0006",
        "1111 0002",
        "1111 0007",
        "1111 0003",
        "1111 0008",
        "1111 0004",
        "1111 0009",
        "1111 0005",
        "1111 0010",
    ]
    assert normalize_backup_codes("  1. 1111 0001\r\n2. 1111 0002  ") == (
        "1. 1111 0001\n2. 1111 0002"
    )
    with pytest.raises(ValueError, match="4096"):
        normalize_backup_codes("x" * 4097)

    assert pm.add_profile("bkp", "", "macos", backup_codes=sheet)
    assert pm.profiles["bkp"].backup_codes == sheet
    reloaded = pm_mod.ProfileManager()
    assert reloaded.profiles["bkp"].backup_codes == sheet

    # ZIP round-trip preserves backup_codes even when browser data is excluded
    ok, archive = export_to_zip(
        reloaded.profiles["bkp"], str(tmp_path / "absent"), str(tmp_path), include_data=False
    )
    assert ok
    ok_imp, imported = import_from_zip(archive, str(tmp_path / "imported"))
    assert ok_imp and imported.backup_codes == sheet

    # Authenticated REST & MCP metadata must NOT expose raw backup codes;
    # only the dedicated authenticated GET /{name}/backup-codes route returns them with no-store.
    container = Container()
    monkeypatch.setattr("src.api.app.API_TOKEN", "fixture-secret-token")
    monkeypatch.setattr("src.api.auth.API_TOKEN", "fixture-secret-token")
    app = create_app(container)
    app.dependency_overrides[get_profile_manager] = lambda: pm
    app.dependency_overrides[get_browser_launcher] = lambda: SimpleNamespace(
        is_running=lambda _: False, running_profile_names=lambda: set()
    )
    app.dependency_overrides[get_event_bus] = lambda: SimpleNamespace(emit=lambda: None)
    client = TestClient(app, base_url="http://127.0.0.1")
    auth = {"Authorization": "Bearer fixture-secret-token"}

    assert client.get("/api/v1/profiles/bkp/backup-codes").status_code == 401
    res_list = client.get("/api/v1/profiles", headers=auth)
    assert res_list.status_code == 200
    assert canary not in res_list.text
    list_item = next(item for item in res_list.json()["profiles"] if item["name"] == "bkp")
    assert list_item["has_backup_codes"] is True
    assert "backup_codes" not in list_item

    res_get = client.get("/api/v1/profiles/bkp", headers=auth)
    assert res_get.status_code == 200
    assert canary not in res_get.text
    assert res_get.json()["has_backup_codes"] is True
    assert "backup_codes" not in res_get.json()

    res_post = client.post(
        "/api/v1/profiles",
        headers=auth,
        json={"name": "bkp-post", "os_type": "macos", "backup_codes": "1. 3333 0001"},
    )
    assert res_post.status_code == 201
    assert "3333 0001" not in res_post.text
    assert res_post.json()["has_backup_codes"] is True

    # MCP metadata tools forward REST metadata without exposing backup codes or adding a secret tool
    tool_names = set(mcp_srv.mcp._tool_manager._tools.keys())
    assert not any("backup" in t_name for t_name in tool_names)

    async def _fake_arequest(method, path, payload=None):
        resp = client.request(method, f"/api/v1{path}", json=payload, headers=auth)
        return resp.json()

    monkeypatch.setattr(mcp_srv, "_arequest", _fake_arequest)
    mcp_list = asyncio.run(mcp_srv.list_profiles())
    mcp_detail = asyncio.run(mcp_srv.get_profile("bkp"))
    assert canary not in json.dumps(mcp_list)
    assert canary not in json.dumps(mcp_detail)

    # Explicit on-demand backup-codes endpoint returns no-store + secret
    res_bkp = client.get("/api/v1/profiles/bkp/backup-codes", headers=auth)
    assert res_bkp.status_code == 200
    assert res_bkp.headers.get("cache-control") == "no-store"
    assert res_bkp.json() == {"name": "bkp", "has_backup_codes": True, "backup_codes": sheet}

    # Notes-only PATCH omitting backup_codes preserves sheet; PATCH with "" clears it
    res_notes_only = client.patch(
        "/api/v1/profiles/bkp", headers=auth, json={"notes": "updated note"}
    )
    assert res_notes_only.status_code == 200
    assert canary not in res_notes_only.text
    assert pm.profiles["bkp"].backup_codes == sheet

    res_patch = client.patch(
        "/api/v1/profiles/bkp", headers=auth, json={"backup_codes": "1. 2222 0001"}
    )
    assert res_patch.status_code == 200
    assert "2222 0001" not in res_patch.text
    assert pm.profiles["bkp"].backup_codes == "1. 2222 0001"

    res_bad = client.patch("/api/v1/profiles/bkp", headers=auth, json={"backup_codes": "x" * 4097})
    assert res_bad.status_code == 400
    assert pm.profiles["bkp"].backup_codes == "1. 2222 0001"

    res_clear = client.patch("/api/v1/profiles/bkp", headers=auth, json={"backup_codes": ""})
    assert res_clear.status_code == 200
    assert res_clear.json()["has_backup_codes"] is False
    assert pm.profiles["bkp"].backup_codes is None
