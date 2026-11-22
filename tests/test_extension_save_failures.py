import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import flet as ft
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import get_event_bus, get_extension_service, get_profile_manager
from src.api.routes.extensions import router
from src.models.profile import Profile
from src.services.browser.extensions import ExtensionService, compute_chrome_extension_id
from src.services.profile import manager
from src.ui.dialogs.extensions import open_extension_settings_dialog, open_extensions_dialog
from src.ui.theme.colors import COLORS


def archive(description="old"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Demo",
                    "version": "1.0",
                    "description": description,
                }
            ),
        )
    return buf.getvalue()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager, "DATA_DIR", str(tmp_path / "data"))
    pm = manager.ProfileManager()
    pm.profiles["alice"] = Profile(name="alice", engine="chrome")
    pm.save_profiles(strict=True)
    ext = ExtensionService(str(tmp_path / "extensions.json"), str(tmp_path / "extensions"))
    entry = ext.install_from_zip(archive(), filename="demo.zip")
    return ext, pm, entry


def fail_write(*_args, **_kwargs):
    raise OSError("disk full")


def test_assignment_rolls_back_and_retry_writes(state, monkeypatch):
    ext, pm, entry = state
    registry = Path(ext.file_path).read_bytes()
    profiles = Path(manager.PROFILES_FILE).read_bytes()
    replace = manager._replace
    monkeypatch.setattr(manager, "_replace", fail_write)
    with pytest.raises(OSError, match="disk full"):
        ext.assign_profiles(entry.id, ["alice"], pm, pin_mode="custom", apply_to_all=True)
    assert pm.profiles["alice"].extension_ids == []
    assert ext._entries[entry.id].pin_mode == entry.pin_mode
    assert not ext._entries[entry.id].apply_to_all
    assert Path(ext.file_path).read_bytes() == registry
    assert Path(manager.PROFILES_FILE).read_bytes() == profiles
    monkeypatch.setattr(manager, "_replace", replace)
    assert ext.assign_profiles(entry.id, ["alice"], pm, pin_mode="custom", apply_to_all=True) == 1
    assert json.loads(Path(manager.PROFILES_FILE).read_text())["alice"]["extension_ids"] == [
        entry.id
    ]
    assert ext.get_entry(entry.id).apply_to_all


def test_update_restores_memory(state, monkeypatch):
    ext, _, entry = state
    registry = Path(ext.file_path).read_bytes()
    monkeypatch.setattr(ext, "_save", fail_write)
    with pytest.raises(OSError, match="disk full"):
        ext.update_extension(entry.id, enabled=False, pin_mode="custom", apply_to_all=True)
    assert ext._entries[entry.id].to_dict() == entry.to_dict()
    assert Path(ext.file_path).read_bytes() == registry


@pytest.mark.parametrize("assigned", [True, False])
def test_patch_returns_save_error(state, monkeypatch, assigned):
    ext, pm, entry = state
    before = Path(ext.file_path).read_bytes(), Path(manager.PROFILES_FILE).read_bytes()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_extension_service] = lambda: ext
    app.dependency_overrides[get_profile_manager] = lambda: pm
    app.dependency_overrides[get_event_bus] = lambda: SimpleNamespace(emit=Mock())
    if assigned:
        monkeypatch.setattr(manager, "_replace", fail_write)
    else:
        monkeypatch.setattr(ext, "_save", fail_write)
    body = {"pin_mode": "custom", "apply_to_all": True, "enabled": False}
    if assigned:
        body["profile_names"] = ["alice"]
    response = TestClient(app, raise_server_exceptions=False).patch(
        f"/extensions/{entry.id}", json=body
    )
    assert response.status_code == 500
    assert "disk full" in response.json()["detail"]
    assert (Path(ext.file_path).read_bytes(), Path(manager.PROFILES_FILE).read_bytes()) == before
    assert pm.profiles["alice"].extension_ids == []


@pytest.mark.parametrize("reinstall", [True, False])
def test_install_save_failure_restores_files(state, monkeypatch, reinstall):
    ext, _, entry = state
    registry = Path(ext.file_path).read_bytes()
    before = entry.to_dict()
    monkeypatch.setattr(ext, "_save", fail_write)
    with pytest.raises((ValueError, OSError), match="disk full"):
        ext.install_from_zip(archive("new"), filename="demo.zip" if reinstall else "other.zip")
    assert Path(ext.file_path).read_bytes() == registry
    assert ext._entries == {entry.id: entry}
    assert ext._entries[entry.id].to_dict() == before
    assert json.loads((Path(entry.path) / "manifest.json").read_text())["description"] == "old"
    assert list(Path(ext.extensions_dir).iterdir()) == [Path(entry.path)]


def page_fake():
    return SimpleNamespace(
        width=1200, height=900, on_resize=None, update=Mock(), pop_dialog=Mock(), show_dialog=Mock()
    )


def controls(control):
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from controls(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from controls(content)


def test_settings_save_error_keeps_dialog_open(state, monkeypatch):
    ext, pm, entry = state
    page, saved, log = page_fake(), Mock(), Mock()
    open_extension_settings_dialog(page, entry, ext, pm, on_saved=saved, log_fn=log)
    dialog = page.show_dialog.call_args.args[0]
    next(c for c in controls(dialog.content) if isinstance(c, ft.Checkbox)).value = True
    monkeypatch.setattr(manager, "_replace", fail_write)
    dialog.actions[-1].on_click(None)
    assert not saved.called
    assert not page.pop_dialog.called
    assert any(
        isinstance(c, ft.Text) and c.visible and "disk full" in (c.value or "")
        for c in controls(dialog.content)
    )
    assert "disk full" in log.call_args.args[0]


def test_upload_reports_broken_and_valid(state, tmp_path):
    ext, pm, _ = state
    broken, valid = tmp_path / "broken.zip", tmp_path / "valid.zip"
    broken.write_bytes(b"broken")
    valid.write_bytes(archive())
    picker = SimpleNamespace(
        pick_files=AsyncMock(
            return_value=[SimpleNamespace(path=str(broken)), SimpleNamespace(path=str(valid))]
        )
    )
    page = page_fake()
    open_extensions_dialog(page, ext, pm, file_picker=picker)
    dialog = page.show_dialog.call_args.args[0]
    upload = next(
        c
        for c in controls(dialog.content)
        if getattr(getattr(c, "on_click", None), "__name__", "") == "_on_upload"
    )
    asyncio.run(upload.on_click(None))
    feedback = next(
        c
        for c in controls(dialog.content)
        if isinstance(c, ft.Text) and "broken.zip" in (c.value or "")
    )
    assert "Demo" in feedback.value and "1.0" in feedback.value
    assert "\n" in feedback.value
    assert feedback.color == COLORS["error"]


def test_install_uses_target_path_for_chrome_id(state):
    ext, _, entry = state
    assert entry.chrome_id == compute_chrome_extension_id(entry.path)
    assert ext.assign_profiles(entry.id, [], None) == 0
