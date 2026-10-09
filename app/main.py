"""FastAPI application and ownership of background services."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.burn_detector import BurnDetector
from app.config import Settings
from app.crypto import CryptoConfigError, decrypt_password, derive_key
from app.db import get_db
from app.gateway_http import HttpGateway
from app.gateway_socks import SocksGateway
from app.geolocation import GeolocationService
from app.health_checker import HealthChecker
from app.pool import Pool
from app.providers.dichvusocks import DichvusocksClient
from app.rotator import Rotator
from app.routes import events, gateway, geo, providers, proxies, ui
from app.routes import pool as pool_routes
from app.runtime import Runtime
from app.salt import load_or_create_salt
from app.ui.rendering import UI_DIR

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
        host = f"[{config.bind_host}]" if ":" in config.bind_host else config.bind_host
        http_configured = f"{host}:{config.gateway_http_port}"
        socks5_configured = f"{host}:{config.gateway_socks_port}"
        http_gateway = HttpGateway(
            pool,
            rotator,
            key,
            host=config.bind_host,
            port=config.gateway_http_port,
            timeout_s=config.gateway_timeout_s,
            max_retry_per_request=config.max_retry_per_request,
            burn_detector=BurnDetector(
                timeout_s=config.gateway_timeout_s,
                inspect_body=config.burn_inspect_body,
            ),
        )
        socks5_gateway = SocksGateway(
            pool,
            rotator,
            key,
            host=config.bind_host,
            port=config.gateway_socks_port,
            timeout_s=config.gateway_timeout_s,
            max_retry_per_request=config.max_retry_per_request,
        )
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(checker.stop)
            checker.start()
            cleanup.push_async_callback(http_gateway.stop)
            await http_gateway.start()
            cleanup.push_async_callback(socks5_gateway.stop)
            await socks5_gateway.start()
            geo_conn = await cleanup.enter_async_context(get_db(config.db_path))
            geo_service = GeolocationService(
                geo_conn,
                enabled=config.geolocation_enabled,
                provider_url=config.geolocation_provider_url,
                cache_ttl_s=config.geolocation_cache_ttl_s,
                timeout_s=config.geolocation_timeout_s,
            )
            await geo_service.ensure_schema()
            from app import provider_store  # noqa: PLC0415

            await provider_store.ensure_schema(conn)
            stored_token = await provider_store.get_token(conn, "dichvusocks", key)
            dichvusocks_client = DichvusocksClient(
                token=stored_token or config.dichvusocks_api_token,
                base_url=config.dichvusocks_base_url,
                timeout_s=config.dichvusocks_timeout_s,
            )
            cleanup.push_async_callback(geo_service.stop)
            geo_service.start(pool)
            yield Runtime(
                pool=pool,
                checker=checker,
                rotator=rotator,
                http_configured=http_configured,
                fernet_key=key,
                socks5_configured=socks5_configured,
                http_gateway=http_gateway,
                socks5_gateway=socks5_gateway,
                geo=geo_service,
                dichvusocks=dichvusocks_client,
                db_conn=conn,
            )


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
    app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")
    app.state.runtime = None
    app.state.config_failed = False
    for router in (proxies.router, pool_routes.router, gateway.router, events.router, geo.router, providers.router, ui.router):
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
