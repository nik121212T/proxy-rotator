"""Real, per-test databases; no SQLite mocks."""

import asyncio
import gc
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import httpx2 as httpx
import pytest
import pytest_asyncio
from aiohttp import web
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import Settings
from app.db import get_db
from app.main import create_app


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with get_db(tmp_path / "proxies.db") as conn:
        yield conn


@pytest_asyncio.fixture(autouse=True)
async def no_leaked_tasks() -> AsyncIterator[None]:
    import asyncio

    baseline = asyncio.all_tasks()
    yield
    await asyncio.sleep(0)
    gc.collect()
    leaked = asyncio.all_tasks() - baseline - {asyncio.current_task()}
    descriptions = [repr(task.get_coro()) for task in leaked]
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    assert not leaked, f"Background tasks survived teardown: {descriptions}"


@pytest.fixture
async def probe_control() -> tuple[asyncio.Event, asyncio.Event]:
    release, entered = asyncio.Event(), asyncio.Event()
    release.set()
    return release, entered


@pytest.fixture
async def running_app(
    tmp_path: Path, probe_control: tuple[asyncio.Event, asyncio.Event]
) -> AsyncIterator[tuple[FastAPI, int]]:
    async def upstream(request: web.Request) -> web.Response:
        probe_control[1].set()
        await probe_control[0].wait()
        return web.json_response({"ip": "203.0.113.30"})

    upstream_app = web.Application()
    upstream_app.router.add_get("/probe", upstream)
    runner = web.AppRunner(
        upstream_app, access_log=None, shutdown_timeout=0.1, handler_cancellation=True
    )
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("api-test-master"),
        gateway_http_port=0,
        gateway_socks_port=0,
        db_path=tmp_path / "proxies.db",
        health_probe_url="http://probe.invalid/probe",
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            yield app, runner.addresses[0][1]
    finally:
        await runner.cleanup()


@pytest.fixture
async def api(running_app: tuple[FastAPI, int]) -> AsyncIterator[tuple[httpx.AsyncClient, int]]:
    app, port = running_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, port


@pytest.fixture
def successful_candidate_probe():
    """Isolated UI/provider tests; network verification is covered separately."""
    from datetime import UTC, datetime

    from app.models import CheckResult

    async def probe(candidate):
        return CheckResult(
            proxy_id=1, ok=True, latency_ms=10, exit_ip="203.0.113.30",
            checked_at=datetime.now(UTC),
        )

    return probe
