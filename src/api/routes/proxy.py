from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...services.proxy.geo_check import check_geo
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..dependencies import get_event_bus, get_profile_manager, get_proxy_pool, get_proxy_service
from ..schemas.proxy import GeoCheckRequest, GeoCheckResponse, ProxyCheckRequest, ProxyCheckResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IProfileManager, IProxyService
    from ...services.proxy.pool import ProxyPoolService

router = APIRouter(prefix="/proxy", tags=["proxy"])


class PoolImportRequest(BaseModel):
    text: str = Field(..., min_length=1)
    default_label: str = ""


class PoolAssignRequest(BaseModel):
    profile_names: list[str]
    only_working: bool = False


@router.post("/check", response_model=ProxyCheckResponse)
def check_proxy(
    body: ProxyCheckRequest,
    ps: IProxyService = Depends(get_proxy_service),
) -> ProxyCheckResponse:
    ok, message = ps.check_proxy_sync(body.proxy, body.timeout or 10)
    return ProxyCheckResponse(success=ok, message=message)


@router.post("/geo-check", response_model=GeoCheckResponse)
def geo_check(body: GeoCheckRequest) -> GeoCheckResponse:
    """Check an unsaved proxy / timezone / locale combination (see GET /profiles/{name}/ip-check)."""
    for validate, value in (
        (validate_proxy_format, body.proxy),
        (validate_timezone, body.timezone),
        (validate_locale, body.locale),
    ):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
    return GeoCheckResponse.from_result(
        check_geo(body.proxy or None, body.timezone or None, body.locale or None)
    )


@router.get("/pool")
def list_proxy_pool(
    pool: ProxyPoolService = Depends(get_proxy_pool),
    pm: IProfileManager = Depends(get_profile_manager),
) -> dict:
    usage = pool.usage_counts(pm)
    items = [{**e.to_dict(), "assigned_profiles": usage.get(e.id, 0)} for e in pool.list_entries()]
    return {"proxies": items, "total": len(items)}


@router.post("/pool/bulk")
def import_proxy_pool(
    body: PoolImportRequest,
    pool: ProxyPoolService = Depends(get_proxy_pool),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    added, invalid = pool.bulk_import(body.text, default_label=body.default_label)
    eb.emit()
    return {"added": added, "invalid": invalid, "total": len(pool.list_entries())}


@router.post("/pool/assign")
def assign_proxy_pool(
    body: PoolAssignRequest,
    pool: ProxyPoolService = Depends(get_proxy_pool),
    pm: IProfileManager = Depends(get_profile_manager),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    updated = pool.assign_round_robin(body.profile_names, pm, only_working=body.only_working)
    eb.emit()
    return {"updated": updated}


@router.delete("/pool/{proxy_id}")
def delete_proxy_pool_entry(
    proxy_id: str,
    pool: ProxyPoolService = Depends(get_proxy_pool),
    eb: EventBus = Depends(get_event_bus),
) -> dict:
    if not pool.remove_proxy(proxy_id):
        raise HTTPException(status_code=404, detail="Proxy entry not found")
    eb.emit()
    return {"deleted": proxy_id}
