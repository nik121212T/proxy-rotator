"""Dichvusocks.net SOCKS5 marketplace API client.

Scope:
  * Lists / countries / regions / cities / user info (read-only, free).
  * ``view`` call is EXPLICIT — it debits the account's credit per request.
    The service never auto-views; the operator triggers each import.

The API token is read from Settings.dichvusocks_api_token. Nothing is sent
when the token is missing (service is implicitly disabled).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from pydantic import SecretStr

logger = logging.getLogger(__name__)


class DichvusocksError(Exception):
    """Raised when the upstream API responds with an unrecoverable error."""


@dataclass(frozen=True)
class ProviderSock:
    sock_id: int
    ip: str
    port: int
    username: str
    password: str
    host: str | None
    country: str | None
    country_code: str | None
    region: str | None
    city: str | None
    speed: str | None
    use_type: str | None


class DichvusocksClient:
    """Thin async client over the dichvusocks.net v1 API."""

    def __init__(
        self,
        *,
        token: SecretStr | None,
        base_url: str = "https://dichvusocks.net/api/v1",
        timeout_s: float = 10.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return self._token is not None and bool(self._token.get_secret_value().strip())

    def set_token(self, token: SecretStr | None) -> None:
        """Swap the live API token at runtime (no restart required)."""
        self._token = token

    # ── public API ────────────────────────────────────────────────────────

    async def user_info(self) -> dict[str, Any]:
        return await self._get_json("/user/info")

    async def countries(self) -> list[str]:
        payload = await self._get_json("/socks/countries")
        values = payload.get("data") or payload.get("countries") or payload
        if isinstance(values, list):
            return [str(v) for v in values if isinstance(v, str | int)]
        return []

    async def list_socks(
        self,
        *,
        country: str | None = None,
        region: str | None = None,
        city: str | None = None,
        auth: str = "all",
        limit: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "auth": auth,
            "limit": str(max(1, min(limit, 100))),
            "page": str(max(1, page)),
        }
        if country:
            params["country"] = country
        if region:
            params["region"] = region
        if city:
            params["city"] = city
        payload = await self._get_json("/socks/data", params=params)
        data = payload.get("data") or payload.get("items") or []
        return [item for item in data if isinstance(item, dict)]

    async def view(self, sock_id: int) -> ProviderSock | None:
        """DEDUCTS CREDIT — fetch full credentials for a single sock."""
        payload = await self._get_json("/socks/view", params={"id": str(int(sock_id))})
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        try:
            port = int(data.get("port", 0))
            username = str(data.get("username") or "")
            password = str(data.get("password") or "")
            ip = str(data.get("ip") or "")
            if not ip or port <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return ProviderSock(
            sock_id=int(data.get("id", sock_id)),
            ip=ip,
            port=port,
            username=username,
            password=password,
            host=str(data.get("host")) if data.get("host") else None,
            country=str(data.get("country")) if data.get("country") else None,
            country_code=str(data.get("countryCode"))[:2].upper()
            if data.get("countryCode")
            else None,
            region=str(data.get("region")) if data.get("region") else None,
            city=str(data.get("city")) if data.get("city") else None,
            speed=str(data.get("speed")) if data.get("speed") else None,
            use_type=str(data.get("useType")) if data.get("useType") else None,
        )

    # ── internals ─────────────────────────────────────────────────────────

    async def _get_json(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if not self.enabled:
            raise DichvusocksError("Dichvusocks provider is disabled (no API token)")
        assert self._token is not None
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
        }
        url = f"{self._base_url}{path}"
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=self._timeout_s)
                ) as session,
                session.get(url, headers=headers, params=params or {}) as response,
            ):
                body = await response.read()
                if response.status == 401:
                    raise DichvusocksError("Invalid or missing API token (401)")
                if response.status == 403:
                    raise DichvusocksError("Account banned or insufficient balance (403)")
                if response.status == 404:
                    raise DichvusocksError("Resource not found (404)")
                if response.status == 410:
                    raise DichvusocksError("Sock removed (410)")
                if response.status == 429:
                    raise DichvusocksError("Rate limited (429)")
                if response.status >= 400:
                    raise DichvusocksError(f"HTTP {response.status}")
                import json as _json  # local to avoid top-level cost on hot paths

                try:
                    data = _json.loads(body)
                except _json.JSONDecodeError as exc:
                    raise DichvusocksError("Non-JSON response from provider") from exc
                if not isinstance(data, dict):
                    raise DichvusocksError("Unexpected response shape")
                return data
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise DichvusocksError(f"Transport failure: {exc}") from exc
