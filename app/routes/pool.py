"""In-memory pool statistics and rotation control."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.models import PoolStats, RotationStrategy
from app.routes.proxies import Services
from app.ui.rendering import feedback

router = APIRouter(prefix="/api/pool", tags=["pool"])


class StrategyRequest(BaseModel):
    strategy: RotationStrategy


@router.get("/stats")
async def stats(runtime: Services) -> PoolStats:
    return runtime.pool.stats(runtime.rotator.strategy)


@router.post("/strategy", response_model=PoolStats)
async def strategy(
    body: StrategyRequest, runtime: Services, request: Request
) -> PoolStats | HTMLResponse:
    runtime.rotator.strategy = body.strategy
    result = runtime.pool.publish_stats(body.strategy)
    if request.headers.get("HX-Request") == "true":
        return feedback(request, "Stratégie de rotation mise à jour.")
    return result


@router.post("/clear-dead", response_model=dict[str, int])
async def clear_dead(runtime: Services, request: Request) -> dict[str, int] | HTMLResponse:
    for proxy in runtime.pool.all():
        if proxy.status == "dead":
            await runtime.checker.cancel_one(proxy.id)
    deleted = await runtime.pool.clear_dead()
    if request.headers.get("HX-Request") == "true":
        return feedback(request, f"{deleted} proxy(s) hors pool supprimé(s).")
    return {"deleted": deleted}
