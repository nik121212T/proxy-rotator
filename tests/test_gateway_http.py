"""HTTP forward proxy tests (T5.1)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography.fernet import Fernet

from app.db import get_db
from app.gateway_http import HttpGateway
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator


@dataclass
class FakeOrigin:
    port: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    body: bytes = b"origin ok"

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.rel_url.path_qs,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": await request.read(),
            }
        )
        return web.Response(status=self.status, body=self.body)


@pytest_asyncio.fixture
async def origin() -> AsyncIterator[FakeOrigin]:
    fake = FakeOrigin()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1, handler_cancellation=True)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        fake.port = runner.addresses[0][1]
        yield fake
    finally:
        await runner.cleanup()


@dataclass
class FakeUpstream:
    host: str = "127.0.0.1"
    port: int = 0
    expected_auth: str = ""
    auth_seen: list[str] = field(default_factory=list)
    connects: list[str] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    received_headers: list[dict[str, str]] = field(default_factory=list)
    accept: bool = True
    reject_status: tuple[int, str] = (502, "Bad Gateway")
    server: asyncio.base_events.Server | None = None
    handlers: set[asyncio.Task[None]] = field(default_factory=set)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._spawn, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        # Own the accepted handlers: a client that keeps its socket open would
        # otherwise leave them pending when the event loop shuts down.
        pending = list(self.handlers)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.handlers.clear()
        self.server = None

    def _spawn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    async def _read_headers(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]]:
        line = await reader.readuntil(b"\r\n")
        method, target, _ = line.rstrip(b"\r\n").decode().split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            raw = await reader.readuntil(b"\r\n")
            if raw == b"\r\n":
                break
            name, _, value = raw.rstrip(b"\r\n").decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        return method.upper(), target, headers

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                # A peer that never completes the close would otherwise leave
                # this handler pending when the loop shuts down.
                await asyncio.wait_for(writer.wait_closed(), 1)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, target, headers = await self._read_headers(reader)
        except asyncio.IncompleteReadError:
            writer.close()
            return
        auth = headers.get("proxy-authorization", "")
        self.auth_seen.append(auth)
        self.received_headers.append(headers)
        if self.expected_auth and auth != self.expected_auth:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if not self.accept:
            code, phrase = self.reject_status
            writer.write(f"HTTP/1.1 {code} {phrase}\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            return
        if method == "CONNECT":
            await self._tunnel(target, reader, writer)
        else:
            await self._forward_http(method, target, headers, reader, writer)

    async def _tunnel(self, target, client_reader, client_writer):
        host, _, port_str = target.rpartition(":")
        self.connects.append(target)
        try:
            up_reader, up_writer = await asyncio.open_connection(host, int(port_str))
        except OSError:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            client_writer.close()
            return
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

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
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except (ConnectionError, OSError):
                    pass

        try:
            await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))
        finally:
            up_writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await up_writer.wait_closed()

    async def _forward_http(self, method, target, headers, reader, writer):
        self.gets.append(target)
        passthrough = {
            k: v
            for k, v in headers.items()
            if k not in {"proxy-authorization", "proxy-connection", "host"}
        }
        body = b""
        if "content-length" in headers:
            size = int(headers["content-length"])
            if size:
                body = await reader.readexactly(size)
        async with (
            aiohttp.ClientSession(trust_env=False) as session,
            session.request(method, target, headers=passthrough, data=body) as resp,
        ):
            content = await resp.read()
            writer.write(f"HTTP/1.1 {resp.status} {resp.reason or ''}\r\n".encode())
            writer.write(f"Content-Length: {len(content)}\r\n".encode())
            for name, value in resp.headers.items():
                if name.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                writer.write(f"{name}: {value}\r\n".encode())
            writer.write(b"\r\n")
            writer.write(content)
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


@pytest_asyncio.fixture
async def upstream_proxy() -> AsyncIterator[FakeUpstream]:
    fake = FakeUpstream()
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
class GatewayHarness:
    gateway: HttpGateway
    pool: Pool
    rotator: Rotator
    key: bytes
    port: int


@pytest_asyncio.fixture
async def harness(db, upstream_proxy):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    rotator = Rotator(strategy="round_robin")
    proxy = await pool.add(
        parse_line(f"gatewayuser:secret-value@{upstream_proxy.host}:{upstream_proxy.port}")
    )
    await pool.mark_success(proxy.id, 10)
    upstream_proxy.expected_auth = "Basic " + base64.b64encode(b"gatewayuser:secret-value").decode()
    gateway = HttpGateway(pool, rotator, key, port=0)
    await gateway.start()
    bound = gateway.status("configured").bound or ""
    port = int(bound.rsplit(":", 1)[1])
    try:
        yield GatewayHarness(gateway, pool, rotator, key, port)
    finally:
        await gateway.stop()


async def _raw_request(gateway_port, first_line, headers, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
    writer.write(first_line.encode() + b"\r\n")
    for name, value in headers:
        writer.write(f"{name}: {value}\r\n".encode())
    writer.write(b"\r\n")
    if body:
        writer.write(body)
    await writer.drain()
    status_line = await reader.readuntil(b"\r\n")
    code = int(status_line.split(b" ", 2)[1])
    reply_headers: dict[str, str] = {}
    while True:
        line = await reader.readuntil(b"\r\n")
        if line == b"\r\n":
            break
        name, _, value = line.rstrip(b"\r\n").decode().partition(":")
        reply_headers[name.strip().lower()] = value.strip()
    content = b""
    length = reply_headers.get("content-length")
    if length is not None:
        remaining = int(length)
        while remaining:
            chunk = await reader.read(remaining)
            if not chunk:
                break
            content += chunk
            remaining -= len(chunk)
    elif reply_headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.strip(), 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            content += await reader.readexactly(size)
            await reader.readexactly(2)
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()
    return code, reply_headers, content


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_gateway_binds_and_reports_status(harness):
    status = harness.gateway.status("127.0.0.1:9999")
    assert status.bound == f"127.0.0.1:{harness.port}"
    assert status.active_conns == 0


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_forward_get_through_upstream_with_via(harness, origin, upstream_proxy):
    code, headers, body = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/path?x=1 HTTP/1.1",
        [
            ("Host", f"127.0.0.1:{origin.port}"),
            ("User-Agent", "test-client/1.0"),
            ("Connection", "close"),
            ("Keep-Alive", "timeout=5"),
        ],
    )
    assert code == 200
    assert body == b"origin ok"
    assert "via" in headers and "proxy-rotator" in headers["via"]
    # Gateway closes after one request/response and advertises it (audit A12).
    assert headers.get("connection", "").lower() == "close"
    assert upstream_proxy.auth_seen[-1].startswith("Basic ")
    assert origin.requests[-1]["headers"].get("connection") != "close"
    assert "via" in origin.requests[-1]["headers"]


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_connect_tunnel_bridges_bytes(harness, upstream_proxy):
    async def echo(reader, writer):
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

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]  # type: ignore[index]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", harness.port)
        writer.write(
            f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{echo_port}\r\n\r\n".encode()
        )
        await writer.drain()
        status_line = await reader.readuntil(b"\r\n")
        assert b"200" in status_line
        while (await reader.readuntil(b"\r\n")) != b"\r\n":
            pass
        writer.write(b"hello-tunnel")
        await writer.drain()
        got = await reader.readexactly(len(b"hello-tunnel"))
        assert got == b"hello-tunnel"
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        assert any(target.endswith(f":{echo_port}") for target in upstream_proxy.connects)
    finally:
        echo_server.close()
        await echo_server.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_pool_empty_returns_503(db, upstream_proxy):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    gateway = HttpGateway(pool, Rotator(strategy="round_robin"), key, port=0)
    await gateway.start()
    try:
        port = int((gateway.status("x").bound or "").rsplit(":", 1)[1])
        code, headers, body = await _raw_request(
            port,
            "GET http://example.invalid/ HTTP/1.1",
            [("Host", "example.invalid"), ("Connection", "close")],
        )
        assert code == 503
        assert b"Pool Empty" in body
        assert headers.get("via", "").endswith("proxy-rotator")
    finally:
        await gateway.stop()


async def _wait_idle(gateway, timeout=1.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if gateway.status("x").active_conns == 0:
            await asyncio.sleep(0.01)
            return
        await asyncio.sleep(0.005)


async def _gateway_for_upstream(
    db,
    upstream_port,
    *,
    timeout_s=15.0,
    max_retry_per_request=2,
    scheme="http",
    username="gatewayuser",
    password="secret-value",  # noqa: S107 - fixture credential, never a real secret
    max_connections=256,
):
    """Gateway with a single proxy aimed at a locally controlled upstream."""
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(
        parse_line(f"{scheme}://{username}:{password}@127.0.0.1:{upstream_port}")
    )
    await pool.mark_success(proxy.id, 10)
    gateway = HttpGateway(
        pool,
        Rotator(strategy="round_robin"),
        key,
        port=0,
        timeout_s=timeout_s,
        max_retry_per_request=max_retry_per_request,
        max_connections=max_connections,
    )
    await gateway.start()
    bound = gateway.status("configured").bound or ""
    return gateway, pool, int(bound.rsplit(":", 1)[1])


@dataclass
class FakeSocks5Upstream:
    """Minimal RFC 1928/1929 SOCKS5 server that records the credentials it sees.

    Refuses to hand back the tunnel unless username/password authentication
    succeeds, so a mangled credential cannot pass unnoticed.
    """

    host: str = "127.0.0.1"
    port: int = 0
    username: str = ""
    password: str = ""
    version_seen: int = 0
    server: asyncio.base_events.Server | None = None
    handlers: set[asyncio.Task[None]] = field(default_factory=set)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._spawn, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]  # type: ignore[index]

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        pending = list(self.handlers)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.handlers.clear()
        self.server = None

    def _spawn(self, reader, writer) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    async def _handle(self, reader, writer) -> None:
        try:
            self.version_seen, nmethods = await reader.readexactly(2)
            await reader.readexactly(nmethods)
            writer.write(bytes([5, 2]))  # offer username/password authentication
            await writer.drain()

            await reader.readexactly(1)  # auth version
            ulen = (await reader.readexactly(1))[0]
            self.username = (await reader.readexactly(ulen)).decode()
            plen = (await reader.readexactly(1))[0]
            self.password = (await reader.readexactly(plen)).decode()
            writer.write(bytes([1, 0]))  # authentication succeeded
            await writer.drain()

            header = await reader.readexactly(4)  # version, command, reserved, atyp
            if header[3] == 1:
                await reader.readexactly(4)
            elif header[3] == 3:
                await reader.readexactly((await reader.readexactly(1))[0])
            elif header[3] == 4:
                await reader.readexactly(16)
            await reader.readexactly(2)  # port
            writer.write(bytes([5, 0, 0, 1, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
            writer.write(b"TUNNEL-OK")
            await writer.drain()
            await asyncio.sleep(0.5)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, IndexError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)


async def _read_raw_response(port, request, timeout=5.0):
    """Send raw bytes and collect everything the gateway writes back."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    with contextlib.suppress(ConnectionError, OSError):
        # A rejected oversized request can close the socket while it is still
        # being written; the response is what the assertions care about.
        writer.write(request)
        await writer.drain()
    data = b""
    with contextlib.suppress(TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError):
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
            if not chunk:
                break
            data += chunk
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()
    return data


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_upstream_refuses_marks_failure_and_returns_502(harness, upstream_proxy, origin):
    # Force a transport-level failure by stopping the upstream before the request.
    await upstream_proxy.stop()
    before_fails = harness.pool.all()[0].fail_count
    code, _, _ = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/ HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert code == 502
    await _wait_idle(harness.gateway)
    after = harness.pool.all()[0]
    assert after.fail_count == before_fails + 1


async def _request_single_response(host: str, port: int, raw_bytes: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(raw_bytes)
        await writer.drain()
        return await reader.read()  # drain to EOF for clean shutdown
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
        # Ensure transport is released before pytest's warning-as-error check.
        if (transport := getattr(writer, "transport", None)) is not None:
            transport.abort()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_malformed_request_returns_400(harness):
    data = await _request_single_response("127.0.0.1", harness.port, b"NOT a request\r\n\r\n")
    assert data.startswith(b"HTTP/1.1 400")
    await _wait_idle(harness.gateway)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_unsupported_method_returns_405(harness):
    data = await _request_single_response(
        "127.0.0.1",
        harness.port,
        b"TRACE http://example.invalid/ HTTP/1.1\r\nHost: x\r\n\r\n",
    )
    assert data.startswith(b"HTTP/1.1 405")
    await _wait_idle(harness.gateway)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_relative_target_rejected(harness):
    code, _, _ = await _raw_request(
        harness.port,
        "GET /relative HTTP/1.1",
        [("Host", "example.invalid"), ("Connection", "close")],
    )
    assert code == 400


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_hop_by_hop_from_connection_header_is_stripped(harness, origin, upstream_proxy):
    code, _, _ = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/x HTTP/1.1",
        [
            ("Host", f"127.0.0.1:{origin.port}"),
            ("X-Secret", "pet-the-cat"),
            ("Connection", "x-secret, close"),
        ],
    )
    assert code == 200
    seen = origin.requests[-1]["headers"]
    assert "x-secret" not in seen
    assert seen.get("connection") != "close"


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_successful_forward_marks_success(harness, origin):
    before = harness.pool.all()[0]
    code, _, _ = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/ HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert code == 200
    await _wait_idle(harness.gateway)
    after = harness.pool.all()[0]
    assert after.success_count == before.success_count + 1


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_requests_total_increments(harness, origin):
    assert harness.gateway.status("x").requests_total == 0
    await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/ HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert harness.gateway.status("x").requests_total >= 1


pytestmark = pytest.mark.asyncio


# ──────────────────────────────────────────────────────────────────────────
# T5.2 — retry + burn classification
# ──────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def two_proxy_harness(db, upstream_proxy):
    """Harness with two upstream proxies in the pool for retry tests."""
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    rotator = Rotator(strategy="round_robin")
    first = await pool.add(
        parse_line(f"gatewayuser:secret-value@{upstream_proxy.host}:{upstream_proxy.port}")
    )
    # Spin up a second fake upstream that always replies 200 ok.
    second = FakeUpstream()
    await second.start()
    second.expected_auth = "Basic " + base64.b64encode(b"gatewayuser:secret-value").decode()
    second_proxy = await pool.add(
        parse_line(f"gatewayuser:secret-value@{second.host}:{second.port}")
    )
    await pool.mark_success(first.id, 10)
    await pool.mark_success(second_proxy.id, 10)
    upstream_proxy.expected_auth = "Basic " + base64.b64encode(b"gatewayuser:secret-value").decode()
    gateway = HttpGateway(pool, rotator, key, port=0, max_retry_per_request=2)
    await gateway.start()
    bound = gateway.status("configured").bound or ""
    port = int(bound.rsplit(":", 1)[1])
    try:
        yield GatewayHarness(gateway, pool, rotator, key, port), second, first.id, second_proxy.id
    finally:
        await gateway.stop()
        await second.stop()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_retry_on_upstream_connect_error_then_success(
    two_proxy_harness, upstream_proxy, origin
):
    harness, _, _, _ = two_proxy_harness
    # Round-robin picks the first proxy, whose upstream is now closed: only a
    # real retry on the second proxy can produce a 200 for this request.
    await upstream_proxy.stop()
    code, _, body = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/retry HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert code == 200
    assert body == b"origin ok"


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_retry_on_burn_status_then_success(two_proxy_harness, upstream_proxy, origin):
    harness, _, _, _ = two_proxy_harness
    # The first proxy answers 429 (a burn signal) without reaching the origin:
    # only a retry on the second proxy can return the real 200.
    upstream_proxy.accept = False
    upstream_proxy.reject_status = (429, "Too Many Requests")
    code, _, body = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/burn HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert code == 200
    assert body == b"origin ok"
    assert len(upstream_proxy.received_headers) == 1


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_non_idempotent_request_is_not_retried_on_burn(
    two_proxy_harness, upstream_proxy, origin
):
    harness, _, _, _ = two_proxy_harness
    upstream_proxy.accept = False
    upstream_proxy.reject_status = (429, "Too Many Requests")
    code, _, _ = await _raw_request(
        harness.port,
        f"POST http://127.0.0.1:{origin.port}/order HTTP/1.1",
        [
            ("Host", f"127.0.0.1:{origin.port}"),
            ("Content-Length", "4"),
            ("Connection", "close"),
        ],
        body=b"data",
    )
    # A POST must never be replayed once the origin may have applied it, but the
    # origin's real status must still reach the client instead of a gateway 502.
    assert code == 429
    assert len(upstream_proxy.received_headers) == 1
    await _wait_idle(harness.gateway)
    assert harness.pool.all()[0].fail_count == 1


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_retry_exhausted_returns_502(harness, upstream_proxy, origin):
    upstream_proxy.accept = False
    upstream_proxy.reject_status = (429, "Too Many Requests")
    code, _, body = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/burn HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    # Only one proxy in the pool; retries hit the same (burned) upstream, so
    # the gateway returns 502 after exhausting attempts.
    assert code == 502
    assert b"Bad Gateway" in body


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_no_retry_on_2xx(harness, upstream_proxy, origin):
    before_success = harness.pool.all()[0].success_count
    code, _, _ = await _raw_request(
        harness.port,
        f"GET http://127.0.0.1:{origin.port}/ok HTTP/1.1",
        [("Host", f"127.0.0.1:{origin.port}"), ("Connection", "close")],
    )
    assert code == 200
    await _wait_idle(harness.gateway)
    after = harness.pool.all()[0]
    assert after.success_count == before_success + 1
    # A 2xx must be served by exactly one upstream attempt.
    assert len(upstream_proxy.received_headers) == 1


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_partial_upstream_body_is_never_retried(db):
    """A response already on the wire must not be followed by a second one."""

    async def truncated(reader, writer):
        with contextlib.suppress(Exception):
            while (await reader.readuntil(b"\r\n")) != b"\r\n":
                continue
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n"
                b"Content-Type: application/octet-stream\r\n\r\n" + b"A" * 100
            )
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(truncated, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    gateway, pool, port = await _gateway_for_upstream(db, upstream_port, max_retry_per_request=3)
    try:
        data = await _read_raw_response(
            port, b"GET http://example.invalid/x HTTP/1.1\r\nHost: example.invalid\r\n\r\n"
        )
        # Exactly one status line, even though three retries were authorised.
        assert data.count(b"HTTP/1.1 ") == 1
        assert data.startswith(b"HTTP/1.1 200 OK")
        # The interrupted stream is an upstream failure, so it is recorded once.
        await _wait_idle(gateway)
        assert pool.all()[0].fail_count == 1
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_slow_but_progressing_response_is_not_cut_by_the_timeout(db):
    """The per-read budget must not act as a cap on total transfer duration."""
    body_chunks = (b"alpha", b"beta", b"gamma")

    async def slow(reader, writer):
        with contextlib.suppress(Exception):
            while (await reader.readuntil(b"\r\n")) != b"\r\n":
                continue
            total = sum(len(chunk) for chunk in body_chunks)
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Length: {total}\r\n"
                "Content-Type: application/octet-stream\r\n\r\n".encode()
            )
            for chunk in body_chunks:
                await asyncio.sleep(0.15)
                writer.write(chunk)
                await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(slow, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    # The transfer spans ~0.45 s, well beyond the 0.2 s per-read budget, while
    # every individual gap stays inside it.
    gateway, pool, port = await _gateway_for_upstream(db, upstream_port, timeout_s=0.2)
    try:
        data = await _read_raw_response(
            port, b"GET http://example.invalid/slow HTTP/1.1\r\nHost: example.invalid\r\n\r\n"
        )
        assert data.count(b"HTTP/1.1 ") == 1
        assert data.endswith(b"alphabetagamma")
        await _wait_idle(gateway)
        assert pool.all()[0].fail_count == 0
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_rejects_content_length_with_transfer_encoding(harness):
    """RFC 9112 section 6.1: ambiguous framing must be refused."""
    code, _, _ = await _raw_request(
        harness.port,
        "POST http://127.0.0.1:1/x HTTP/1.1",
        [
            ("Host", "127.0.0.1:1"),
            ("Content-Length", "5"),
            ("Transfer-Encoding", "chunked"),
            ("Connection", "close"),
        ],
    )
    assert code == 400


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_rejects_conflicting_content_length_headers(harness):
    """RFC 9112 section 6.3: differing lengths are an unrecoverable error."""
    code, _, _ = await _raw_request(
        harness.port,
        "POST http://127.0.0.1:1/x HTTP/1.1",
        [
            ("Host", "127.0.0.1:1"),
            ("Content-Length", "5"),
            ("Content-Length", "6"),
            ("Connection", "close"),
        ],
    )
    assert code == 400


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_rejects_transfer_encoding_without_final_chunked(harness):
    """RFC 9112 section 6.3: a non-final chunked coding cannot be framed."""
    code, _, _ = await _raw_request(
        harness.port,
        "POST http://127.0.0.1:1/x HTTP/1.1",
        [
            ("Host", "127.0.0.1:1"),
            ("Transfer-Encoding", "gzip"),
            ("Connection", "close"),
        ],
    )
    assert code == 400


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_accepts_duplicate_identical_content_length(harness, upstream_proxy, origin):
    """RFC 9112 section 6.3 allows equal repeats, collapsed to one value."""
    code, _, body = await _raw_request(
        harness.port,
        f"POST http://127.0.0.1:{origin.port}/dup HTTP/1.1",
        [
            ("Host", f"127.0.0.1:{origin.port}"),
            ("Content-Length", "4"),
            ("Content-Length", "4"),
            ("Connection", "close"),
        ],
        body=b"ping",
    )
    assert code == 200
    assert body == b"origin ok"
    assert upstream_proxy.received_headers[-1]["content-length"] == "4"


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_rejects_negative_chunk_size(db):
    """RFC 9112 section 7.1: chunk-size is 1*HEXDIG, so a sign is invalid.

    A silent upstream is used deliberately: any response other than the
    gateway's own 400 would prove the malformed framing was forwarded.
    """

    async def silent(reader, writer):
        with contextlib.suppress(Exception):
            await reader.read(65536)
            with contextlib.suppress(TimeoutError):
                # Wait for the gateway to close once it has answered.
                await asyncio.wait_for(reader.read(), 5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    gateway, pool, port = await _gateway_for_upstream(db, upstream_port)
    try:
        # Bounded read: the old unbounded read(-1) would hang here.
        data = await _read_raw_response(
            port,
            b"POST http://example.invalid/chunk HTTP/1.1\r\n"
            b"Host: example.invalid\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"-1\r\nAAAA",
            timeout=1.5,
        )
        assert data.startswith(b"HTTP/1.1 400")
        await _wait_idle(gateway)
        # A malformed client body is the client's error, not the proxy's.
        assert pool.all()[0].fail_count == 0
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("gateway_type", ["http", "socks5"])
async def test_https_upstream_starts_tls_before_sending_credentials(db, gateway_type):
    from app.gateway_socks import SocksGateway
    observed = asyncio.Future()
    finished = asyncio.Event()

    async def upstream(reader, writer):
        try:
            observed.set_result(await reader.read(1))
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    port = server.sockets[0].getsockname()[1]
    proxy = await pool.add(parse_line(f"https://tester:synthetic@127.0.0.1:{port}"))
    await pool.mark_success(proxy.id, 10)
    gateway_class = HttpGateway if gateway_type == "http" else SocksGateway
    gateway = gateway_class(
        pool, Rotator("round_robin"), key, port=0, timeout_s=0.5, max_retry_per_request=0
    )
    await gateway.start()
    try:
        bound = gateway.status("").bound
        reader, writer = await asyncio.open_connection("127.0.0.1", int(bound.rsplit(":", 1)[1]))
        try:
            if gateway_type == "http":
                writer.write(b"CONNECT target.invalid:443 HTTP/1.1\r\nHost: target.invalid:443\r\n\r\n")
            else:
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x00"
                domain = b"target.invalid"
                writer.write(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + b"\x01\xbb")
            await writer.drain()
            first_byte = await asyncio.wait_for(observed, 2)
            await asyncio.wait_for(finished.wait(), 2)
            assert first_byte == b"\x16", "Expected TLS ClientHello, never plaintext CONNECT/auth"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


# ── Expect: 100-continue (RFC 9110 section 10.1.1) ──────────────────────────


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_expect_continue_is_answered_locally(db, upstream_proxy, origin):
    """A complying client must not deadlock waiting for the interim response."""
    gateway, _, port = await _gateway_for_upstream(db, upstream_proxy.port)
    try:
        data = await _read_raw_response(
            port,
            (
                f"POST http://127.0.0.1:{origin.port}/expect HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{origin.port}\r\n"
                "Content-Length: 4\r\n"
                "Expect: 100-continue\r\n"
                "Connection: close\r\n\r\n"
                "ping"
            ).encode(),
            timeout=3.0,
        )
        interim, _, rest = data.partition(b"\r\n\r\n")
        assert interim == b"HTTP/1.1 100 Continue"
        assert rest.startswith(b"HTTP/1.1 200")
        # We answered the expectation, so it must not travel upstream.
        assert "expect" not in upstream_proxy.received_headers[-1]
    finally:
        await gateway.stop()


# ── Client body timeouts (slow-loris after the headers) ─────────────────────


async def _silent_upstream() -> tuple[asyncio.base_events.Server, int]:
    """A server that accepts the connection and never answers."""

    async def handler(reader, writer):
        with contextlib.suppress(Exception):
            await reader.read(65536)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(reader.read(), 5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]  # type: ignore[index]


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_stalled_request_body_is_a_client_error(db):
    server, upstream_port = await _silent_upstream()
    gateway, pool, port = await _gateway_for_upstream(db, upstream_port, timeout_s=0.3)
    try:
        data = await _read_raw_response(
            port,
            b"POST http://example.invalid/slow HTTP/1.1\r\n"
            b"Host: example.invalid\r\n"
            b"Content-Length: 10\r\n\r\n"
            b"X",
            timeout=2.0,
        )
        assert data.startswith(b"HTTP/1.1 400")
        await _wait_idle(gateway)
        assert pool.all()[0].fail_count == 0
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_stalled_chunked_upload_does_not_penalise_the_proxy(db):
    """A client that stops mid-chunk is its own error, not the proxy's."""
    server, upstream_port = await _silent_upstream()
    gateway, pool, port = await _gateway_for_upstream(db, upstream_port, timeout_s=0.3)
    try:
        data = await _read_raw_response(
            port,
            b"POST http://example.invalid/chunked HTTP/1.1\r\n"
            b"Host: example.invalid\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"5\r\nAB",
            timeout=2.0,
        )
        assert data.startswith(b"HTTP/1.1 400")
        await _wait_idle(gateway)
        assert pool.all()[0].fail_count == 0
    finally:
        await gateway.stop()
        server.close()
        await server.wait_closed()


# ── Transport limits ───────────────────────────────────────────────────────


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_overlong_header_line_is_a_client_error(harness):
    """An over-long line hits the stream limit before any length check runs."""
    data = await _read_raw_response(
        harness.port,
        b"GET http://127.0.0.1:1/ HTTP/1.1\r\n"
        b"Host: 127.0.0.1:1\r\n"
        b"X-Oversized: " + b"a" * 70000 + b"\r\n\r\n",
        timeout=2.0,
    )
    assert data.startswith(b"HTTP/1.1 400")


# ── SOCKS5 upstream credentials (app/socks_upstream.py) ─────────────────────────


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_connect_through_socks5_upstream_sends_exact_credentials(db):
    """Credentials the parser accepts must survive the URL handed to python_socks.

    ``p%41/ss`` is legal input; interpolated raw it arrives as ``pA/ss`` (or
    raises), so the upstream refuses the tunnel and a working proxy is burned.
    """
    upstream = FakeSocks5Upstream()
    await upstream.start()
    password = "p%41/ss"
    gateway, _, port = await _gateway_for_upstream(
        db, upstream.port, scheme="socks5", password=password
    )
    try:
        data = await _read_raw_response(
            port,
            b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n",
            timeout=3.0,
        )
        assert data.startswith(b"HTTP/1.1 200")
        assert b"TUNNEL-OK" in data
        assert upstream.version_seen == 5
        assert upstream.username == "gatewayuser"
        assert upstream.password == password
    finally:
        await gateway.stop()
        await upstream.stop()


# ── Admission control ──────────────────────────────────────────────────────


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_http_gateway_refuses_beyond_max_connections(db):
    """A saturated gateway answers 503 instead of accumulating tasks."""
    server, upstream_port = await _silent_upstream()
    gateway, _, port = await _gateway_for_upstream(
        db, upstream_port, timeout_s=5.0, max_connections=1
    )
    occupying = None
    try:
        # The first connection holds the only slot while it waits for a request.
        occupying = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)
        assert gateway.status("x").active_conns == 1

        data = await _read_raw_response(
            port,
            b"GET http://example.invalid/x HTTP/1.1\r\nHost: example.invalid\r\n\r\n",
            timeout=2.0,
        )
        assert data.startswith(b"HTTP/1.1 503")
        assert b"Gateway Busy" in data
    finally:
        if occupying is not None:
            occupying[1].close()
            with contextlib.suppress(ConnectionError, OSError):
                await occupying[1].wait_closed()
        await gateway.stop()
        server.close()
        await server.wait_closed()


# ── SPEC section 6 failure reasons ─────────────────────────────────────────


@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_connection_failure_reason_reaches_the_pool(db, caplog):
    """A refused upstream is reported as a connection failure, not 'upstream_error'."""
    probe = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]  # type: ignore[index]
    probe.close()
    await probe.wait_closed()

    gateway, _, port = await _gateway_for_upstream(db, dead_port, max_retry_per_request=0)
    try:
        with caplog.at_level("INFO", logger="app.pool"):
            data = await _read_raw_response(
                port,
                b"GET http://example.invalid/x HTTP/1.1\r\nHost: example.invalid\r\n\r\n",
                timeout=3.0,
            )
        assert data.startswith(b"HTTP/1.1 502")
        assert "reason=Connection failure" in caplog.text
    finally:
        await gateway.stop()
