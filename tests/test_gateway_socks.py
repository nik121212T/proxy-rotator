"""SOCKS5 inbound server tests (T5.3)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.db import get_db
from app.gateway_socks import SocksGateway
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator


@dataclass
class FakeHttpUpstream:
    """Minimal HTTP forward proxy supporting CONNECT.

    Used as the upstream for the SOCKS5 gateway (which translates inbound
    SOCKS5 into HTTP CONNECT or SOCKS5 toward the chosen upstream).
    """
    host: str = "127.0.0.1"
    port: int = 0
    expected_auth: str = ""
    connects: list[str] = field(default_factory=list)
    accept: bool = True
    reject_code: int = 502
    server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        self.server = None

    async def _handle(self, reader, writer):
        try:
            await self._serve(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _serve(self, reader, writer):
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        method, target, _ = line.rstrip(b"\r\n").decode().split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            raw = await reader.readuntil(b"\r\n")
            if raw == b"\r\n":
                break
            name, _, value = raw.rstrip(b"\r\n").decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        auth = headers.get("proxy-authorization", "")
        if self.expected_auth and auth != self.expected_auth:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if not self.accept:
            writer.write(f"HTTP/1.1 {self.reject_code} Bad\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            return
        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port_str = target.rpartition(":")
        self.connects.append(target)
        try:
            up_reader, up_writer = await asyncio.open_connection(host, int(port_str))
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(src, dst):
            try:
                while True:
                    chunk = await src.read(32768)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(ConnectionError, OSError):
                    if dst.can_write_eof():
                        dst.write_eof()

        try:
            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                up_writer.close()
                await up_writer.wait_closed()


@pytest_asyncio.fixture
async def upstream_http() -> AsyncIterator[FakeHttpUpstream]:
    fake = FakeHttpUpstream()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with get_db(tmp_path / "proxies.db") as conn:
        yield conn


@dataclass
class SocksHarness:
    gateway: SocksGateway
    pool: Pool
    port: int


@pytest_asyncio.fixture
async def socks_harness(db, upstream_http):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    rotator = Rotator(strategy="round_robin")
    proxy = await pool.add(
        parse_line(f"socksuser:secret-value@{upstream_http.host}:{upstream_http.port}")
    )
    await pool.mark_success(proxy.id, 10)
    upstream_http.expected_auth = (
        "Basic " + base64.b64encode(b"socksuser:secret-value").decode()
    )
    gateway = SocksGateway(pool, rotator, key, port=0, max_retry_per_request=1)
    await gateway.start()
    bound = gateway.status("configured").bound or ""
    port = int(bound.rsplit(":", 1)[1])
    try:
        yield SocksHarness(gateway, pool, port)
    finally:
        await gateway.stop()


async def _socks_connect(
    gateway_port: int, target_host: str, target_port: int, atyp: str = "domain"
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
    # Greeting: ver=5, nmethods=1, methods=[NO AUTH]
    writer.write(bytes([0x05, 0x01, 0x00]))
    await writer.drain()
    ver, method = await reader.readexactly(2)
    assert ver == 0x05
    assert method == 0x00
    # Request
    body = bytes([0x05, 0x01, 0x00])  # VER, CMD=CONNECT, RSV
    if atyp == "domain":
        name = target_host.encode("ascii")
        body += bytes([0x03, len(name)]) + name
    elif atyp == "ipv4":
        parts = [int(p) for p in target_host.split(".")]
        body += bytes([0x01, *parts])
    else:
        raise ValueError(atyp)
    body += struct.pack("!H", target_port)
    writer.write(body)
    await writer.drain()
    reply = await reader.readexactly(10)  # ver + rep + rsv + atyp(1) + ipv4(4) + port(2)
    assert reply[0] == 0x05
    return reader, writer, reply[1]


async def _echo_server() -> tuple[asyncio.base_events.Server, int]:
    async def handle(reader, writer):
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]  # type: ignore[index]
    return server, port


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_binds_and_reports_status(socks_harness):
    status = socks_harness.gateway.status("127.0.0.1:9999")
    assert status.bound == f"127.0.0.1:{socks_harness.port}"
    assert status.active_conns == 0


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_tunnel_through_http_upstream(socks_harness, upstream_http):
    echo, echo_port = await _echo_server()
    try:
        reader, writer, rep = await _socks_connect(
            socks_harness.port, "127.0.0.1", echo_port, atyp="ipv4"
        )
        assert rep == 0x00
        writer.write(b"hello-socks")
        await writer.drain()
        got = await reader.readexactly(len(b"hello-socks"))
        assert got == b"hello-socks"
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        assert any(target.endswith(f":{echo_port}") for target in upstream_http.connects)
    finally:
        echo.close()
        await echo.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_domain_atyp_passes_through(socks_harness, upstream_http):
    echo, echo_port = await _echo_server()
    try:
        reader, writer, rep = await _socks_connect(
            socks_harness.port, "localhost", echo_port, atyp="domain"
        )
        assert rep == 0x00
        writer.write(b"domain-pass")
        await writer.drain()
        got = await reader.readexactly(len(b"domain-pass"))
        assert got == b"domain-pass"
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        # upstream CONNECT target used the domain name (DNS deferred to upstream)
        assert any(target.startswith("localhost:") for target in upstream_http.connects)
    finally:
        echo.close()
        await echo.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_rejects_unsupported_method(socks_harness):
    reader, writer = await asyncio.open_connection("127.0.0.1", socks_harness.port)
    # Only offer GSSAPI method (not NO AUTH)
    writer.write(bytes([0x05, 0x01, 0x01]))
    await writer.drain()
    ver, method = await reader.readexactly(2)
    assert ver == 0x05
    assert method == 0xFF
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_pool_empty_returns_general_failure(db):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    gateway = SocksGateway(pool, Rotator(strategy="round_robin"), key, port=0)
    await gateway.start()
    try:
        port = int((gateway.status("x").bound or "").rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(bytes([0x05, 0x01, 0x00]))
        await writer.drain()
        ver, method = await reader.readexactly(2)
        assert method == 0x00
        # CONNECT request to any target
        req = bytes([0x05, 0x01, 0x00, 0x01, 1, 2, 3, 4]) + struct.pack("!H", 80)
        writer.write(req)
        await writer.drain()
        reply = await reader.readexactly(10)
        assert reply[0] == 0x05
        assert reply[1] in {0x01, 0x04}  # general failure or host unreachable
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
    finally:
        await gateway.stop()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_rejects_bind_command(socks_harness):
    reader, writer = await asyncio.open_connection("127.0.0.1", socks_harness.port)
    writer.write(bytes([0x05, 0x01, 0x00]))
    await writer.drain()
    await reader.readexactly(2)
    # CMD=BIND (0x02) not supported
    req = bytes([0x05, 0x02, 0x00, 0x01, 127, 0, 0, 1]) + struct.pack("!H", 80)
    writer.write(req)
    await writer.drain()
    reply = await reader.readexactly(10)
    assert reply[1] == 0x07  # COMMAND NOT SUPPORTED
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_socks5_marks_success_on_tunnel_close(socks_harness):
    echo, echo_port = await _echo_server()
    try:
        before = socks_harness.pool.all()[0].success_count
        reader, writer, rep = await _socks_connect(
            socks_harness.port, "127.0.0.1", echo_port, atyp="ipv4"
        )
        assert rep == 0x00
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        # Wait for background task to finish mark_success.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if socks_harness.pool.all()[0].success_count > before:
                break
        after = socks_harness.pool.all()[0].success_count
        assert after == before + 1
    finally:
        echo.close()
        await echo.wait_closed()


pytestmark = pytest.mark.asyncio
