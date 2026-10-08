"""Opt-in geolocation lookup for proxy exit IPs.

Rules:
  * Disabled by default (``GEOLOCATION_ENABLED=false``). Must be flipped on
    explicitly in ``.env`` — nothing leaves the machine otherwise.
  * Only the exit IP is ever sent to the provider. Proxy credentials are NEVER
    serialized into a lookup URL.
  * Results are cached in SQLite (``geo_cache`` table) so repeated lookups
    for the same IP never hit the provider again until the TTL expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import aiohttp
import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoLookup:
    ip: str
    country: str | None
    country_code: str | None  # ISO 3166-1 alpha-2 (e.g. "US", "FR")
    cached_at: datetime
    source: str  # "cache" | "provider" | "unknown"


class GeolocationService:
    """Resolves exit IPs to a (country, country_code) pair with cache."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        enabled: bool,
        provider_url: str,
        cache_ttl_s: int,
        timeout_s: float,
    ) -> None:
        self._conn = conn
        self._enabled = enabled
        self._provider_url = provider_url
        self._cache_ttl = timedelta(seconds=cache_ttl_s)
        self._timeout_s = timeout_s
        self._inflight: dict[str, asyncio.Future[GeoLookup | None]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def ensure_schema(self) -> None:
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS geo_cache (
                ip            TEXT PRIMARY KEY,
                country       TEXT,
                country_code  TEXT,
                cached_at     TEXT NOT NULL,
                source        TEXT NOT NULL
            )
            """
        )
        await self._conn.commit()

    async def lookup(self, ip: str) -> GeoLookup | None:
        """Return a geolocation hit for ``ip`` or ``None`` when unknown.

        Returns ``None`` immediately when the service is disabled.
        """
        if not self._enabled:
            return None
        try:
            parsed = ip_address(ip)
        except ValueError:
            return None
        if parsed.is_private or parsed.is_loopback or parsed.is_reserved:
            return None

        cached = await self._fetch_cached(ip)
        if cached is not None:
            return cached

        # Coalesce concurrent lookups of the same IP.
        inflight = self._inflight.get(ip)
        if inflight is not None:
            return await inflight
        future: asyncio.Future[GeoLookup | None] = asyncio.get_running_loop().create_future()
        self._inflight[ip] = future
        try:
            fresh = await self._fetch_provider(ip)
            future.set_result(fresh)
            return fresh
        finally:
            self._inflight.pop(ip, None)

    async def _fetch_cached(self, ip: str) -> GeoLookup | None:
        async with self._conn.execute(
            "SELECT country, country_code, cached_at, source FROM geo_cache WHERE ip = ?",
            (ip,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        cached_at = datetime.fromisoformat(row[2]).replace(tzinfo=UTC)
        if datetime.now(tz=UTC) - cached_at > self._cache_ttl:
            return None
        return GeoLookup(
            ip=ip, country=row[0], country_code=row[1], cached_at=cached_at, source="cache"
        )

    async def _fetch_provider(self, ip: str) -> GeoLookup | None:
        try:
            url = self._provider_url.format(ip=ip)
        except (KeyError, IndexError):
            logger.warning("geolocation_provider_url_malformed")
            return None
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=self._timeout_s)
                ) as session,
                session.get(url, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    return None
                payload = await response.read()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.info("geolocation_fetch_failed ip=%s err=%s", ip, exc)
            return None

        country, country_code = _parse_provider_payload(payload)
        now = datetime.now(tz=UTC)
        result = GeoLookup(
            ip=ip, country=country, country_code=country_code, cached_at=now, source="provider"
        )
        await self._store(result)
        return result

    async def _store(self, hit: GeoLookup) -> None:
        await self._conn.execute(
            """
            INSERT INTO geo_cache (ip, country, country_code, cached_at, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                country       = excluded.country,
                country_code  = excluded.country_code,
                cached_at     = excluded.cached_at,
                source        = excluded.source
            """,
            (hit.ip, hit.country, hit.country_code, hit.cached_at.isoformat(), hit.source),
        )
        await self._conn.commit()


def _parse_provider_payload(payload: bytes) -> tuple[str | None, str | None]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    if data.get("status") not in {"success", None}:
        return None, None
    country = data.get("country") if isinstance(data.get("country"), str) else None
    code_raw = data.get("countryCode")
    country_code = code_raw.upper() if isinstance(code_raw, str) and len(code_raw) == 2 else None
    return country, country_code
