"""Opening raw TCP tunnels through a SOCKS5 upstream.

Credentials are handed to ``python_socks`` as keyword arguments rather than
interpolated into a ``socks5://`` URL. Such a URL is parsed with ``urlparse``
and then ``unquote``d, which silently rewrites a stored password like
``p%41ss`` into ``pAss`` and raises ``ValueError`` for ``pa/ss``, ``pa?ss``,
``pa#ss`` or ``pa[ss`` — every one of which ``app/parser.py`` accepts. Passing
the values directly removes the encoding step instead of trying to escape it.

``aiohttp_socks.open_connection`` performed that URL round-trip for us and is
deprecated in 0.9.1; this is the direct equivalent.
"""

from __future__ import annotations

import asyncio

from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy


async def open_socks_tunnel(
    *,
    proxy_host: str,
    proxy_port: int,
    username: str,
    password: str,
    dest_host: str,
    dest_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a SOCKS5 tunnel and wrap it in asyncio streams."""
    # The annotated constructor, not the untyped ``Proxy.create`` passthrough.
    proxy = Proxy(
        proxy_type=ProxyType.SOCKS5,
        host=proxy_host,
        port=proxy_port,
        username=username,
        password=password,
        # Resolve names at the proxy: no local DNS leak (CLAUDE.md rule 5).
        rdns=True,
    )
    sock = await proxy.connect(dest_host=dest_host, dest_port=dest_port)
    # If the outer caller cancels (asyncio.wait_for timeout, task cancellation)
    # between proxy.connect() and open_connection(), the raw socket would leak
    # — the proxy returned a live FD that nothing else holds a reference to
    # (audit A17). Guard it with a close-on-failure fallback.
    try:
        return await asyncio.open_connection(host=None, port=None, sock=sock)
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise
