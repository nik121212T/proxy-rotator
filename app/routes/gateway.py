"""Gateway status snapshots; listener wiring belongs to Phase 5."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException

from app.gateway_probe import probe_gateway
from app.models import GatewayStatus
from app.routes.proxies import Services

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


@router.get("/status")
async def status(runtime: Services) -> GatewayStatus:
    return runtime.gateway


@router.get("/activity")
async def activity(runtime: Services) -> dict[str, object]:
    return runtime.activity()


@router.post("/{kind}/test")
async def test_connection(kind: Literal["http", "socks5"], runtime: Services) -> dict[str, object]:
    lock = runtime.gateway_test_locks.setdefault(kind, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="Un test est déjà en cours.")
    async with lock:
        listener = runtime.gateway.http if kind == "http" else runtime.gateway.socks5
        return await probe_gateway(kind, listener.bound, runtime.checker.probe_url)
