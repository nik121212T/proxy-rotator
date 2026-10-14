"""In-memory pool statistics and rotation control."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.models import PoolStats, RotationSelection, RotationStrategy
from app.routes.proxies import Services
from app.views import feedback

router = APIRouter(prefix="/api/pool", tags=["pool"])


class StrategyRequest(BaseModel):
    strategy: RotationStrategy
    pinned_only: bool | None = Field(default=None, strict=True)
    interval_minutes: int = Field(default=1, ge=1, le=1440)


@router.get("/stats")
async def stats(runtime: Services) -> PoolStats:
    return runtime.pool.stats(runtime.rotator.strategy)


@router.post("/strategy", response_model=PoolStats)
async def strategy(
    body: StrategyRequest, runtime: Services, request: Request
) -> PoolStats | HTMLResponse:
    if body.pinned_only is not None:
        runtime.rotator.selection = runtime.rotator.selection.model_copy(
            update={"pinned_only": body.pinned_only}
        )
    chosen = "round_robin" if runtime.rotator.selection.pinned_only else body.strategy
    runtime.rotator.configure(chosen, interval_minutes=body.interval_minutes)
    result = runtime.pool.publish_stats(chosen)
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


@router.post("/clear-all", response_model=dict[str, int])
async def clear_all(runtime: Services, request: Request) -> dict[str, int] | HTMLResponse:
    for proxy in runtime.pool.all():
        await runtime.checker.cancel_one(proxy.id)
    deleted = await runtime.pool.clear_all()
    if request.headers.get("HX-Request") == "true":
        return feedback(request, f"Pool réinitialisé — {deleted} proxy(s) supprimé(s).")
    return {"deleted": deleted}


@router.post("/selection", response_model=RotationSelection)
async def selection(
    body: RotationSelection, runtime: Services, request: Request
) -> RotationSelection | HTMLResponse:
    if "pinned_only" not in body.model_fields_set:
        body = body.model_copy(update={"pinned_only": runtime.rotator.selection.pinned_only})
    runtime.rotator.selection = body
    if body.pinned_only and runtime.rotator.strategy != "round_robin":
        runtime.rotator.configure("round_robin")
    runtime.pool.publish_stats(runtime.rotator.strategy)
    if request.headers.get("HX-Request") == "true":
        return feedback(
            request,
            "Sélection appliquée aux prochaines connexions. Les connexions ouvertes continuent.",
        )
    return body
