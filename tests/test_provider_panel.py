"""Provider panel API checks without external calls or listening sockets."""

import httpx2 as httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app import provider_store
from app.health_checker import HealthChecker
from app.pool import Pool
from app.providers.dichvusocks import DichvusocksClient
from app.rotator import Rotator
from app.routes import providers
from app.runtime import Runtime


async def test_provider_token_lifecycle_is_persisted_and_never_echoed(db):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    runtime = Runtime(
        pool,
        HealthChecker(pool, key),
        Rotator(),
        "",
        "",
        fernet_key=key,
        db_conn=db,
        dichvusocks=DichvusocksClient(token=None),
    )
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(providers.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        base = "/api/providers/dichvusocks"
        assert (await client.get(base + "/status")).json() == {"enabled": False}
        assert (await client.get(base + "/user-info")).status_code == 404
        response = await client.post(base + "/token", json={"token": "synthetic-token"})
        assert response.status_code == 204
        assert response.content == b""
        stored = await provider_store.get_token(db, "dichvusocks", key)
        assert stored.get_secret_value() == "synthetic-token"
        runtime.dichvusocks = DichvusocksClient(token=stored)
        status = await client.get(base + "/status")
        assert status.json() == {"enabled": True}
        assert "synthetic-token" not in status.text
        assert (await client.delete(base + "/token")).status_code == 204
        assert await provider_store.get_token(db, "dichvusocks", key) is None
        assert (await client.get(base + "/status")).json() == {"enabled": False}
