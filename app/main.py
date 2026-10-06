"""FastAPI entry point. Codex: implement lifespan + mount routers per SPEC.md."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    # TODO: load settings, open DB, start health checker + gateways.
    yield
    # TODO: cancel background tasks, close DB.


def create_app() -> FastAPI:
    app = FastAPI(title="proxy-rotator", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # TODO: mount routers from app.routes.proxies / pool / gateway

    return app


app = create_app()
