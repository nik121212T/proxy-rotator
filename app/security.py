"""Request guards for the single-user admin application.

Three independent controls are applied to every HTTP request:

``HostGuard``
    Refuses a ``Host`` header that is not one of the configured names. A page
    served from an attacker-controlled domain that resolves to ``127.0.0.1``
    (DNS rebinding) reaches the loopback socket but carries that domain in
    ``Host``, so it is refused.

``CrossSiteGuard``
    Refuses state-changing requests that a browser marks as coming from another
    site. Without it, any visited page could POST to ``127.0.0.1:8080`` and
    clear the pool or spend provider credits.

``TokenGuard``
    Enforces ``ADMIN_TOKEN`` when one is configured, and is a no-op otherwise,
    so the default loopback setup needs no configuration.

These are plain ASGI middlewares rather than ``BaseHTTPMiddleware`` subclasses:
they never wrap the response body, which keeps streaming, background tasks and
WebSocket scopes untouched.

The proxy gateways (ports 8888/1080) speak HTTP CONNECT, SOCKS5 and raw bytes,
not this application's HTTP routes, and are deliberately not covered here. See
``docs/SECURITY.md`` for what exposing them means.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Names reserved for testing (RFC 6761 ``.test`` and the ASGI test-client
# default). Neither can be resolved by a browser, so accepting them cannot
# weaken the rebinding defence.
_ASGI_TEST_HOSTS = frozenset({"test", "testserver"})
_TOKEN_EXEMPT_PATHS = frozenset({"/healthz", "/login"})
_TOKEN_EXEMPT_PREFIXES = ("/static/",)

ADMIN_COOKIE = "proxy_rotator_admin"


def is_loopback(host: str) -> bool:
    """True when the value is a loopback name or address."""
    return host.strip().lower() in _LOOPBACK_HOSTS


def host_without_port(value: str) -> str:
    """Lower-cased host part of a ``Host`` header or origin, port removed."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def configured_hosts(bind_host: str, extra: Iterable[str]) -> frozenset[str]:
    """Host names this instance answers to."""
    hosts = set(_LOOPBACK_HOSTS) | set(_ASGI_TEST_HOSTS)
    hosts.add(host_without_port(bind_host))
    hosts.update(host_without_port(item) for item in extra)
    return frozenset(host for host in hosts if host)


def _unauthorised(message: str) -> Response:
    """Signal that a token is required, in the shape the caller expects."""
    return JSONResponse({"detail": message}, status_code=401)


class HostGuard:
    """Reject requests whose ``Host`` header is not configured."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = host_without_port(Headers(scope=scope).get("host", ""))
        if host not in self.allowed:
            logger.warning("admin_host_rejected host=%s path=%s", host or "-", scope.get("path"))
            response = PlainTextResponse("Invalid Host header\n", status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class CrossSiteGuard:
    """Reject state-changing requests a browser reports as cross-site."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
        origin = (headers.get("origin") or "").strip()
        rejected = ""
        if fetch_site == "cross-site":
            rejected = "sec-fetch-site"
        elif origin.lower() == "null":
            rejected = "null origin"
        elif origin:
            candidate = host_without_port(urlsplit(origin).netloc)
            if candidate not in self.allowed:
                rejected = f"origin {candidate or '-'}"
        if rejected:
            logger.warning(
                "admin_cross_site_rejected source=%s path=%s method=%s",
                rejected,
                scope.get("path"),
                scope["method"],
            )
            response = PlainTextResponse("Cross-site request refused\n", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class TokenGuard:
    """Require ``ADMIN_TOKEN`` on every route when one is configured."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token or None

    def _supplied(self, scope: Scope) -> str:
        headers = Headers(scope=scope)
        authorization = headers.get("authorization") or ""
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        raw = headers.get("cookie")
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is simply no credential
            return ""
        morsel = jar.get(ADMIN_COOKIE)
        return morsel.value if morsel is not None else ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.token is None:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path in _TOKEN_EXEMPT_PATHS or path.startswith(_TOKEN_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        supplied = self._supplied(scope)
        if supplied and secrets.compare_digest(supplied, self.token):
            await self.app(scope, receive, send)
            return
        logger.warning("admin_token_rejected path=%s", path)
        if path.startswith("/api/"):
            await _unauthorised("Admin token required")(scope, receive, send)
            return
        response = PlainTextResponse(
            "Jeton d'administration requis : ouvrez http://127.0.0.1:8080/login\n",
            status_code=401,
        )
        await response(scope, receive, send)
