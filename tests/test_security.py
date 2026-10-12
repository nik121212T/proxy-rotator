"""Exposure guards: Host validation, cross-site refusal and the admin token."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2 as httpx
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.security import ADMIN_COOKIE


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "master_secret": SecretStr("api-test-master"),
        "gateway_http_port": 0,
        "gateway_socks_port": 0,
        "db_path": tmp_path / "proxies.db",
        "health_probe_url": "http://probe.invalid/probe",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def plain_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Loopback app with no admin token, as shipped by default."""
    app: FastAPI = create_app(_settings(tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client,
    ):
        yield client


@pytest_asyncio.fixture
async def token_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app: FastAPI = create_app(_settings(tmp_path, admin_token=SecretStr("s3cret-token")))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client,
    ):
        yield client


# ── Host validation (DNS rebinding) ─────────────────────────────────────────


async def test_foreign_host_header_is_refused(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.get("/api/pool/stats", headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert "Invalid Host" in response.text


async def test_loopback_host_names_are_accepted(plain_client: httpx.AsyncClient) -> None:
    for host in ("127.0.0.1:8080", "localhost", "localhost:8080", "[::1]:8080"):
        response = await plain_client.get("/api/pool/stats", headers={"Host": host})
        assert response.status_code == 200, host


async def test_host_check_ignores_the_port(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.get("/api/pool/stats", headers={"Host": "127.0.0.1:9"})
    assert response.status_code == 200


async def test_configured_extra_host_is_accepted(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, admin_allowed_hosts=["proxy.lan"]))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.lan:8080"
        ) as client,
    ):
        assert (await client.get("/api/pool/stats")).status_code == 200
        refused = await client.get("/api/pool/stats", headers={"Host": "other.lan"})
        assert refused.status_code == 400


# ── Cross-site refusal (CSRF) ───────────────────────────────────────────────


async def test_cross_site_post_is_refused(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.post(
        "/api/pool/clear-all", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403


async def test_foreign_origin_post_is_refused(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.post("/api/pool/clear-all", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


async def test_null_origin_post_is_refused(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.post("/api/pool/clear-all", headers={"Origin": "null"})
    assert response.status_code == 403


async def test_same_origin_post_is_allowed(plain_client: httpx.AsyncClient) -> None:
    response = await plain_client.post(
        "/api/pool/clear-all",
        headers={"Origin": "http://127.0.0.1:8080", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200


async def test_post_without_browser_headers_is_allowed(plain_client: httpx.AsyncClient) -> None:
    # curl and the test suite itself send neither Origin nor Sec-Fetch-Site.
    assert (await plain_client.post("/api/pool/clear-all")).status_code == 200


async def test_cross_site_get_is_not_blocked(plain_client: httpx.AsyncClient) -> None:
    # The guard is scoped to state-changing methods; a GET carries no side effect.
    response = await plain_client.get(
        "/api/pool/stats", headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"}
    )
    assert response.status_code == 200


# ── Admin token ─────────────────────────────────────────────────────────────


async def test_token_is_optional_by_default(plain_client: httpx.AsyncClient) -> None:
    assert (await plain_client.get("/api/pool/stats")).status_code == 200


async def test_missing_token_is_refused(token_client: httpx.AsyncClient) -> None:
    response = await token_client.get("/api/pool/stats")
    assert response.status_code == 401
    assert response.json() == {"detail": "Admin token required"}


async def test_wrong_token_is_refused(token_client: httpx.AsyncClient) -> None:
    response = await token_client.get(
        "/api/pool/stats", headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


async def test_bearer_token_grants_access(token_client: httpx.AsyncClient) -> None:
    response = await token_client.get(
        "/api/pool/stats", headers={"Authorization": "Bearer s3cret-token"}
    )
    assert response.status_code == 200


async def test_cookie_token_grants_access(token_client: httpx.AsyncClient) -> None:
    # Sent as a raw header: httpx deprecates per-request cookie jars.
    response = await token_client.get(
        "/api/pool/stats", headers={"Cookie": f"{ADMIN_COOKIE}=s3cret-token"}
    )
    assert response.status_code == 200


async def test_healthz_and_login_stay_reachable(token_client: httpx.AsyncClient) -> None:
    assert (await token_client.get("/healthz")).status_code == 200
    assert (await token_client.get("/login")).status_code == 200


async def test_login_sets_a_cookie_and_rejects_a_bad_token(
    token_client: httpx.AsyncClient,
) -> None:
    refused = await token_client.post("/login", content="token=nope")
    assert refused.status_code == 401
    assert ADMIN_COOKIE not in refused.cookies

    accepted = await token_client.post("/login", content="token=s3cret-token")
    assert accepted.status_code == 303
    assert accepted.cookies.get(ADMIN_COOKIE) == "s3cret-token"


async def test_login_redirects_when_no_token_is_configured(
    plain_client: httpx.AsyncClient,
) -> None:
    response = await plain_client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_export_still_works_on_loopback(plain_client: httpx.AsyncClient) -> None:
    """The documented export keeps working for the default local setup."""
    response = await plain_client.get("/api/proxies/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_export_filename_carries_a_utc_timestamp(
    plain_client: httpx.AsyncClient,
) -> None:
    """Successive exports must not collide in the user's downloads folder."""
    import re

    response = await plain_client.get("/api/proxies/export")
    disposition = response.headers["content-disposition"]
    match = re.search(r'filename="proxies-(alive|all)-(\d{8}-\d{6})\.txt"', disposition)
    assert match is not None, disposition
