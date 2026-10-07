"""Gateway status snapshots; listener wiring belongs to Phase 5."""

from fastapi import APIRouter

from app.models import GatewayStatus
from app.routes.proxies import Services

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


@router.get("/status")
async def status(runtime: Services) -> GatewayStatus:
    return runtime.gateway
