"""Browser sessions must route through owned gateways without touching user profiles."""

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.browser_catalog import InstalledBrowser, firefox_preferences, native_arguments
from app.browser_manager import BrowserError, BrowserManager, FixedRotator
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator


def test_native_profiles_have_mandatory_proxy_and_no_direct_fallback(tmp_path):
    chrome = InstalledBrowser("chrome", "Chrome", "chromium", Path("/synthetic/chrome"))
    args = native_arguments(chrome, tmp_path, 12345, "https://example.invalid")
    assert f"--user-data-dir={tmp_path}" in args
    assert "--proxy-server=http://127.0.0.1:12345" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    firefox = InstalledBrowser("firefox", "Firefox", "firefox", Path("/synthetic/firefox"))
    args = native_arguments(firefox, tmp_path, 12345, "https://example.invalid")
    assert "-no-remote" in args and "-profile" in args
    prefs = firefox_preferences(12345)
    assert prefs["network.proxy.failover_direct"] is False
    assert prefs["network.proxy.ssl_port"] == 12345
    assert '"network.proxy.no_proxies_on", ""' in (tmp_path / "user.js").read_text()


async def test_fixed_browser_never_falls_back_to_another_proxy(db):
    pool = await Pool.load(db, Fernet.generate_key())
    first = await pool.add(parse_line("first:fake@first.invalid:80"))
    second = await pool.add(parse_line("second:fake@second.invalid:80"))
    await pool.mark_success(first.id, 10)
    await pool.mark_success(second.id, 10)
    rotator = FixedRotator(first.id)
    assert rotator.pick(pool).id == first.id
    await pool.quarantine(first.id)
    assert rotator.pick(pool) is None


async def test_browser_launch_rejects_empty_pool_and_unsafe_url(db, tmp_path):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    manager = BrowserManager(pool, Rotator(), key, tmp_path / "profiles")
    for url in ["file:///etc/passwd", "javascript:alert(1)", "https://user:secret@example.invalid"]:
        with pytest.raises(BrowserError, match="URL"):
            await manager.launch("dedicated", "chromium", None, url)
    with pytest.raises(BrowserError, match="Aucun proxy"):
        await manager.launch("dedicated", "chromium", None, "https://example.invalid")
    assert not (tmp_path / "profiles").exists() or not list((tmp_path / "profiles").iterdir())
    await manager.stop()


async def test_failed_native_launch_cleans_gateway_and_profile(db, tmp_path, monkeypatch):
    from app import browser_manager

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line("proxy.invalid:80"))
    await pool.mark_success(proxy.id, 10)
    monkeypatch.setattr(browser_manager, "discover_installed", lambda: [
        InstalledBrowser("chrome", "Chrome", "chromium", Path("/does-not-exist/browser")),
    ])
    manager = BrowserManager(pool, Rotator(), key, tmp_path / "profiles")
    with pytest.raises(BrowserError, match="Ouverture impossible"):
        await manager.launch("installed", "chrome", proxy.id, "https://example.invalid")
    assert not manager.sessions
    assert not (tmp_path / "profiles").exists() or not list((tmp_path / "profiles").iterdir())
    await manager.stop()


async def test_closing_native_session_stops_its_private_gateway(db, tmp_path, monkeypatch):
    import sys

    from app import browser_manager

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line("proxy.invalid:80"))
    await pool.mark_success(proxy.id, 10)
    monkeypatch.setattr(browser_manager, "discover_installed", lambda: [
        InstalledBrowser("chrome", "Chrome", "chromium", Path(sys.executable)),
    ])
    monkeypatch.setattr(browser_manager, "native_arguments", lambda *args: [
        sys.executable, "-c", "import time; time.sleep(60)",
    ])
    manager = BrowserManager(pool, Rotator(), key, tmp_path / "profiles")
    identifier = await manager.launch("installed", "chrome", proxy.id, "https://example.invalid")
    session = manager.sessions[identifier]
    bound = session.gateway.status("").bound
    port = int(bound.rsplit(":", 1)[1])
    await pool.quarantine(proxy.id)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET http://origin.invalid/ HTTP/1.1\r\nHost: origin.invalid\r\n\r\n")
    await writer.drain()
    assert b"503" in await reader.readline()
    writer.close()
    await writer.wait_closed()
    await manager.close_session(identifier)
    assert session.process.returncode is not None
    assert not manager.sessions and not session.profile.exists()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    await manager.stop()


async def test_browser_api_validates_choices_and_rolls_back_partial_launch(db, tmp_path, monkeypatch):
    import httpx2 as httpx
    from fastapi import FastAPI

    from app.health_checker import HealthChecker
    from app.routes import browsers
    from app.runtime import Runtime

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    for host in ("one.invalid", "two.invalid"):
        proxy = await pool.add(parse_line(f"{host}:80"))
        await pool.mark_success(proxy.id, 10)
    service = BrowserManager(pool, Rotator(), key, tmp_path / "profiles")
    runtime = Runtime(pool, HealthChecker(pool, key), Rotator(), "", "", browsers=service)
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(browsers.router)
    closed = []

    async def launch(source, browser_id, proxy_id, url):
        if proxy_id == 2:
            raise BrowserError("Ouverture impossible.")
        return "owned-session"

    async def close(identifier):
        closed.append(identifier)

    monkeypatch.setattr(service, "launch", launch)
    monkeypatch.setattr(service, "close_session", close)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"source": "dedicated", "browser_id": "chromium", "mode": "fixed", "proxy_ids": [1, 2]}
        response = await client.post("/api/browsers/sessions", json=body)
        assert response.status_code == 409
        assert closed == ["owned-session"]
        assert (await client.post("/api/browsers/install/not-an-engine")).status_code == 422
        body["proxy_ids"] = []
        assert (await client.post("/api/browsers/sessions", json=body)).status_code == 422
        page = await client.get("/navigateurs")
        assert page.status_code == 200
        assert "browser_controls.js" in page.text
        assert "Profils temporaires isolés" in page.text
    await service.stop()


async def test_engine_download_runs_once_and_shutdown_cancels_it(db, tmp_path, monkeypatch):
    import sys

    from app import browser_manager

    pool = await Pool.load(db, Fernet.generate_key())
    service = BrowserManager(pool, Rotator(), Fernet.generate_key(), tmp_path / "profiles")
    original = asyncio.create_subprocess_exec
    processes = []

    async def subprocess(*args, **kwargs):
        assert args[1:4] == ("-m", "playwright", "install")
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(browser_manager.asyncio, "create_subprocess_exec", subprocess)
    service.install("firefox")
    service.install("firefox")
    async with asyncio.timeout(3):
        while not processes:
            await asyncio.sleep(0.01)
    await service.stop()
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert not service._install_tasks
