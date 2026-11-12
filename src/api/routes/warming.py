from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...services.warming.scheduler import WARMING_PRESETS
from ..dependencies import (
    get_browser_launcher,
    get_event_bus,
    get_profile_manager,
    get_warming_scheduler,
)

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager
    from ...services.warming.scheduler import WarmingSchedulerService

router = APIRouter(prefix="/warming", tags=["warming"])


class WarmingCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    profile_names: list[str] = Field(..., min_length=1)
    preset: str = "youtube_trust"
    custom_urls: list[str] = Field(default_factory=list)
    duration_minutes: int = Field(default=3, ge=1, le=60)
    schedule_time: str = ""
    repeat_daily: bool = True
    enabled: bool = True


@router.get("/tasks")
def list_warming_tasks(
    ws: WarmingSchedulerService = Depends(get_warming_scheduler),
) -> dict:
    tasks = [t.to_dict() for t in ws.list_tasks()]
    return {"tasks": tasks, "presets": list(WARMING_PRESETS.keys())}


@router.post("/tasks", status_code=201)
def create_warming_task(
    body: WarmingCreateRequest,
    ws: WarmingSchedulerService = Depends(get_warming_scheduler),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    task = ws.add_task(
        name=body.name,
        profile_names=body.profile_names,
        preset=body.preset,
        custom_urls=body.custom_urls,
        duration_minutes=body.duration_minutes,
        schedule_time=body.schedule_time,
        repeat_daily=body.repeat_daily,
        enabled=body.enabled,
    )
    if task is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid warming task (check schedule_time HH:MM, profile_names, or custom URLs)",
        )
    eb.emit()
    return task.to_dict()


@router.post("/tasks/{task_id}/run")
def run_warming_task(
    task_id: str,
    ws: WarmingSchedulerService = Depends(get_warming_scheduler),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    if ws.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Warming task not found")
    started = ws.run_task_async(task_id, pm, bl, on_update=eb.emit)
    return {"started": started, "task_id": task_id}


@router.post("/tasks/{task_id}/stop")
def stop_warming_task(
    task_id: str,
    ws: WarmingSchedulerService = Depends(get_warming_scheduler),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    if not ws.stop_task(task_id):
        raise HTTPException(status_code=404, detail="Warming task not found")
    eb.emit()
    return {"stopped": True, "task_id": task_id}


@router.delete("/tasks/{task_id}")
def delete_warming_task(
    task_id: str,
    ws: WarmingSchedulerService = Depends(get_warming_scheduler),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    if not ws.delete_task(task_id):
        raise HTTPException(status_code=404, detail="Warming task not found")
    eb.emit()
    return {"deleted": task_id}
