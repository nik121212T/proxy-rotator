"""End-to-end coverage of the /api/providers/dichvusocks/* routes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from pydantic import SecretStr

from app.providers.dichvusocks import DichvusocksClient
from tests.test_dichvusocks_client import FakeDichvusocks


@pytest.fixture
async def provider_upstream() -> AsyncIterator[FakeDichvusocks]:
    server = FakeDichvusocks()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _wire_client(api, running_app, provider_upstream: FakeDichvusocks) -> None:
    """Inject a live DichvusocksClient aimed at the fake upstream."""
    app, _ = running_app
    app.state.runtime.dichvusocks = DichvusocksClient(
        token=SecretStr("TOKEN"),
        base_url=f"http://127.0.0.1:{provider_upstream.port}",
        timeout_s=2,
    )


# ── /status ────────────────────────────────────────────────────────────────


async def test_status_reports_disabled_when_no_client(api, running_app) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.dichvusocks = None
    response = await client.get("/api/providers/dichvusocks/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}


async def test_status_reports_enabled_when_token_is_set(api, running_app, provider_upstream):
    client, _ = api
    await _wire_client(api, running_app, provider_upstream)
    response = await client.get("/api/providers/dichvusocks/status")
    assert response.json() == {"enabled": True}


# ── /user-info surfaces 404 vs 502 ─────────────────────────────────────────


async def test_user_info_requires_client(api, running_app) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.dichvusocks = None
    response = await client.get("/api/providers/dichvusocks/user-info")
    assert response.status_code == 404


async def test_user_info_proxies_payload(api, running_app, provider_upstream) -> None:
    client, _ = api
    await _wire_client(api, running_app, provider_upstream)
    provider_upstream.set_response(
        "/user/info", web.json_response({"balance": 42, "ok": True})
    )
    response = await client.get("/api/providers/dichvusocks/user-info")
    assert response.status_code == 200
    assert response.json() == {"balance": 42, "ok": True}


async def test_user_info_maps_upstream_error_to_502(
    api, running_app, provider_upstream
) -> None:
    client, _ = api
    await _wire_client(api, running_app, provider_upstream)
    provider_upstream.set_status("/user/info", 401)
    response = await client.get("/api/providers/dichvusocks/user-info")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "Invalid" in detail
    assert detail.isascii() and len(detail) <= 160


# ── /token round-trip ──────────────────────────────────────────────────────


async def test_token_set_and_clear_round_trip(api, running_app, provider_upstream) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.dichvusocks = DichvusocksClient(
        token=None,
        base_url=f"http://127.0.0.1:{provider_upstream.port}",
        timeout_s=2,
    )
    assert (await client.get("/api/providers/dichvusocks/status")).json() == {
        "enabled": False
    }
    response = await client.post(
        "/api/providers/dichvusocks/token", json={"token": "new-token"}
    )
    assert response.status_code == 204
    assert (await client.get("/api/providers/dichvusocks/status")).json() == {
        "enabled": True
    }
    response = await client.request("DELETE", "/api/providers/dichvusocks/token")
    assert response.status_code == 204
    assert (await client.get("/api/providers/dichvusocks/status")).json() == {
        "enabled": False
    }


async def test_token_payload_rejects_unknown_fields(
    api, running_app, provider_upstream
) -> None:
    client, _ = api
    await _wire_client(api, running_app, provider_upstream)
    response = await client.post(
        "/api/providers/dichvusocks/token",
        json={"token": "ok", "extra": "nope"},
    )
    assert response.status_code == 422


# ── /preview and /import ───────────────────────────────────────────────────


async def test_preview_relays_filters_without_importing(
    api, running_app, provider_upstream
) -> None:
    client, _ = api
    await _wire_client(api, running_app, provider_upstream)
    seen: dict[str, str] = {}
    viewed: list[int] = []

    async def list_handler(request: web.Request) -> web.Response:
        seen.update({k: v for k, v in request.query.items()})
        return web.json_response(
            {"data": [{"id": 1, "ip": "1.1.1.1", "port": 1080}]}
        )

    async def view_handler(request: web.Request) -> web.Response:
        viewed.append(1)
        return web.json_response({"data": {}})

    provider_upstream.set_inline_handler("/socks/data", list_handler)
    provider_upstream.set_inline_handler("/socks/view", view_handler)

    response = await client.get(
        "/api/providers/dichvusocks/preview?country=FR&limit=3&page=1"
    )
    assert response.status_code == 200
    assert response.json()[0]["id"] == 1
    assert seen["country"] == "FR"
    assert seen["limit"] == "3"
    assert viewed == []  # preview NEVER calls view (would debit credit)


async def test_import_views_each_entry_and_adds_to_pool(
    api, running_app, provider_upstream
) -> None:
    client, _ = api
    app, _ = running_app
    await _wire_client(api, running_app, provider_upstream)

    async def list_handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"data": [{"id": 7, "ip": "203.0.113.7", "port": 1080}]}
        )

    async def view_handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": {
                    "id": 7,
                    "ip": "203.0.113.7",
                    "port": 1080,
                    "username": "proxy-user",
                    "password": "proxy-pass-321",
                    "countryCode": "FR",
                }
            }
        )

    provider_upstream.set_inline_handler("/socks/data", list_handler)
    provider_upstream.set_inline_handler("/socks/view", view_handler)

    response = await client.post(
        "/api/providers/dichvusocks/import?limit=1&country=FR"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 1
    assert app.state.runtime.pool.all()[0].host == "203.0.113.7"
