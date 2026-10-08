from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.crypto import derive_key
from app.db import get_db
from app.main import create_app
from app.parser import parse_line
from app.pool import Pool
from app.salt import load_or_create_salt


async def test_lifespan_persists_pool_and_serves_health(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("test-master"),
        gateway_http_port=0,
        gateway_socks_port=0,
        db_path=tmp_path / "proxies.db",
    )
    key = derive_key(settings.master_secret, load_or_create_salt(tmp_path / ".salt"))
    async with get_db(settings.db_path) as conn:
        pool = await Pool.load(conn, key)
        proxy = await pool.add(parse_line("tester:fake@127.0.0.1:1"))
        await pool.quarantine(proxy.id)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert app.state.runtime is None
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.pool.all()[0].id == proxy.id
        assert restarted.state.pool.all()[0].status == "quarantine"
    assert (tmp_path / ".salt").exists()


async def test_wrong_master_secret_rejected_before_readiness(tmp_path: Path) -> None:
    from app.crypto import CryptoConfigError

    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("original"),
        gateway_http_port=0,
        gateway_socks_port=0,
        db_path=tmp_path / "proxies.db",
    )
    key = derive_key(settings.master_secret, load_or_create_salt(tmp_path / ".salt"))
    async with get_db(settings.db_path) as conn:
        pool = await Pool.load(conn, key)
        proxy = await pool.add(parse_line("tester:fake@127.0.0.1:1"))
        await pool.quarantine(proxy.id)
    wrong = settings.model_copy(update={"master_secret": SecretStr("incorrect")})
    app = create_app(wrong)
    with pytest.raises(CryptoConfigError, match="Cannot decrypt"):
        async with app.router.lifespan_context(app):
            pytest.fail("An unreadable credential store must not become ready")
    assert app.state.runtime is None


@pytest.mark.parametrize("secret", ["", "   ", "CHANGE_ME_NOW"])
def test_launcher_rejects_unconfigured_secret(tmp_path: Path, secret: str) -> None:
    import os
    import subprocess
    import sys

    env = dict(
        os.environ,
        MASTER_SECRET=secret,
        DB_PATH=str(tmp_path / "proxies.db"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and module, no shell
        [sys.executable, "-m", "app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 78
    assert not (tmp_path / "proxies.db").exists()
    assert not (tmp_path / ".salt").exists()


def test_settings_load_dotenv_with_safe_defaults(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MASTER_SECRET=synthetic-master\nHEALTH_CONCURRENCY=7\n" "ROTATION_STRATEGY=round_robin\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)
    assert settings.bind_host == "127.0.0.1"
    assert settings.admin_port == 8080
    assert settings.health_concurrency == 7
    assert settings.rotation_strategy == "round_robin"
    assert "synthetic-master" not in repr(settings)


async def test_healthz_is_unavailable_without_lifespan() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 503


async def test_lifespan_checks_imports_and_closes_database(tmp_path: Path) -> None:
    import asyncio

    from aiohttp import web

    requests = 0

    async def upstream(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response({"ip": "203.0.113.20"})

    upstream_app = web.Application()
    upstream_app.router.add_get("/probe", upstream)
    runner = web.AppRunner(upstream_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        settings = Settings(
            _env_file=None,
            master_secret=SecretStr("test-master"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
            health_probe_url="http://fake.invalid/probe",
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            pool = app.state.runtime.pool
            assert app.state.runtime.rotator.strategy == "weighted"
            await pool.add(parse_line(f"tester:fake@127.0.0.1:{port}"))
            async with asyncio.timeout(3):
                while not pool.active():
                    await asyncio.sleep(0.01)
            assert requests == 1
        with pytest.raises(ValueError, match="no active connection"):
            await pool.add(parse_line("another:fake@127.0.0.1:1"))
        await asyncio.sleep(0.25)
        assert requests == 1
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("url", ["file:///tmp/probe", "http://user:secret@host/", "invalid"])
def test_settings_reject_unsafe_probe_urls(url: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, master_secret=SecretStr("test-master"), health_probe_url=url)


async def test_launcher_reports_crypto_startup_failure_as_78(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    key = derive_key(SecretStr("original"), load_or_create_salt(tmp_path / ".salt"))
    async with get_db(tmp_path / "proxies.db") as conn:
        pool = await Pool.load(conn, key)
        proxy = await pool.add(parse_line("tester:synthetic-password@127.0.0.1:1"))
        await pool.quarantine(proxy.id)
    env = dict(
        os.environ,
        MASTER_SECRET="wrong-master",
        DB_PATH=str(tmp_path / "proxies.db"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter/module, no shell
        [sys.executable, "-m", "app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 78
    assert "synthetic-password" not in result.stderr
    assert "wrong-master" not in result.stderr


async def test_lifespan_warns_when_binding_to_lan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("test-master"),
        gateway_http_port=0,
        gateway_socks_port=0,
        db_path=tmp_path / "proxies.db",
        bind_host="0.0.0.0",  # noqa: S104 — verify warning only; no listener is opened
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert "event=lan_binding" in caplog.text


async def test_gateway_start_failure_cleans_started_services(tmp_path):
    import asyncio

    from app.main import services

    occupied = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    baseline = asyncio.all_tasks()
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("test-master"),
        db_path=tmp_path / "failure.db",
        gateway_http_port=0,
        gateway_socks_port=occupied.sockets[0].getsockname()[1],
    )
    leaked = set()
    try:
        with pytest.raises(OSError):
            async with services(settings):
                pytest.fail("Occupied SOCKS port must fail startup")
        await asyncio.sleep(0)
        leaked = asyncio.all_tasks() - baseline
        assert not leaked, "Startup failure must stop health checker and subscriptions"
    finally:
        occupied.close()
        await occupied.wait_closed()
        for task in leaked:
            task.cancel()
        await asyncio.gather(*leaked, return_exceptions=True)
