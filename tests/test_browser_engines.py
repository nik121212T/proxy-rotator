"""Opt-in real engine tests: synthetic loopback proxies, never a user's pool."""

import asyncio
import base64
import contextlib
import os
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import Error

from app.browser_manager import BrowserManager
from app.config import Settings
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_ENGINES") != "1",
    reason="Opt-in real browsers: RUN_BROWSER_ENGINES=1 (Windows CI job)",
)


@pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_real_engine_proxy_tls_fail_closed_and_cleanup(db, tmp_path, monkeypatch, engine, authenticated):
    # A test-only certificate; ignore_https_errors is confined to this fixture.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.invalid")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .sign(private, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    direct_hits = []

    async def secure(request):
        return web.Response(text="VIA TLS PROXY", content_type="text/html")

    async def direct(request):
        direct_hits.append(request.path)
        return web.Response(text="DIRECT CONNECTION")

    tls_app, direct_app = web.Application(), web.Application()
    tls_app.router.add_get("/{path:.*}", secure)
    direct_app.router.add_get("/{path:.*}", direct)
    tls_runner, direct_runner = web.AppRunner(tls_app), web.AppRunner(direct_app)
    await tls_runner.setup()
    await direct_runner.setup()
    await web.TCPSite(tls_runner, "127.0.0.1", 0, ssl_context=tls).start()
    await web.TCPSite(direct_runner, "127.0.0.1", 0).start()
    observed = []
    handlers = set()

    async def copy(reader, writer):
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()

    async def upstream(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        remote = None
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            auth = b"Proxy-Authorization: Basic " + base64.b64encode(b"fixture:synthetic")
            has_auth = auth.lower() in headers.lower()
            observed.append((headers.startswith(b"CONNECT "), has_auth))
            if has_auth != authenticated:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            if headers.startswith(b"CONNECT "):
                target, remote = await asyncio.open_connection("127.0.0.1", tls_runner.addresses[0][1])
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(copy(reader, remote), copy(target, writer))
            else:
                body = b"VIA HTTP PROXY"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if remote:
                remote.close()
                with contextlib.suppress(OSError):
                    await remote.wait_closed()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    credentials = "fixture:synthetic@" if authenticated else ""
    proxy = await pool.add(parse_line(f"{credentials}127.0.0.1:{server.sockets[0].getsockname()[1]}"))
    await pool.mark_success(proxy.id, 10)
    settings = Settings(_env_file=None, master_secret="synthetic", burn_inspect_body=False)
    service = BrowserManager(pool, Rotator(), key, tmp_path / "profiles", headless=True, settings=settings)
    try:
        driver = await service._playwright()
        browser_type = getattr(driver, engine)

        async def launch_context(*args, **kwargs):
            return await browser_type.launch_persistent_context(*args, **kwargs, ignore_https_errors=True)

        async def test_driver():
            return SimpleNamespace(**{engine: SimpleNamespace(
                executable_path=browser_type.executable_path, launch_persistent_context=launch_context,
            )})

        monkeypatch.setattr(service, "_playwright", test_driver)
        # Fixed and rotation both use the real private gateway.
        for selection in (proxy.id, None):
            identifier = await service.launch("dedicated", engine, selection, "http://fixture.invalid/")
            session = service.sessions[identifier]
            page = session.context.pages[0]
            assert "VIA HTTP PROXY" in await page.content()
            await page.goto("https://fixture.invalid/", wait_until="domcontentloaded")
            assert "VIA TLS PROXY" in await page.content()
            # A reachable local origin makes silent direct fallback observable.
            target = f"http://127.0.0.1:{direct_runner.addresses[0][1]}/direct"
            await page.goto(target, wait_until="domcontentloaded")
            assert "VIA HTTP PROXY" in await page.content()
            assert not direct_hits
            await pool.quarantine(proxy.id)
            fresh = await session.context.new_page()
            with contextlib.suppress(Error):
                await fresh.goto(target + "?empty=1", wait_until="domcontentloaded", timeout=5000)
            assert not direct_hits
            port = int(session.gateway.status("").bound.rsplit(":", 1)[1])
            await service.close_session(identifier)
            assert not session.profile.exists()
            assert not session.context.browser.is_connected()
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)
            await pool.mark_success(proxy.id, 10)
        assert any(connect for connect, _ in observed)
        assert any(not connect for connect, _ in observed)
        assert all(auth == authenticated for _, auth in observed)
    finally:
        await service.stop()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await tls_runner.cleanup()
        await direct_runner.cleanup()
