"""FastAPI application and ownership of background services."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.crypto import CryptoConfigError, decrypt_password, derive_key
from app.db import get_db
from app.health_checker import HealthChecker
from app.models import GatewayStatus, ListenerStatus
from app.pool import Pool
from app.rotator import Rotator
from app.routes import events, gateway, proxies
from app.routes import pool as pool_routes
from app.runtime import Runtime
from app.salt import load_or_create_salt

logger = logging.getLogger(__name__)


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError:
        raise CryptoConfigError("Invalid application configuration; check .env") from None


@asynccontextmanager
async def services(config: Settings) -> AsyncIterator[Runtime]:
    salt = await asyncio.to_thread(load_or_create_salt, config.db_path.parent / ".salt")
    key = await asyncio.to_thread(derive_key, config.master_secret, salt)
    async with get_db(config.db_path) as conn:
        pool = await Pool.load(conn, key, min_score=config.pool_min_score)
        for proxy in pool.all():
            await asyncio.to_thread(decrypt_password, proxy.password_enc.get_secret_value(), key)
        checker = HealthChecker(
            pool,
            key,
            probe_url=config.health_probe_url,
            concurrency=config.health_concurrency,
            timeout_s=config.health_timeout_s,
            interval_s=config.health_check_interval_s,
        )
        rotator = Rotator(config.rotation_strategy, sticky_ttl_s=config.sticky_ttl_s)
        checker.start()
        try:
            host = f"[{config.bind_host}]" if ":" in config.bind_host else config.bind_host
            gateway_status = GatewayStatus(
                http=ListenerStatus(configured=f"{host}:{config.gateway_http_port}"),
                socks5=ListenerStatus(configured=f"{host}:{config.gateway_socks_port}"),
            )
            yield Runtime(pool, checker, rotator, gateway_status)
        finally:
            await checker.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            config = settings if settings is not None else load_settings()
            if config.bind_host not in {"127.0.0.1", "localhost", "::1"}:
                logger.warning("event=lan_binding admin_interface_exposed=true")
            async with services(config) as runtime:
                app.state.runtime = runtime
                app.state.pool = runtime.pool
                yield
        except CryptoConfigError:
            app.state.config_failed = True
            raise
        finally:
            app.state.runtime = None
            app.state.pool = None

    app = FastAPI(title="proxy-rotator", version="0.1.0", lifespan=lifespan)
    app.state.runtime = None
    app.state.config_failed = False
    for router in (proxies.router, pool_routes.router, gateway.router, events.router):
        app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic/FastAPI error details may contain the original credential-bearing input.
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        if app.state.runtime is None:
            raise HTTPException(status_code=503, detail="Application is not ready")
        return {"status": "ok"}

    return app


def run() -> int:
    try:
        settings = load_settings()
    except CryptoConfigError:
        logger.error("event=config_error check_env=true")
        return 78
    application = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=settings.bind_host,
            port=settings.admin_port,
            log_level=settings.log_level.lower(),
        )
    )
    server.run()
    if application.state.config_failed:
        return 78
    return 0 if server.started else 1


app = create_app()

if __name__ == "__main__":
    raise SystemExit(run())
