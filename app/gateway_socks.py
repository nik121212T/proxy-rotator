"""Local SOCKS5 forward proxy.

Scope (T5.3):
  * SOCKS5 version 5, NO AUTH only on the inbound side (localhost gateway).
  * CONNECT command only.
  * ATYP: IPv4, IPv6, and domain (passed through so DNS resolves upstream — no leak).
  * Retry upstream-connect errors up to ``max_retry_per_request`` times.
  * Report per-connection success/failure to the Pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import Final

from app.crypto import decrypt_password
from app.models import ListenerStatus, Proxy
from app.pool import Pool
from app.rotator import Rotator
from app.socks_upstream import open_socks_tunnel

logger = logging.getLogger(__name__)

_SOCKS_VER: Final[int] = 0x05
_METHOD_NO_AUTH: Final[int] = 0x00
_METHOD_NO_ACCEPTABLE: Final[int] = 0xFF
_CMD_CONNECT: Final[int] = 0x01
_ATYP_IPV4: Final[int] = 0x01
_ATYP_DOMAIN: Final[int] = 0x03
_ATYP_IPV6: Final[int] = 0x04

_REP_SUCCESS: Final[int] = 0x00
_REP_GENERAL_FAILURE: Final[int] = 0x01
_REP_NET_UNREACHABLE: Final[int] = 0x03
_REP_HOST_UNREACHABLE: Final[int] = 0x04
_REP_CONN_REFUSED: Final[int] = 0x05
_REP_TTL_EXPIRED: Final[int] = 0x06
_REP_COMMAND_NOT_SUPPORTED: Final[int] = 0x07
_REP_ATYP_NOT_SUPPORTED: Final[int] = 0x08

_PIPE_CHUNK: Final[int] = 64 * 1024
_ZERO_IPV4_PORT: Final[bytes] = bytes([_ATYP_IPV4, 0, 0, 0, 0, 0, 0])


class SocksGatewayError(Exception):
    """Protocol-level error from the inbound SOCKS5 client."""


class SocksGateway:
    def __init__(
        self,
        pool: Pool,
        rotator: Rotator,
        key: bytes,
        *,
        host: str = "127.0.0.1",
        port: int = 1080,
        timeout_s: float = 15.0,
        max_retry_per_request: int = 2,
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
        self._max_retry = max_retry_per_request
        self._max_connections = max_connections
        self._server: asyncio.base_events.Server | None = None
        self._bound: tuple[str, int] | None = None
        self._active_conns = 0
        self._requests_total = 0
        self._active_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        sockets = self._server.sockets or ()
        if sockets:
            sockname = sockets[0].getsockname()
            self._bound = (sockname[0], sockname[1])
        logger.info(
            "gateway_socks_started bind=%s:%s", *(self._bound if self._bound else ("?", "?"))
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
        logger.info("gateway_socks_stopped")

    def status(self, configured: str) -> ListenerStatus:
        bound = f"{self._bound[0]}:{self._bound[1]}" if self._bound else None
        return ListenerStatus(
            configured=configured,
            bound=bound,
            active_conns=self._active_conns,
            requests_total=self._requests_total,
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Admission control: refuse rather than accumulate tasks and buffers.
        # The connection is closed without a SOCKS5 frame: the method-selection
        # exchange has not started, so the client sees a refused connection.
        if self._active_conns >= self._max_connections:
            logger.warning(
                "gateway_socks_busy active=%s limit=%s", self._active_conns, self._max_connections
            )
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()
            return
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        self._active_conns += 1
        try:
            # Slow-loris guard: bound the SOCKS5 handshake.
            async with asyncio.timeout(self._timeout_s):
                if not await self._negotiate_methods(reader, writer):
                    return
                target_host, target_port = await self._read_request(reader, writer)
            self._requests_total += 1

            chosen, up_reader, up_writer, failure_rep = await self._open_tunnel_with_retry(
                target_host, target_port, writer=writer
            )
            if chosen is None or up_reader is None or up_writer is None:
                await self._reply(writer, failure_rep)
                return

            with self.pool.activity.track(chosen.id, "SOCKS5"):
                await self._reply(writer, _REP_SUCCESS)
                try:
                    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
                finally:
                    with contextlib.suppress(ConnectionError, OSError):
                        up_writer.close()
                        await up_writer.wait_closed()
                    with contextlib.suppress(KeyError):
                        # Liveness only: a tunnel has no request latency, and the
                        # proxy may have been deleted while the tunnel was open.
                        await self.pool.mark_tunnel_success(chosen.id)

        except SocksGatewayError as exc:
            logger.warning("gateway_socks_protocol_error err=%s", exc)
        except TimeoutError:
            logger.info("gateway_socks_handshake_timeout bind=%s:%s", *(self._bound or ("?", "?")))
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("gateway_socks_unexpected_error")
        finally:
            self._active_conns -= 1
            if task is not None:
                self._active_tasks.discard(task)
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()
                await writer.wait_closed()

    async def _negotiate_methods(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        header = await reader.readexactly(2)
        version, nmethods = header[0], header[1]
        if version != _SOCKS_VER:
            raise SocksGatewayError(f"Unsupported SOCKS version: {version}")
        if nmethods == 0:
            raise SocksGatewayError("Empty methods list")
        methods = await reader.readexactly(nmethods)
        if _METHOD_NO_AUTH not in methods:
            writer.write(bytes([_SOCKS_VER, _METHOD_NO_ACCEPTABLE]))
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            return False
        writer.write(bytes([_SOCKS_VER, _METHOD_NO_AUTH]))
        await writer.drain()
        return True

    async def _read_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, int]:
        header = await reader.readexactly(4)
        version, cmd, _rsv, atyp = header[0], header[1], header[2], header[3]
        if version != _SOCKS_VER:
            raise SocksGatewayError(f"Unsupported SOCKS version in request: {version}")
        if cmd != _CMD_CONNECT:
            await self._reply(writer, _REP_COMMAND_NOT_SUPPORTED)
            raise SocksGatewayError(f"Unsupported SOCKS command: {cmd}")
        if atyp == _ATYP_IPV4:
            addr_bytes = await reader.readexactly(4)
            host = ".".join(str(b) for b in addr_bytes)
        elif atyp == _ATYP_IPV6:
            addr_bytes = await reader.readexactly(16)
            host = _format_ipv6(addr_bytes)
        elif atyp == _ATYP_DOMAIN:
            length_byte = await reader.readexactly(1)
            length = length_byte[0]
            if length == 0:
                raise SocksGatewayError("Empty domain name")
            host = (await reader.readexactly(length)).decode("ascii", errors="strict")
        else:
            await self._reply(writer, _REP_ATYP_NOT_SUPPORTED)
            raise SocksGatewayError(f"Unsupported ATYP: {atyp}")
        port_bytes = await reader.readexactly(2)
        (port,) = struct.unpack("!H", port_bytes)
        return host, port

    async def _reply(self, writer: asyncio.StreamWriter, rep: int) -> None:
        payload = bytes([_SOCKS_VER, rep, 0x00]) + _ZERO_IPV4_PORT
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(payload)
            await writer.drain()

    async def _open_tunnel_with_retry(
        self,
        target_host: str,
        target_port: int,
        *,
        writer: asyncio.StreamWriter | None = None,
    ) -> tuple[
        Proxy | None,
        asyncio.StreamReader | None,
        asyncio.StreamWriter | None,
        int,
    ]:
        attempts_left = self._max_retry + 1
        tried: set[int] = set()
        last_rep = _REP_GENERAL_FAILURE
        while attempts_left > 0:
            attempts_left -= 1
            proxy = self._pick(target_domain=target_host.strip("[]"), writer=writer)
            if proxy is None:
                return None, None, None, last_rep
            if proxy.id in tried:
                break
            tried.add(proxy.id)
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    self._open_upstream_tunnel(proxy, target_host, target_port),
                    timeout=self._timeout_s,
                )
                return proxy, up_reader, up_writer, _REP_SUCCESS
            except TimeoutError as exc:
                last_rep = _REP_TTL_EXPIRED
                reason = str(exc) or "timeout"
            except ConnectionRefusedError:
                last_rep = _REP_CONN_REFUSED
                reason = "connection refused"
            except (ConnectionError, OSError, SocksGatewayError) as exc:
                last_rep = _REP_HOST_UNREACHABLE
                reason = str(exc)[:200]
            await self.pool.mark_fail(proxy.id, "upstream_connect_error")
            logger.info(
                "gateway_socks_upstream_failed proxy_id=%s target=%s:%s err=%s",
                proxy.id, target_host, target_port, reason,
            )
        return None, None, None, last_rep

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

        from app.gateway_http import _basic_auth, _read_headers  # noqa: PLC0415

        reader_up, writer_up = await asyncio.open_connection(
            proxy.host, proxy.port, ssl=True if proxy.scheme == "https" else None
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
                "\r\n"
            ).encode("ascii")
            writer_up.write(request)
            await writer_up.drain()
            status_line = await reader_up.readuntil(b"\r\n")
            try:
                version, code_str, *_ = status_line.decode("latin-1").split(" ", 2)
                code = int(code_str)
            except (ValueError, UnicodeDecodeError) as exc:
                with contextlib.suppress(ConnectionError, OSError):
                    writer_up.close()
                    await writer_up.wait_closed()
                raise SocksGatewayError(f"Bad CONNECT reply: {status_line!r}") from exc
            if not version.upper().startswith("HTTP/"):
                raise SocksGatewayError(f"Bad CONNECT reply: {status_line!r}")
            await _read_headers(reader_up)
            if not 200 <= code < 300:
                with contextlib.suppress(ConnectionError, OSError):
                    writer_up.close()
                    await writer_up.wait_closed()
                raise SocksGatewayError(f"Upstream refused CONNECT: {code}")
            return reader_up, writer_up
        except BaseException:
            writer_up.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer_up.wait_closed()
            raise

    def _pick(
        self,
        *,
        target_domain: str,
        writer: asyncio.StreamWriter | None = None,
    ) -> Proxy | None:
        if self.rotator.strategy == "sticky":
            # Reuse the HTTP gateway helper to extract the real peer IP so
            # sticky partitions are per-client, not per-proxy-rotator (audit B5).
            from app.gateway_http import _peer_ip

            return self.rotator.pick(
                self.pool, client_ip=_peer_ip(writer), target_domain=target_domain
            )
        return self.rotator.pick(self.pool)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(_PIPE_CHUNK)
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


def _format_ipv6(raw: bytes) -> str:
    pieces = [f"{int.from_bytes(raw[i:i + 2], 'big'):x}" for i in range(0, 16, 2)]
    return ":".join(pieces)
