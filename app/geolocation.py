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
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import aiohttp
import aiosqlite

from app.pool import Pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeoLookup:
    ip: str
    country: str | None
    country_code: str | None  # ISO 3166-1 alpha-2 (e.g. "US", "FR")
    cached_at: datetime
    source: str  # "cache" | "provider" | "unknown"
    city: str | None = None
    region: str | None = None


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
        self._inflight: dict[str, asyncio.Task[GeoLookup | None]] = {}
        self._worker: asyncio.Task[None] | None = None

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
                source        TEXT NOT NULL,
                city          TEXT,
                region        TEXT
            )
            """
        )
        await self._conn.commit()

    async def lookup(self, ip: str) -> GeoLookup | None:
        """Return a geolocation hit for ``ip`` or ``None`` when unknown.

        When disabled, only cached metadata from explicit imports is available.
        """
        try:
            parsed = ip_address(ip)
        except ValueError:
            return None
        if parsed.is_private or parsed.is_loopback or parsed.is_reserved:
            return None

        cached = await self._fetch_cached(ip)
        if cached is not None:
            return cached

        if not self._enabled:
            return None

        # Shield the shared request: one disconnected caller cannot strand others.
        task = self._inflight.get(ip)
        if task is None:
            task = asyncio.create_task(self._fetch_provider(ip))
            self._inflight[ip] = task

            def finished(done: asyncio.Task[GeoLookup | None]) -> None:
                self._inflight.pop(ip, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def remember_provider_location(
        self,
        ip: str,
        country: str,
        country_code: str,
        *,
        city: str | None = None,
        region: str | None = None,
    ) -> None:
        """Cache metadata from an explicit import; never makes a network request."""
        try:
            canonical_ip = str(ip_address(ip))
        except ValueError:
            return
        await self._store(
            GeoLookup(
                ip=canonical_ip,
                country=country,
                country_code=country_code.upper(),
                cached_at=datetime.now(UTC),
                source="dichvusocks",
                city=city,
                region=region,
            )
        )

    def start(self, pool: Pool) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._refresh_pool(pool))

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        pending = tuple(self._inflight.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _refresh_pool(self, pool: Pool) -> None:
        next_check: dict[str, float] = {}
        loop = asyncio.get_running_loop()
        while True:
            ips = {str(value.ip) for value in pool.exit_observations().values()}
            next_check = {ip: when for ip, when in next_check.items() if ip in ips}
            for ip in sorted(ips):
                if next_check.get(ip, 0) > loop.time():
                    continue
                hit = await self.lookup(ip)
                if hit and hit.country_code and hit.country:
                    pool.set_country(
                        ip, hit.country_code, hit.country, city=hit.city, region=hit.region
                    )
                else:
                    pool.clear_location(ip)
                next_check[ip] = loop.time() + (self._cache_ttl.total_seconds() if hit else 60)
                # Serial, paced requests; cached data never blocks gateway selection.
                await asyncio.sleep(1.5)
            await asyncio.sleep(1)

    async def _fetch_cached(self, ip: str) -> GeoLookup | None:
        async with self._conn.execute(
            "SELECT country, country_code, cached_at, source, city, region FROM geo_cache "
            "WHERE ip = ? AND (? OR source = 'dichvusocks')",
            (ip, self._enabled),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        cached_at = datetime.fromisoformat(row[2]).replace(tzinfo=UTC)
        if datetime.now(tz=UTC) - cached_at > self._cache_ttl:
            return None
        return GeoLookup(
            ip=ip,
            country=row[0],
            country_code=row[1],
            cached_at=cached_at,
            source="cache",
            city=row[4],
            region=row[5],
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
                body = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > 65536:
                        return None
                payload = bytes(body)
        except (aiohttp.ClientError, TimeoutError, OSError):
            logger.info("geolocation_fetch_failed")
            return None

        country, country_code = _parse_provider_payload(payload)
        city, region = _parse_city_payload(payload)
        now = datetime.now(tz=UTC)
        result = GeoLookup(
            ip=ip,
            country=country,
            country_code=country_code,
            cached_at=now,
            source="provider",
            city=city,
            region=region,
        )
        await self._store(result)
        return result

    async def _store(self, hit: GeoLookup) -> None:
        await self._conn.execute(
            """
            INSERT INTO geo_cache (ip, country, country_code, cached_at, source, city, region)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                country       = excluded.country,
                country_code  = excluded.country_code,
                cached_at     = excluded.cached_at,
                source        = excluded.source,
                city          = excluded.city,
                region        = excluded.region
            """,
            (
                hit.ip,
                hit.country,
                hit.country_code,
                hit.cached_at.isoformat(),
                hit.source,
                hit.city,
                hit.region,
            ),
        )
        await self._conn.commit()


def _parse_provider_payload(payload: bytes) -> tuple[str | None, str | None]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    if data.get("status") not in ("success", None):
        return None, None
    # Accept both schemas: ip-api.com (country / countryCode) and ipapi.co
    # (country_name / country_code), so swapping providers only needs an env
    # var change.
    country_raw = (
        data.get("country") if isinstance(data.get("country"), str) else data.get("country_name")
    )
    country = country_raw if isinstance(country_raw, str) else None
    code_raw = data.get("countryCode") or data.get("country_code")
    country_code = (
        code_raw.upper()
        if isinstance(code_raw, str)
        and len(code_raw) == 2
        and code_raw.isascii()
        and code_raw.isalpha()
        else None
    )
    return country, country_code


def _parse_city_payload(payload: bytes) -> tuple[str | None, str | None]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict) or data.get("status") not in ("success", None):
        return None, None

    def text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 128 else None

    return text(data.get("city")), text(data.get("regionName") or data.get("region"))
