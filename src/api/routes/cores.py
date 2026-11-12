from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from chrome_engine.fetch import FetchError

from ...services.browser import cores
from ...services.browser.launcher import ProfileBusyError
from ..dependencies import _container

router = APIRouter(prefix="/cores", tags=["cores"])


class StageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str
    version: str
    confirm_download: StrictBool = False


@router.get("")
def inventory() -> dict:
    return cores.inventory()


@router.post("/stage")
def stage(request: StageRequest) -> dict:
    try:
        return cores.stage(
            request.engine, request.version, confirm_download=request.confirm_download
        )
    except (ValueError, OSError, FetchError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ApplyRequest(StageRequest):
    name: str
    confirm_backup: StrictBool = False
    confirm_apply: StrictBool = False


class PruneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_prune: StrictBool = False


@router.post("/activate")
def activate(request: ApplyRequest, container=Depends(_container)) -> dict:
    try:
        if not (request.confirm_backup and request.confirm_apply):
            raise cores.CoreError("Separate backup and apply confirmations are required")
        return cores.backup_and_apply(
            container.profile_manager,
            container.browser_launcher,
            request.name,
            request.engine,
            request.version,
            confirm_backup=request.confirm_backup,
            confirm_apply=request.confirm_apply,
        )
    except (ValueError, OSError, RuntimeError, ProfileBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prune")
def prune(request: PruneRequest, container=Depends(_container)) -> dict:
    try:
        return cores.prune_unused(
            container.profile_manager,
            container.browser_launcher,
            confirm_prune=request.confirm_prune,
        )
    except (ValueError, OSError, RuntimeError, ProfileBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
