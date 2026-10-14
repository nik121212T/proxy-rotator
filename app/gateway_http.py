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
import re
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
from app.socks_upstream import open_socks_tunnel

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

# RFC 9110 section 9.2.2: only idempotent methods may be replayed after a
# failure, because the origin may already have applied the request.
_IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"}
)

_VIA_TOKEN: Final[str] = "1.1 proxy-rotator"
_CONTINUE: Final[bytes] = b"HTTP/1.1 100 Continue\r\n\r\n"
_MAX_REQUEST_LINE: Final[int] = 8192
_MAX_HEADERS_BYTES: Final[int] = 32 * 1024
_TUNNEL_CHUNK: Final[int] = 64 * 1024

# RFC 9112 section 7.1: ``chunk-size = 1*HEXDIG``. The RFC also requires
# recipients to defend against integer-conversion overflow, so the size field
# is bounded well above any real-world chunk.
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}\Z")
_MAX_CHUNK_LINE_BYTES: Final[int] = 1024
_MAX_TRAILER_FIELDS: Final[int] = 32


class HttpGatewayError(Exception):
    """Raised when the gateway cannot complete a client request."""


class _AttemptState:
    """Tracks whether one upstream attempt already committed bytes to the client.

    Once a status line has been written the response is on the wire; retrying
    would append a second HTTP response to the first one (RFC 9112 section 9.2).
    """

    __slots__ = ("burn_reason", "response_started")

    def __init__(self) -> None:
        self.response_started = False
        self.burn_reason: str | None = None


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
    """Serialize headers to latin-1 bytes without raising on non-latin bytes.

    RFC 7230 §3.2.4 recommends ISO-8859-1 but modern origins occasionally
    emit UTF-8 (e.g. a stray non-ASCII character in a server-generated
    reason or a header value). Replacing instead of raising keeps the
    gateway from crashing out of the response path (audit A16).
    """
    buffer = bytearray()
    for name, value in headers:
        buffer.extend(name.encode("latin-1", errors="replace"))
        buffer.extend(b": ")
        buffer.extend(value.encode("latin-1", errors="replace"))
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
        line = await _read_line(reader)
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


def _peer_ip(writer: asyncio.StreamWriter | None) -> str:
    """Best-effort peer IP for sticky rotation (audit B5)."""
    if writer is None:
        return "127.0.0.1"
    peer = writer.get_extra_info("peername")
    if peer and isinstance(peer, tuple) and len(peer) >= 1 and isinstance(peer[0], str):
        return peer[0]
    return "127.0.0.1"


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
            # Flush buffered bytes before the FIN so the peer sees them (A15).
            await dst.drain()
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
        max_connections: int = 256,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_retry_per_request < 0:
            raise ValueError("max_retry_per_request must be non-negative")
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self.pool = pool
        self.rotator = rotator
        self._key = key
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._tls_context = tls_context
        self._max_retry = max_retry_per_request
        self._burn = burn_detector or BurnDetector(timeout_s=timeout_s)
        self._max_connections = max_connections
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
        # Admission control: refuse rather than accumulate tasks and buffers.
        # No await sits between the check and the increment, so it is atomic on
        # the single event loop.
        if self._active_conns >= self._max_connections:
            logger.warning(
                "gateway_http_busy active=%s limit=%s", self._active_conns, self._max_connections
            )
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 503, "Gateway Busy")
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()
            return
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        self._active_conns += 1
        try:
            # Slow-loris guard: bound how long we wait for the request headers.
            async with asyncio.timeout(self._timeout_s):
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
        except TimeoutError:
            logger.info("gateway_http_handshake_timeout bind=%s:%s", *(self._bound or ("?", "?")))
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
        tried: set[int] = set()
        pool_was_empty = True
        while attempts_left > 0:
            attempts_left -= 1
            chosen = self._pick(target_domain=host.strip("[]"), writer=writer)
            if chosen is None:
                # On the very first attempt the pool is simply empty; after
                # that the only candidates left are proxies we already tried,
                # so a retry would just re-burn them (audit A14).
                if pool_was_empty:
                    await self._reply_error(writer, 503, "Pool Empty")
                    return
                break
            pool_was_empty = False
            if chosen.id in tried:
                break
            tried.add(chosen.id)
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

        with self.pool.activity.track(chosen.id, "HTTP"):
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
                with contextlib.suppress(KeyError):
                    # A tunnel is not a latency measurement, and the proxy may
                    # have been deleted while the tunnel was open.
                    await self.pool.mark_tunnel_success(chosen.id)

    async def _open_upstream_tunnel(
        self, proxy: Proxy, target_host: str, target_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if proxy.scheme == "socks5":
            password = decrypt_password(proxy.password_enc.get_secret_value(), self._key)
            return await open_socks_tunnel(
                proxy_host=proxy.host,
                proxy_port=proxy.port,
                username=proxy.username,
                password=password.get_secret_value(),
                dest_host=target_host,
                dest_port=target_port,
            )

        # HTTP(S) upstream: send CONNECT and bridge once we see 2xx.
        reader_up, writer_up = await asyncio.open_connection(
            proxy.host,
            proxy.port,
            ssl=(self._tls_context or True) if proxy.scheme == "https" else None,
        )
        try:
            auth_header = (
                f"Proxy-Authorization: Basic {_basic_auth(proxy, self._key)}\r\n"
                if proxy.username else ""
            )
            request = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"{auth_header}"
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

        # RFC 9112 sections 6.1/6.3: reject ambiguous framing before reading a
        # body. A rejection here propagates to the 400 handler, which then
        # closes the connection as the RFC requires.
        headers = _normalize_body_framing(headers)

        clean_headers = _append_via(_strip_hop_by_hop(headers))
        # The expectation is answered here, so it must not travel further.
        clean_headers = [
            (name, value) for name, value in clean_headers if name.lower() != "expect"
        ]
        # Overwrite any client-sent Host so it matches the absolute URI's
        # authority (audit A13): otherwise a client could proxy to origin A
        # while pretending to address virtual host B at that origin.
        host_value = url.host if url.port is None else f"{url.host}:{url.port}"
        clean_headers = [(name, value) for name, value in clean_headers if name.lower() != "host"]
        clean_headers.append(("Host", host_value))

        if _expects_continue(headers):
            # RFC 9110 section 10.1.1: a client may wait for the interim
            # response before sending its body. Answering it here removes the
            # deadlock, where a compliant client waits for 100 while the gateway
            # waits for the body.
            with contextlib.suppress(ConnectionError, OSError):
                writer.write(_CONTINUE)
                await writer.drain()

        # Buffer small bodies so we can retry on burn; otherwise stream once.
        body_bytes = await _collect_body_if_small(reader, headers, self._timeout_s)
        has_body_stream = body_bytes is None and _has_body(headers)
        if has_body_stream or method not in _IDEMPOTENT_METHODS:
            # A streamed body cannot be rewound, and a non-idempotent method
            # (RFC 9110 section 9.2.2, RFC 9112 section 9.3.1) must never be
            # replayed once the origin may have applied it. Both take exactly
            # one attempt, whatever the outcome.
            if has_body_stream:
                attempt_body: AsyncIterator[bytes] | None = _request_body_stream(
                    reader, headers, self._timeout_s
                )
            else:
                attempt_body = _bytes_async_iter(body_bytes) if body_bytes is not None else None
            await self._single_forward(writer, method, url, clean_headers, attempt_body)
            return

        attempts_left = self._max_retry + 1
        last_reason = "no attempt"
        tried: set[int] = set()
        while attempts_left > 0:
            attempts_left -= 1
            proxy = self._pick(target_domain=url.host or "", writer=writer)
            if proxy is None or proxy.id in tried:
                break
            tried.add(proxy.id)
            attempt_body = _bytes_async_iter(body_bytes) if body_bytes is not None else None
            state = _AttemptState()
            try:
                burn = await self._forward_attempt(
                    writer, method, url, clean_headers, attempt_body, proxy, state
                )
            except (
                HttpGatewayError,
                TimeoutError,
                aiohttp.ClientError,
                ConnectionError,
                OSError,
            ) as exc:
                if _caused_by_client_framing(exc):
                    await self._reply_client_body_error(writer, exc)
                    return
                if state.response_started:
                    # The response is already committed to the client: a retry
                    # would concatenate a second response into the same stream.
                    await self._note_interrupted_stream(proxy, url, exc)
                    return
                last_reason = _format_reason(exc)
                logger.info(
                    "gateway_http_forward_failed proxy_id=%s target=%s err=%s",
                    proxy.id,
                    url.host,
                    exc,
                )
                await self.pool.mark_fail(proxy.id, self._failure_reason(exc, "upstream_error"))
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
        proxy = self._pick(target_domain=url.host or "", writer=writer)
        if proxy is None:
            await self._reply_error(writer, 503, "Pool Empty")
            return
        state = _AttemptState()
        try:
            await self._forward_attempt(
                writer, method, url, clean_headers, body, proxy, state, retry_on_burn=False
            )
        except (HttpGatewayError, TimeoutError, aiohttp.ClientError, ConnectionError, OSError) as exc:
            if _caused_by_client_framing(exc):
                # The client sent a malformed body: its error, not the proxy's.
                await self._reply_client_body_error(writer, exc)
                return
            if state.response_started:
                # The response is already committed to the client: a retry would
                # concatenate a second response into the same stream.
                await self._note_interrupted_stream(proxy, url, exc)
                return
            logger.info(
                "gateway_http_forward_failed proxy_id=%s target=%s err=%s",
                proxy.id,
                url.host,
                exc,
            )
            await self.pool.mark_fail(proxy.id, self._failure_reason(exc, "upstream_error"))
            with contextlib.suppress(ConnectionError, OSError):
                await self._reply_error(writer, 502, "Bad Gateway")
            return

    async def _forward_attempt(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        url: URL,
        headers: list[tuple[str, str]],
        body: AsyncIterator[bytes] | None,
        proxy: Proxy,
        state: _AttemptState,
        *,
        retry_on_burn: bool = True,
    ) -> str | None:
        """Perform one upstream attempt.

        Returns ``None`` when the response was successfully streamed to the
        client; returns a short burn reason string when the response was
        classified as burned *before* any bytes left the gateway (so the
        caller can safely retry on a different proxy). ``state`` records the
        moment the response becomes unrecoverable, so the caller never retries
        over bytes that already reached the client.

        With ``retry_on_burn=False`` the attempt is not replayable: a burned
        response is forwarded to the client unchanged (so the client sees the
        origin's real status) and the burn is recorded in ``state`` instead of
        being turned into a retry.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        connector: aiohttp.BaseConnector | None = None
        proxy_url: URL | None = None
        password = decrypt_password(proxy.password_enc.get_secret_value(), self._key)
        if proxy.scheme == "socks5":
            connector = ProxyConnector(
                host=proxy.host,
                port=proxy.port,
                username=proxy.username or None,
                password=password.get_secret_value() if proxy.username else None,
                rdns=True,
            )
        else:
            # Credentials travel inside the proxy URL: aiohttp 3.14 deprecates
            # both ``proxy_auth`` and ``aiohttp.BasicAuth``, and yarl encodes the
            # userinfo correctly (aiohttp derives Proxy-Authorization from it).
            proxy_url = URL.build(
                scheme=proxy.scheme,
                host=proxy.host,
                port=proxy.port,
                user=proxy.username or None,
                password=password.get_secret_value() if proxy.username else None,
            )

        async with (
            aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                auto_decompress=False,
                skip_auto_headers={"User-Agent", "Accept-Encoding"},
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=self._timeout_s, sock_read=self._timeout_s
                ),
            ) as session,
            session.request(
                method,
                url,
                headers=headers,
                data=body,
                proxy=proxy_url,
                allow_redirects=False,
                ssl=self._tls_context if self._tls_context is not None else True,
            ) as response,
        ):
            with self.pool.activity.track(proxy.id, "HTTP"):
                # Status-based burn classification BEFORE forwarding anything.
                burn = self._burn.classify(status_code=response.status)
                if burn.burned:
                    if retry_on_burn:
                        response.release()
                        return burn.reason or f"HTTP {response.status}"
                    state.burn_reason = burn.reason or f"HTTP {response.status}"

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
                        if retry_on_burn:
                            return body_burn.reason or "body marker"
                        state.burn_reason = body_burn.reason or "body marker"

                status_line = f"HTTP/1.1 {response.status} {response.reason or ''}\r\n".encode(
                    "latin-1", errors="replace"
                )
                out_headers = _append_via(_strip_hop_by_hop(list(response.headers.items())))
                out_headers = [
                    (name, value)
                    for name, value in out_headers
                    if name.lower() not in {"content-length", "transfer-encoding", "connection"}
                ]
                # The gateway closes the connection after one request/response
                # pair, so tell the client not to try pipelining (audit A12).
                out_headers.append(("Connection", "close"))
                if peek_body is not None:
                    out_headers.append(("Content-Length", str(len(peek_body))))
                    state.response_started = True
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
                    state.response_started = True
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
        if state.burn_reason is not None:
            # The response went through, but this proxy is burned: learn from it
            # without re-sending a request the origin may already have applied.
            await self.pool.mark_fail(proxy.id, state.burn_reason)
        else:
            await self.pool.mark_success(proxy.id, latency_ms)
        return None

    # ── helpers ───────────────────────────────────────────────────────────

    def _pick(
        self,
        *,
        target_domain: str,
        writer: asyncio.StreamWriter | None = None,
    ) -> Proxy | None:
        if self.rotator.strategy == "sticky":
            return self.rotator.pick(
                self.pool,
                client_ip=_peer_ip(writer),
                target_domain=target_domain,
            )
        return self.rotator.pick(self.pool)

    async def _reply_client_body_error(
        self, writer: asyncio.StreamWriter, exc: BaseException
    ) -> None:
        """Answer 400 for a malformed client body instead of blaming the proxy."""
        logger.info("gateway_http_bad_body err=%s", exc)
        with contextlib.suppress(ConnectionError, OSError):
            await self._reply_error(writer, 400, "Bad Request")

    def _failure_reason(self, exc: BaseException, fallback: str) -> str:
        """Name a transport failure the way SPEC section 6 does.

        Without this the timeouts and connection errors the detector knows how
        to classify were all reported as the generic ``upstream_error``.
        """
        verdict = self._burn.classify(error=exc)
        return verdict.reason or fallback

    async def _note_interrupted_stream(self, proxy: Proxy, url: URL, exc: BaseException) -> None:
        """Record an upstream failure that happened after the response committed.

        A client abort raises ``OSError`` rather than an aiohttp error, so only
        upstream failures are charged to the proxy.
        """
        logger.info(
            "gateway_http_stream_interrupted proxy_id=%s target=%s err=%s",
            proxy.id,
            url.host,
            exc,
        )
        if isinstance(exc, aiohttp.ClientError):
            await self.pool.mark_fail(
                proxy.id, self._failure_reason(exc, "stream_interrupted")
            )

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


def _caused_by_client_framing(exc: BaseException) -> bool:
    """True when a send failure originates from the client's own malformed body.

    aiohttp wraps a body-generator failure in ``ClientConnectionError``, so the
    cause chain is the only reliable way to distinguish a bad client body from
    a bad proxy. Misclassifying this would burn a healthy proxy for a client
    error and answer 502 instead of 400.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, HttpGatewayError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


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


def _expects_continue(headers: list[tuple[str, str]]) -> bool:
    """True when the client asked for a 100 Continue interim response."""
    return any(
        name.lower() == "expect" and "100-continue" in value.lower() for name, value in headers
    )


def _normalize_body_framing(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate request framing and collapse Content-Length per RFC 9112.

    Rules applied (RFC 9112 sections 6.1 and 6.3):

    * both ``Transfer-Encoding`` and ``Content-Length`` present -> reject. The
      RFC permits processing by ``Transfer-Encoding`` alone, but only if the
      received ``Content-Length`` is removed first; rejecting is the safer
      choice for a request, and the connection is closed either way.
    * ``Transfer-Encoding`` whose final coding is not ``chunked`` -> reject:
      the body length cannot be determined reliably.
    * duplicate or list-valued ``Content-Length`` -> accepted only when every
      value is a valid integer and all values are identical, then collapsed to
      a single field; anything else is an unrecoverable framing error.
    """
    lengths: list[str] = []
    encodings: list[str] = []
    for name, value in headers:
        lname = name.lower()
        if lname == "content-length":
            lengths.extend(part.strip() for part in value.split(","))
        elif lname == "transfer-encoding":
            encodings.extend(part.strip().lower() for part in value.split(","))

    if encodings and lengths:
        raise HttpGatewayError("Both Content-Length and Transfer-Encoding")
    if encodings:
        codings = [coding for coding in encodings if coding]
        if not codings or codings[-1] != "chunked" or codings.count("chunked") != 1:
            raise HttpGatewayError("Unsupported Transfer-Encoding sequence")
        return headers
    if not lengths:
        return headers
    if any(not value.isdigit() for value in lengths) or len(set(lengths)) != 1:
        raise HttpGatewayError("Invalid Content-Length")

    collapsed: list[tuple[str, str]] = []
    written = False
    for name, value in headers:
        if name.lower() != "content-length":
            collapsed.append((name, value))
        elif not written:
            collapsed.append((name, lengths[0]))
            written = True
    return collapsed


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    """Read one CRLF-terminated line.

    A line longer than the stream limit raises ``LimitOverrunError`` before any
    length check can run; without this mapping it escaped as a generic 500.
    """
    try:
        return await reader.readuntil(b"\r\n")
    except asyncio.LimitOverrunError as exc:
        raise HttpGatewayError("Line too long") from exc


async def _read_client(
    reader: asyncio.StreamReader, count: int, timeout_s: float, *, exact: bool
) -> bytes:
    """Read client bytes under an idle budget.

    A client that announces a body and then goes silent would otherwise hold the
    gateway open indefinitely (slow-loris after the headers). Raising
    ``HttpGatewayError`` keeps that a client error: it answers 400 and never
    charges the failure to the proxy.
    """
    try:
        async with asyncio.timeout(timeout_s):
            return await (reader.readexactly(count) if exact else reader.read(count))
    except TimeoutError as exc:
        raise HttpGatewayError("Client body timeout") from exc


async def _collect_body_if_small(
    reader: asyncio.StreamReader, headers: list[tuple[str, str]], timeout_s: float
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
    return await _read_client(reader, length, timeout_s, exact=True)


async def _bytes_async_iter(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ── request-body streaming ──────────────────────────────────────────────────


def _request_body_stream(
    reader: asyncio.StreamReader, headers: list[tuple[str, str]], timeout_s: float
) -> AsyncIterator[bytes] | None:
    encoding = ""
    length_text = ""
    for name, value in headers:
        if name.lower() == "transfer-encoding":
            encoding = value.lower()
        elif name.lower() == "content-length":
            length_text = value
    if "chunked" in encoding:
        return _chunked_body(reader, timeout_s)
    if length_text and length_text.isdigit():
        length = int(length_text)
        if length <= 0:
            return None
        return _fixed_body(reader, length, timeout_s)
    return None


async def _fixed_body(
    reader: asyncio.StreamReader, length: int, timeout_s: float
) -> AsyncIterator[bytes]:
    remaining = length
    while remaining:
        chunk = await _read_client(reader, min(remaining, _TUNNEL_CHUNK), timeout_s, exact=False)
        if not chunk:
            raise HttpGatewayError("Client closed before body completion")
        remaining -= len(chunk)
        yield chunk


async def _chunked_body(reader: asyncio.StreamReader, timeout_s: float) -> AsyncIterator[bytes]:
    while True:
        size_line = await _read_line(reader)
        if len(size_line) > _MAX_CHUNK_LINE_BYTES:
            raise HttpGatewayError("Chunk header too long")
        # RFC 9112 section 7.1: ``chunk-size = 1*HEXDIG``. Rejecting anything
        # else (a sign, junk, an over-long numeral) is required to prevent
        # integer-conversion surprises and unbounded reads.
        size_text = size_line.strip().split(b";", 1)[0].strip()
        if not _CHUNK_SIZE.match(size_text):
            raise HttpGatewayError("Invalid chunk size")
        size = int(size_text, 16)
        if size == 0:
            for _ in range(_MAX_TRAILER_FIELDS):
                if await _read_line(reader) == b"\r\n":
                    return
            raise HttpGatewayError("Trailer section too large")
        remaining = size
        while remaining:
            chunk = await _read_client(
                reader, min(remaining, _TUNNEL_CHUNK), timeout_s, exact=False
            )
            if not chunk:
                raise HttpGatewayError("Chunked body truncated")
            remaining -= len(chunk)
            yield chunk
        if await _read_client(reader, 2, timeout_s, exact=True) != b"\r\n":
            raise HttpGatewayError("Missing CRLF after chunk")
