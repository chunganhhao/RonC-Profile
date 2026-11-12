from __future__ import annotations

import base64
import os
import tempfile
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..dependencies import get_event_bus, get_extension_service, get_profile_manager

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IProfileManager
    from ...services.browser.extensions import ExtensionService

router = APIRouter(prefix="/extensions", tags=["extensions"])


class ExtensionUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    zip_base64: str = Field(..., min_length=4)


class ExtensionUpdateRequest(BaseModel):
    enabled: bool | None = None
    pin_mode: str | None = None
    apply_to_all_chrome: bool | None = None
    apply_to_all: bool | None = None
    profile_names: list[str] | None = None
    assigned_profiles: list[str] | None = None


@router.get("")
def list_extensions(
    ext: ExtensionService = Depends(get_extension_service),
    pm: IProfileManager = Depends(get_profile_manager),
) -> dict:
    usage = ext.usage_counts(pm)
    profiles = pm.list_profiles()
    items = []
    for e in ext.list_entries():
        assigned = [
            p.name
            for p in profiles
            if (e.id in (p.extension_ids or [])) or (e.apply_to_all_chrome and p.engine == "chrome")
        ]
        items.append(
            {
                **e.to_dict(),
                "assigned_count": usage.get(e.id, 0),
                "assigned_profiles": assigned,
                "has_icon": bool(
                    e.icon_rel_path
                    and os.path.isfile(os.path.join(ext.extension_dir(e.id), e.icon_rel_path))
                ),
            }
        )
    return {"extensions": items, "items": items, "total": len(items)}


@router.get("/{ext_id}/icon", include_in_schema=False)
def get_extension_icon(
    ext_id: str,
    ext: ExtensionService = Depends(get_extension_service),
) -> FileResponse:
    entry = ext.get_entry(ext_id)
    icon_path = entry.icon_abs_path if entry else None
    if not icon_path:
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(icon_path)


@router.post("/upload", status_code=201)
def upload_extension(
    body: ExtensionUploadRequest,
    ext: ExtensionService = Depends(get_extension_service),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    raw_b64 = body.zip_base64.strip()
    if "," in raw_b64 and raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        data = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}") from exc

    if len(data) < 22 or len(data) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Archive size out of allowed range")

    fd, tmp_zip = tempfile.mkstemp(prefix="foxext_upload_", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        entry = ext.install_from_zip(tmp_zip, filename=os.path.basename(body.filename))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if os.path.exists(tmp_zip):
            os.unlink(tmp_zip)

    eb.emit()
    return entry.to_dict()


@router.patch("/{ext_id}")
def update_extension(
    ext_id: str,
    body: ExtensionUpdateRequest,
    ext: ExtensionService = Depends(get_extension_service),
    pm: IProfileManager = Depends(get_profile_manager),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    from ...services.browser.extensions import PIN_MODES

    if body.pin_mode is not None and body.pin_mode not in PIN_MODES:
        raise HTTPException(
            status_code=400, detail=f"pin_mode must be one of: {', '.join(PIN_MODES)}"
        )
    entry = ext.update_extension(
        ext_id,
        enabled=body.enabled,
        pin_mode=body.pin_mode,
        apply_to_all_chrome=(
            body.apply_to_all_chrome if body.apply_to_all_chrome is not None else body.apply_to_all
        ),
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Extension not found")
    target_profiles = (
        body.profile_names if body.profile_names is not None else body.assigned_profiles
    )
    if target_profiles is not None:
        ext.assign_profiles(ext_id, target_profiles, pm)
    eb.emit()
    return entry.to_dict()


@router.delete("/{ext_id}")
def delete_extension(
    ext_id: str,
    ext: ExtensionService = Depends(get_extension_service),
    pm: IProfileManager = Depends(get_profile_manager),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    if not ext.remove_extension(ext_id, pm=pm):
        raise HTTPException(status_code=404, detail="Extension not found")
    eb.emit()
    return {"deleted": ext_id}
