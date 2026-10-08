"""Local HTTP forward proxy that routes every request through an upstream pool.

Scope (T5.1):
  * Accept GET/POST/PUT/DELETE/HEAD/OPTIONS and CONNECT.
  * Pick a healthy upstream through the Rotator for each request.
  * Forward via aiohttp (http/https upstream) or aiohttp_socks (socks5 upstream).
  * Add ``Via: 1.1 proxy-rotator`` and strip hop-by-hop headers.
  * Report per-request success/failure to the Pool.

Deferred:
  * Burn-on-response rules + per-request retry (T5.2).
  * SOCKS5 inbound server (T5.3).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator, Iterable
from typing import Final

import aiohttp
from aiohttp_socks import ProxyConnector  # type: ignore[import-untyped]
from yarl import URL

from app.burn_detector import BurnDetector
from app.crypto import decrypt_password
from app.models import ListenerStatus, Proxy
from app.pool import Pool
from app.rotator import Rotator

logger = logging.getLogger(__name__)

_HOP_BY_HOP: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

_FORWARDED_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}
)

_VIA_TOKEN: Final[str] = "1.1 proxy-rotator"
_MAX_REQUEST_LINE: Final[int] = 8192
_MAX_HEADERS_BYTES: Final[int] = 32 * 1024
_TUNNEL_CHUNK: Final[int] = 64 * 1024


class HttpGatewayError(Exception):
    """Raised when the gateway cannot complete a client request."""


def _strip_hop_by_hop(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    hop_extra: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            hop_extra.update(token.strip().lower() for token in value.split(",") if token.strip())
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in _HOP_BY_HOP and name.lower() not in hop_extra
    ]


def _append_via(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    for i, (name, value) in enumerate(headers):
        if name.lower() == "via":
            headers[i] = (name, f"{value}, {_VIA_TOKEN}")
            return headers
    headers.append(("Via", _VIA_TOKEN))
    return headers


def _format_response_headers(headers: Iterable[tuple[str, str]]) -> bytes:
    buffer = bytearray()
    for name, value in headers:
        buffer.extend(name.encode("latin-1"))
        buffer.extend(b": ")
        buffer.extend(value.encode("latin-1"))
        buffer.extend(b"\r\n")
    return bytes(buffer)


async def _read_request_line(reader: asyncio.StreamReader) -> tuple[str, str, str]:
    try:
        line = await reader.readuntil(b"\r\n")
    except asyncio.IncompleteReadError as exc:
        raise HttpGatewayError("Client closed connection before request-line") from exc
    except asyncio.LimitOverrunError as exc:
        raise HttpGatewayError("Request-line too long") from exc
    if len(line) > _MAX_REQUEST_LINE:
        raise HttpGatewayError("Request-line too long")
    text = line.rstrip(b"\r\n").decode("latin-1")
    parts = text.split(" ", 2)
    if len(parts) != 3:
        raise HttpGatewayError(f"Malformed request-line: {text!r}")
    method, target, version = parts
    if not version.upper().startswith("HTTP/"):
        raise HttpGatewayError(f"Unsupported protocol: {version!r}")
    return method.upper(), target, version


async def _read_headers(reader: asyncio.StreamReader) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    total = 0
    while True:
        line = await reader.readuntil(b"\r\n")
        total += len(line)
        if total > _MAX_HEADERS_BYTES:
            raise HttpGatewayError("Header block too large")
        if line == b"\r\n":
            return headers
        try:
            name_bytes, value_bytes = line.rstrip(b"\r\n").split(b":", 1)
        except ValueError as exc:
            raise HttpGatewayError(f"Malformed header: {line!r}") from exc
        headers.append(
            (name_bytes.decode("latin-1").strip(), value_bytes.decode("latin-1").strip())
        )


def _basic_auth(proxy: Proxy, key: bytes) -> str:
    secret = decrypt_password(proxy.password_enc.get_secret_value(), key)
    token = f"{proxy.username}:{secret.get_secret_value()}".encode()
    return base64.b64encode(token).decode("ascii")


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(_TUNNEL_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        return
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            if dst.can_write_eof():
                dst.write_eof()


class HttpGateway:
    """Asyncio-based HTTP forward proxy."""

    def __init__(
        self,
        pool: Pool,
        rotator: Rotator,
        key: bytes,
        *,
        host: str = "127.0.0.1",
        port: int = 8888,
        timeout_s: float = 15.0,
        tls_context: ssl.SSLContext | None = None,
        max_retry_per_request: int = 2,
        burn_detector: BurnDetector | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_retry_per_request < 0:
            raise ValueError("max_retry_per_request must be non-negative")
        self.pool = pool
        self.rotator = rotator
        self._key = key
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._tls_context = tls_context
        self._max_retry = max_retry_per_request
        self._burn = burn_detector or BurnDetector(timeout_s=timeout_s)
        self._server: asyncio.base_events.Server | None = None
        self._bound: tuple[str, int] | None = None
        self._active_conns = 0
        self._requests_total = 0
        self._active_tasks: set[asyncio.Task[None]] = set()

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        sockets = self._server.sockets or ()
        if sockets:
            sockname = sockets[0].getsockname()
            self._bound = (sockname[0], sockname[1])
        logger.info(
            "gateway_http_started bind=%s:%s", *(self._bound if self._bound else ("?", "?"))
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._bound = None
        pending = list(self._active_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("gateway_http_stopped")

    # ── introspection for the admin UI ────────────────────────────────────

    def status(self, configured: str) -> ListenerStatus:
        bound = f"{self._bound[0]}:{self._bound[1]}" if self._bound else None
        return ListenerStatus(
            configured=configured,
            bound=bound,
            active_conns=self._active_conns,
            requests_total=self._requests_total,
        )

    # ── connection dispatch ───────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        self._active_conns += 1
        try:
            method, target, _ = await _read_request_line(reader)
            headers = await _read_headers(reader)
            self._requests_total += 1
            if method == "CONNECT":
                await self._handle_connect(reader, writer, target)
            elif method in _FORWARDED_METHODS:
                await self._handle_forward(reader, writer, method, target, headers)
            else:
                await self._reply_error(writer, 405, "Method Not Allowed")
        except HttpGatewayError as exc:
            logger.warning("gateway_http_bad_request err=%s", exc)
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 400, "Bad Request")
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("gateway_http_unexpected_error")
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 500, "Internal Server Error")
        finally:
            self._active_conns -= 1
            if task is not None:
                self._active_tasks.discard(task)
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()

    # ── CONNECT (TLS tunnel) ──────────────────────────────────────────────

    async def _handle_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
    ) -> None:
        host, _, port_str = target.rpartition(":")
        if not host or not port_str.isdigit():
            raise HttpGatewayError(f"Malformed CONNECT target: {target!r}")
        port = int(port_str)

        # Retry only on upstream-connect errors. Once bytes bridge, we cannot
        # swap proxies without observing the TLS session itself (out of scope).
        attempts_left = self._max_retry + 1
        chosen: Proxy | None = None
        up_reader: asyncio.StreamReader | None = None
        up_writer: asyncio.StreamWriter | None = None
        last_reason = "no attempt"
        while attempts_left > 0:
            attempts_left -= 1
            chosen = self._pick(target_domain=host.strip("[]"))
            if chosen is None:
                await self._reply_error(writer, 503, "Pool Empty")
                return
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    self._open_upstream_tunnel(chosen, host, port),
                    timeout=self._timeout_s,
                )
                break
            except (TimeoutError, ConnectionError, OSError, HttpGatewayError) as exc:
                last_reason = _format_reason(exc)
                logger.info(
                    "gateway_http_connect_upstream_failed proxy_id=%s target=%s:%s err=%s",
                    chosen.id,
                    host,
                    port,
                    exc,
                )
                await self.pool.mark_fail(chosen.id, "upstream_connect_error")
                up_reader = None
                up_writer = None

        if up_reader is None or up_writer is None or chosen is None:
            await self._reply_error(writer, 502, f"Bad Gateway: {last_reason}")
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            with contextlib.suppress(ConnectionError, OSError):
                up_writer.close()
                await up_writer.wait_closed()
            return

        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                up_writer.close()
                await up_writer.wait_closed()
            await self.pool.mark_success(chosen.id, 0)

    async def _open_upstream_tunnel(
        self, proxy: Proxy, target_host: str, target_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if proxy.scheme == "socks5":
            # aiohttp_socks exposes a dedicated helper for raw TCP tunnels.
            from aiohttp_socks import open_connection  # noqa: PLC0415

            password = decrypt_password(proxy.password_enc.get_secret_value(), self._key)
            reader, writer = await open_connection(
                proxy_url=(
                    f"socks5://{proxy.username}:{password.get_secret_value()}"
                    f"@{proxy.host}:{proxy.port}"
                ),
                host=target_host,
                port=target_port,
            )
            return reader, writer

        # HTTP(S) upstream: send CONNECT and bridge once we see 2xx.
        reader_up, writer_up = await asyncio.open_connection(
            proxy.host,
            proxy.port,
            ssl=(self._tls_context or True) if proxy.scheme == "https" else None,
        )
        try:
            auth = _basic_auth(proxy, self._key)
            request = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n"
                f"Proxy-Connection: keep-alive\r\n"
                f"Via: {_VIA_TOKEN}\r\n"
                "\r\n"
            ).encode("ascii")
            writer_up.write(request)
            await writer_up.drain()
            status_line = await reader_up.readuntil(b"\r\n")
            try:
                http_version, code_str, *_ = status_line.decode("latin-1").split(" ", 2)
                code = int(code_str)
            except (ValueError, UnicodeDecodeError) as exc:
                with contextlib.suppress(ConnectionError, OSError):
                    writer_up.close()
                    await writer_up.wait_closed()
                raise HttpGatewayError(f"Bad CONNECT reply: {status_line!r}") from exc
            if not http_version.upper().startswith("HTTP/"):
                raise HttpGatewayError(f"Bad CONNECT reply: {status_line!r}")
            # Drain remaining header block.
            await _read_headers(reader_up)
            if not 200 <= code < 300:
                with contextlib.suppress(ConnectionError, OSError):
                    writer_up.close()
                    await writer_up.wait_closed()
                raise HttpGatewayError(f"Upstream refused CONNECT: {code}")
            return reader_up, writer_up
        except BaseException:
            writer_up.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer_up.wait_closed()
            raise

    # ── absolute-URI forwarded request ────────────────────────────────────

    async def _handle_forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
    ) -> None:
        if not target.lower().startswith(("http://", "https://")):
            await self._reply_error(writer, 400, "Absolute URI required for forward proxy")
            return
        url = URL(target)
        if url.scheme not in {"http", "https"} or not url.host:
            raise HttpGatewayError(f"Unsupported absolute URI: {target!r}")

        clean_headers = _append_via(_strip_hop_by_hop(headers))
        header_names = {name.lower() for name, _ in clean_headers}
        if "host" not in header_names:
            host_value = url.host if url.port is None else f"{url.host}:{url.port}"
            clean_headers.append(("Host", host_value))

        # Buffer small bodies so we can retry on burn; otherwise stream once.
        body_bytes = await _collect_body_if_small(reader, headers)
        has_body_stream = body_bytes is None and _has_body(headers)
        if has_body_stream:
            # Non-idempotent: a single attempt only (we cannot rewind the stream).
            unbuffered = _request_body_stream(reader, headers)
            await self._single_forward(writer, method, url, clean_headers, unbuffered)
            return

        attempts_left = self._max_retry + 1
        last_reason = "no attempt"
        tried: set[int] = set()
        while attempts_left > 0:
            attempts_left -= 1
            proxy = self._pick(target_domain=url.host or "")
            if proxy is None or proxy.id in tried:
                break
            tried.add(proxy.id)
            attempt_body = _bytes_async_iter(body_bytes) if body_bytes is not None else None
            try:
                burn = await asyncio.wait_for(
                    self._forward_attempt(writer, method, url, clean_headers, attempt_body, proxy),
                    timeout=self._timeout_s,
                )
            except (TimeoutError, aiohttp.ClientError, ConnectionError, OSError) as exc:
                last_reason = _format_reason(exc)
                logger.info(
                    "gateway_http_forward_failed proxy_id=%s target=%s err=%s",
                    proxy.id,
                    url.host,
                    exc,
                )
                await self.pool.mark_fail(proxy.id, "upstream_error")
                continue
            if burn is None:
                return  # success — already streamed to client
            last_reason = burn
            await self.pool.mark_fail(proxy.id, burn)

        if not tried:
            await self._reply_error(writer, 503, "Pool Empty")
        else:
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 502, f"Bad Gateway: {last_reason}")

    async def _single_forward(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        url: URL,
        clean_headers: list[tuple[str, str]],
        body: AsyncIterator[bytes] | None,
    ) -> None:
        proxy = self._pick(target_domain=url.host or "")
        if proxy is None:
            await self._reply_error(writer, 503, "Pool Empty")
            return
        try:
            burn = await asyncio.wait_for(
                self._forward_attempt(writer, method, url, clean_headers, body, proxy),
                timeout=self._timeout_s,
            )
        except (TimeoutError, aiohttp.ClientError, ConnectionError, OSError) as exc:
            logger.info(
                "gateway_http_forward_failed proxy_id=%s target=%s err=%s",
                proxy.id,
                url.host,
                exc,
            )
            await self.pool.mark_fail(proxy.id, "upstream_error")
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 502, "Bad Gateway")
            return
        if burn is not None:
            await self.pool.mark_fail(proxy.id, burn)
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 502, f"Bad Gateway: {burn}")

    async def _forward_attempt(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        url: URL,
        headers: list[tuple[str, str]],
        body: AsyncIterator[bytes] | None,
        proxy: Proxy,
    ) -> str | None:
        """Perform one upstream attempt.

        Returns ``None`` when the response was successfully streamed to the
        client; returns a short burn reason string when the response was
        classified as burned *before* any bytes left the gateway (so the
        caller can safely retry on a different proxy).
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        connector: aiohttp.BaseConnector | None = None
        proxy_url: URL | None = None
        proxy_auth: aiohttp.BasicAuth | None = None
        password = decrypt_password(proxy.password_enc.get_secret_value(), self._key)
        if proxy.scheme == "socks5":
            connector = ProxyConnector(
                host=proxy.host,
                port=proxy.port,
                username=proxy.username,
                password=password.get_secret_value(),
                rdns=True,
            )
        else:
            proxy_url = URL.build(scheme=proxy.scheme, host=proxy.host, port=proxy.port)
            proxy_auth = aiohttp.BasicAuth(proxy.username, password.get_secret_value())

        async with (
            aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                auto_decompress=False,
                skip_auto_headers={"User-Agent", "Accept-Encoding"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=self._timeout_s),
            ) as session,
            session.request(
                method,
                url,
                headers=headers,
                data=body,
                proxy=proxy_url,
                proxy_auth=proxy_auth,
                allow_redirects=False,
                ssl=self._tls_context if self._tls_context is not None else True,
            ) as response,
        ):
            # Status-based burn classification BEFORE forwarding anything.
            burn = self._burn.classify(status_code=response.status)
            if burn.burned:
                response.release()
                return burn.reason or f"HTTP {response.status}"

            # Optional body-marker classification for small, inspectable bodies.
            upstream_cl = response.headers.get("Content-Length")
            content_type = response.headers.get("Content-Type") or ""
            peek_body: bytes | None = None
            if (
                upstream_cl is not None
                and upstream_cl.isdigit()
                and int(upstream_cl) <= 128 * 1024
                and self._burn.inspect_body
                and _is_inspectable(content_type)
            ):
                peek_body = await response.read()
                body_burn = self._burn.classify(
                    status_code=response.status,
                    body=peek_body,
                    content_type=content_type,
                )
                if body_burn.burned:
                    return body_burn.reason or "body marker"

            status_line = f"HTTP/1.1 {response.status} {response.reason or ''}\r\n".encode(
                "latin-1"
            )
            out_headers = _append_via(_strip_hop_by_hop(list(response.headers.items())))
            out_headers = [
                (name, value)
                for name, value in out_headers
                if name.lower() not in {"content-length", "transfer-encoding"}
            ]
            if peek_body is not None:
                out_headers.append(("Content-Length", str(len(peek_body))))
                writer.write(status_line + _format_response_headers(out_headers) + b"\r\n")
                writer.write(peek_body)
                await writer.drain()
            elif upstream_cl is None:
                out_headers.append(("Transfer-Encoding", "chunked"))
                use_chunked = True
            else:
                out_headers.append(("Content-Length", upstream_cl))
                use_chunked = False
            if peek_body is None:
                writer.write(status_line + _format_response_headers(out_headers) + b"\r\n")
                await writer.drain()
                async for chunk in response.content.iter_any():
                    if use_chunked:
                        writer.write(f"{len(chunk):x}\r\n".encode("ascii"))
                    writer.write(chunk)
                    if use_chunked:
                        writer.write(b"\r\n")
                    await writer.drain()
                if use_chunked:
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()
        latency_ms = max(1, int((loop.time() - started) * 1000))
        await self.pool.mark_success(proxy.id, latency_ms)
        return None

    # ── helpers ───────────────────────────────────────────────────────────

    def _pick(self, *, target_domain: str) -> Proxy | None:
        if self.rotator.strategy == "sticky":
            return self.rotator.pick(self.pool, client_ip="127.0.0.1", target_domain=target_domain)
        return self.rotator.pick(self.pool)

    async def _reply_error(self, writer: asyncio.StreamWriter, code: int, phrase: str) -> None:
        body = f"{code} {phrase}\n".encode("ascii")
        head = (
            f"HTTP/1.1 {code} {phrase}\r\n"
            f"Content-Type: text/plain; charset=us-ascii\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Via: {_VIA_TOKEN}\r\n"
            f"Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            writer.write(head + body)
            await writer.drain()
        except (ConnectionError, OSError):
            return


# ── retry helpers ───────────────────────────────────────────────────────────


_MAX_BUFFERED_BODY: Final[int] = 1 * 1024 * 1024  # 1 MiB cap for retryable bodies


def _format_reason(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:200]


def _is_inspectable(content_type: str) -> bool:
    media = content_type.partition(";")[0].strip().lower()
    return media.startswith("text/") or media == "application/json"


def _has_body(headers: list[tuple[str, str]]) -> bool:
    for name, value in headers:
        lname = name.lower()
        if lname == "transfer-encoding" and "chunked" in value.lower():
            return True
        if lname == "content-length" and value.isdigit() and int(value) > 0:
            return True
    return False


async def _collect_body_if_small(
    reader: asyncio.StreamReader, headers: list[tuple[str, str]]
) -> bytes | None:
    """Return the request body as bytes when small enough to buffer for retry.

    Returns:
        * ``None`` when the request has no body (safe to retry freely),
        * ``bytes`` when the body was buffered (safe to retry with a fresh
          iterator per attempt),
        * ``None`` ALSO when the body is too large to buffer; the caller must
          then stream once without retry support.
    """
    length_text = ""
    encoding = ""
    for name, value in headers:
        lname = name.lower()
        if lname == "content-length":
            length_text = value
        elif lname == "transfer-encoding":
            encoding = value.lower()
    if "chunked" in encoding:
        return None  # unknown size → do not buffer
    if not length_text or not length_text.isdigit():
        return None  # no body
    length = int(length_text)
    if length == 0:
        return None
    if length > _MAX_BUFFERED_BODY:
        return None  # too large to buffer
    return await reader.readexactly(length)


async def _bytes_async_iter(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ── request-body streaming ──────────────────────────────────────────────────


def _request_body_stream(
    reader: asyncio.StreamReader, headers: list[tuple[str, str]]
) -> AsyncIterator[bytes] | None:
    encoding = ""
    length_text = ""
    for name, value in headers:
        if name.lower() == "transfer-encoding":
            encoding = value.lower()
        elif name.lower() == "content-length":
            length_text = value
    if "chunked" in encoding:
        return _chunked_body(reader)
    if length_text and length_text.isdigit():
        length = int(length_text)
        if length <= 0:
            return None
        return _fixed_body(reader, length)
    return None


async def _fixed_body(reader: asyncio.StreamReader, length: int) -> AsyncIterator[bytes]:
    remaining = length
    while remaining:
        chunk = await reader.read(min(remaining, _TUNNEL_CHUNK))
        if not chunk:
            raise HttpGatewayError("Client closed before body completion")
        remaining -= len(chunk)
        yield chunk


async def _chunked_body(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while True:
        size_line = await reader.readuntil(b"\r\n")
        size_text = size_line.strip().split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise HttpGatewayError(f"Invalid chunk size: {size_text!r}") from exc
        if size == 0:
            while (await reader.readuntil(b"\r\n")) != b"\r\n":
                continue
            return
        remaining = size
        while remaining:
            chunk = await reader.read(min(remaining, _TUNNEL_CHUNK))
            if not chunk:
                raise HttpGatewayError("Chunked body truncated")
            remaining -= len(chunk)
            yield chunk
        if await reader.readexactly(2) != b"\r\n":
            raise HttpGatewayError("Missing CRLF after chunk")
