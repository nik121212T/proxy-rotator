"""In-memory pool statistics and rotation control."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.models import PoolStats, RotationStrategy
from app.routes.proxies import Services

router = APIRouter(prefix="/api/pool", tags=["pool"])


class StrategyRequest(BaseModel):
    strategy: RotationStrategy


@router.get("/stats")
async def stats(runtime: Services) -> PoolStats:
    return runtime.pool.stats(runtime.rotator.strategy)


@router.post("/strategy")
async def strategy(body: StrategyRequest, runtime: Services) -> PoolStats:
    runtime.rotator.strategy = body.strategy
    return runtime.pool.publish_stats(body.strategy)


@router.post("/clear-dead")
async def clear_dead(runtime: Services) -> dict[str, int]:
    for proxy in runtime.pool.all():
        if proxy.status == "dead":
            await runtime.checker.cancel_one(proxy.id)
    return {"deleted": await runtime.pool.clear_dead()}
