"""Application-owned services shared by API dependencies."""

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.health_checker import HealthChecker
from app.pool import Pool
from app.rotator import Rotator


@dataclass(repr=False)
class Runtime:
    pool: Pool
    checker: HealthChecker
    rotator: Rotator


async def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = request.app.state.runtime
    if runtime is None:
        raise HTTPException(status_code=503, detail="Application is not ready")
    return runtime
