"""Verified TLS through a real CONNECT tunnel, using an ephemeral local CA."""

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiohttp import web
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.health_checker import HealthChecker
from app.parser import parse_line
from app.pool import Pool


async def test_https_probe_traverses_authenticated_connect(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "probe.invalid")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("probe.invalid"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "certificate.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))

    async def respond(request: web.Request) -> web.Response:
        assert request.secure
        return web.json_response({"ip": "203.0.113.55"})

    app = web.Application()
    app.router.add_get("/", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context).start()
    target_port = runner.addresses[0][1]
    observed: list[bool] = []
    handlers: set[asyncio.Task[None]] = set()

    async def copy(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
        try:
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()
        finally:
            destination.close()

    async def tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            observed.append(
                headers.startswith(b"CONNECT probe.invalid:443 ")
                and b"Proxy-Authorization: Basic " in headers
            )
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", target_port
            )
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(tunnel, "127.0.0.1", 0)
    try:
        async with server:
            key = Fernet.generate_key()
            pool = await Pool.load(db, key)
            port = server.sockets[0].getsockname()[1]
            proxy = await pool.add(parse_line(f"user:synthetic@127.0.0.1:{port}"))
            checker = HealthChecker(
                pool,
                key,
                probe_url="https://probe.invalid/",
                tls_context=client_context,
            )
            result = await checker.check_one(proxy.id)
            assert result.ok
            assert str(result.exit_ip) == "203.0.113.55"
            assert observed == [True]
            untrusted = await HealthChecker(
                pool,
                key,
                probe_url="https://probe.invalid/",
                timeout_s=1,
            ).check_one(proxy.id)
            assert not untrusted.ok
            assert untrusted.error == "Proxy connection failed"
    finally:
        await runner.cleanup()
        await asyncio.gather(*handlers)
