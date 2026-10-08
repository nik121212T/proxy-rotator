"""Optional per-IP geolocation lookup endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.routes.proxies import Services

router = APIRouter(prefix="/api/geo", tags=["geo"])


@router.get("/{ip}")
async def lookup(ip: str, runtime: Services) -> dict[str, object]:
    service = runtime.geo
    if service is None or not service.enabled:
        raise HTTPException(status_code=404, detail="Geolocation disabled")
    hit = await service.lookup(ip)
    if hit is None:
        return {"ip": ip, "country": None, "country_code": None, "source": "unknown"}
    return {
        "ip": hit.ip,
        "country": hit.country,
        "country_code": hit.country_code,
        "source": hit.source,
    }
