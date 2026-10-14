"""Real SOCKS5 handshake against localhost, without external DNS or traffic."""

import asyncio

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from app.health_checker import HealthChecker
from app.parser import parse_line
from app.pool import Pool


@pytest.mark.parametrize("with_auth", [True, False])
async def test_socks_probe_forwards_domain_and_authenticates(
    db: aiosqlite.Connection, with_auth: bool
) -> None:
    received: asyncio.Future[tuple[str, bool]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(count)
            authenticated = False
            if with_auth:
                assert 2 in methods
                writer.write(b"\x05\x02")
                await writer.drain()
                auth_version, length = await reader.readexactly(2)
                username = await reader.readexactly(length)
                password_length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(password_length)
                authenticated = auth_version == 1 and username == b"user" and password == b"synthetic"
                writer.write(b"\x01\x00")
            else:
                assert methods == b"\x00"
                writer.write(b"\x05\x00")
            await writer.drain()
            header = await reader.readexactly(4)
            assert header == b"\x05\x01\x00\x03"  # Domain ATYP, never local resolution.
            length = (await reader.readexactly(1))[0]
            domain = (await reader.readexactly(length)).decode("ascii")
            await reader.readexactly(2)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"ip":"203.0.113.22"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            received.set_result((domain, authenticated))
        except Exception as error:
            if not received.done():
                received.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        key = Fernet.generate_key()
        pool = await Pool.load(db, key)
        auth = "user:synthetic@" if with_auth else ""
        proxy = await pool.add(parse_line(f"socks5://{auth}127.0.0.1:{port}"))
        checker = HealthChecker(pool, key, probe_url="http://unresolvable.invalid/", timeout_s=1)
        result = await checker.check_one(proxy.id)
        assert await asyncio.wait_for(received, 1) == ("unresolvable.invalid", with_auth)
        assert result.ok
        assert str(result.exit_ip) == "203.0.113.22"
