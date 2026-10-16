"""FastAPI application and ownership of background services."""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.browser_manager import BrowserManager
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
from app.routes import browsers, events, gateway, geo, providers, proxies, ui
from app.routes import pool as pool_routes
from app.runtime import Runtime
from app.salt import load_or_create_salt
from app.security import (
    ADMIN_COOKIE,
    CrossSiteGuard,
    HostGuard,
    TokenGuard,
    configured_hosts,
    is_loopback,
)
from app.views import UI_DIR, templates

logger = logging.getLogger(__name__)


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        # Field names and our own messages only: ``hide_input_in_errors`` keeps
        # submitted values out of the report.
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'config'}: {item['msg']}"
            for item in error.errors()
        )
        raise CryptoConfigError(f"Invalid application configuration: {details}") from None


@asynccontextmanager
async def services(config: Settings) -> AsyncIterator[Runtime]:
    salt = await asyncio.to_thread(load_or_create_salt, config.db_path.parent / ".salt")
    key = await asyncio.to_thread(derive_key, config.master_secret, salt)
    async with get_db(config.db_path) as conn:
        pool = await Pool.load(
            conn,
            key,
            min_score=config.pool_min_score,
            quarantine_minutes=config.quarantine_minutes,
        )
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
            max_connections=config.gateway_max_connections,
        )
        socks5_gateway = SocksGateway(
            pool,
            rotator,
            key,
            host=config.bind_host,
            port=config.gateway_socks_port,
            timeout_s=config.gateway_timeout_s,
            max_retry_per_request=config.max_retry_per_request,
            max_connections=config.gateway_max_connections,
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
            browser_manager = BrowserManager(pool, rotator, key, config.db_path.parent / "browser-profiles", settings=config)
            cleanup.push_async_callback(browser_manager.stop)
            yield Runtime(
                browsers=browser_manager,
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
    if settings is None:
        # Resolve now so the request guards can be built. A broken configuration
        # is not fatal here: the lifespan below reports it and exits with
        # EX_CONFIG exactly as before.
        try:
            settings = load_settings()
        except CryptoConfigError:
            settings = None
    allowed_hosts = configured_hosts(
        settings.bind_host if settings is not None else "127.0.0.1",
        settings.admin_allowed_hosts if settings is not None else [],
    )
    admin_token = (
        settings.admin_token.get_secret_value().strip()
        if settings is not None and settings.admin_token is not None
        else ""
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            config = settings if settings is not None else load_settings()
            if not is_loopback(config.bind_host):
                # Reachable only after ALLOW_REMOTE_BIND, ADMIN_TOKEN and
                # ADMIN_ALLOWED_HOSTS have all been set deliberately.
                logger.warning(
                    "event=lan_binding bind_host=%s admin_token=set "
                    "proxy_gateways_unauthenticated=true",
                    config.bind_host,
                )
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
    # Added innermost first: the last one added runs first.
    app.add_middleware(TokenGuard, token=admin_token)
    app.add_middleware(CrossSiteGuard, allowed=allowed_hosts)
    app.add_middleware(HostGuard, allowed=allowed_hosts)
    app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")
    app.state.runtime = None
    app.state.config_failed = False
    app.state.admin_token_required = bool(admin_token)
    for router in (browsers.router, proxies.router, pool_routes.router, gateway.router, events.router, geo.router, providers.router, ui.router):
        app.include_router(router)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(request: Request) -> Response:
        if not admin_token:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request=request, name="login.html", context={})

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        if not admin_token:
            return RedirectResponse("/", status_code=303)
        # Parsed by hand: urlencoded form data needs no extra dependency.
        body = (await request.body()).decode("utf-8", "replace")
        supplied = (parse_qs(body).get("token") or [""])[0]
        if not secrets.compare_digest(supplied.strip(), admin_token):
            logger.warning("admin_login_rejected")
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Jeton invalide."},
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            ADMIN_COOKIE,
            admin_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

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
    except CryptoConfigError as error:
        # The message names the offending setting and never contains a value, so
        # a refused startup says what to fix instead of just failing.
        logger.error("event=config_error check_env=true detail=%s", error)
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
