"""Bounded end-to-end probes through a local listener, never a direct fallback."""

import asyncio
import json
from ipaddress import ip_address
from time import monotonic

import aiohttp
from aiohttp_socks import ProxyConnector  # type: ignore[import-untyped]
from python_socks import ProxyError


async def probe_gateway(kind: str, bound: str | None, target: str) -> dict[str, object]:
    start = monotonic()
    result: dict[str, object] = {"ok": False, "exit_ip": None, "latency_ms": 0}
    if not bound:
        return {**result, "message": "Passerelle hors service."}
    host, port = bound.rsplit(":", 1)
    host = host.strip("[]")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104
    address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    connector = (
        ProxyConnector.from_url(f"socks5://{address}", rdns=True) if kind == "socks5" else None
    )
    try:
        async with asyncio.timeout(10):
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(
                    target,
                    proxy=f"http://{address}" if kind == "http" else None,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        return {
                            **result,
                            "message": f"Échec via la passerelle (HTTP {response.status}). Vérifiez le pool et les filtres.",
                        }
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise ValueError("oversize")
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ValueError("invalid response")
                    exit_ip = str(ip_address(data["ip"]))
        return {
            "ok": True,
            "exit_ip": exit_ip,
            "latency_ms": round((monotonic() - start) * 1000),
            "message": "Connexion réussie via la passerelle.",
        }
    except (
        aiohttp.ClientError,
        ProxyError,
        OSError,
        TimeoutError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return {
            **result,
            "latency_ms": round((monotonic() - start) * 1000),
            "message": "Connexion impossible ou délai dépassé (10 s). Vérifiez les proxies disponibles et les filtres de rotation.",
        }
