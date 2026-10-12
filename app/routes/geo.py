"""Optional per-IP geolocation lookup endpoint."""

from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request

from app.routes.proxies import Services

router = APIRouter(prefix="/api/geo", tags=["geo"])

# Simple per-client rate limit so the endpoint cannot be used as an open
# relay against the geolocation upstream (audit C3). The ceiling is low
# on purpose: a dashboard needs a handful of lookups per minute.
_RATE_WINDOW_S = 60.0
_RATE_MAX = 30
_history: dict[str, deque[float]] = {}


def _rate_limit(client: str) -> None:
    now = time.monotonic()
    bucket = _history.setdefault(client, deque())
    while bucket and now - bucket[0] > _RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= _RATE_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    bucket.append(now)


@router.get("/{ip}")
async def lookup(ip: str, runtime: Services, request: Request) -> dict[str, object]:
    service = runtime.geo
    if service is None or not service.enabled:
        raise HTTPException(status_code=404, detail="Geolocation disabled")
    client_host = request.client.host if request.client else "unknown"
    _rate_limit(client_host)
    hit = await service.lookup(ip)
    if hit is None:
        return {"ip": ip, "country": None, "country_code": None, "source": "unknown"}
    return {
        "ip": hit.ip,
        "country": hit.country,
        "country_code": hit.country_code,
        "source": hit.source,
    }
