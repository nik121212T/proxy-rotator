"""DichvusocksClient covered against a local aiohttp fake upstream."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from pydantic import SecretStr

from app.providers.dichvusocks import DichvusocksClient, DichvusocksError, ProviderSock


class FakeDichvusocks:
    """Minimal stand-in for api.dichvusocks.net."""

    def __init__(self) -> None:
        self.seen_auth: list[str | None] = []
        self.handlers: dict[str, web.StreamResponse | web.Response] = {}
        self.inline_handlers: dict[str, object] = {}
        self._status_overrides: dict[str, int] = {}
        self.port: int = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{path:.*}", self._dispatch)
        self._runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def set_status(self, path: str, status: int) -> None:
        self._status_overrides[path] = status

    def set_response(self, path: str, response: web.StreamResponse | web.Response) -> None:
        self.handlers[path] = response

    def set_inline_handler(self, path: str, handler: object) -> None:
        self.inline_handlers[path] = handler

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        auth = request.headers.get("Authorization")
        self.seen_auth.append(auth)
        path = "/" + request.match_info["path"]
        if path in self._status_overrides:
            return web.Response(status=self._status_overrides[path])
        if path in self.inline_handlers:
            handler = self.inline_handlers[path]
            return await handler(request)  # type: ignore[misc,operator]
        if path in self.handlers:
            return self.handlers[path]
        return web.json_response({"ok": True, "path": path})


@pytest.fixture
async def upstream() -> AsyncIterator[FakeDichvusocks]:
    server = FakeDichvusocks()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _client(upstream: FakeDichvusocks, *, token: str | None = "T0K") -> DichvusocksClient:  # noqa: S107 - synthetic test token
    return DichvusocksClient(
        token=SecretStr(token) if token else None,
        base_url=f"http://127.0.0.1:{upstream.port}",
        timeout_s=2,
    )


# ── enabled / disabled toggles ────────────────────────────────────────────


async def test_client_is_disabled_without_a_token(upstream: FakeDichvusocks) -> None:
    client = _client(upstream, token=None)
    assert client.enabled is False
    with pytest.raises(DichvusocksError):
        await client.user_info()


async def test_blank_token_counts_as_disabled(upstream: FakeDichvusocks) -> None:
    client = _client(upstream, token="   ")
    assert client.enabled is False


async def test_set_token_toggles_enabled(upstream: FakeDichvusocks) -> None:
    client = _client(upstream, token=None)
    assert client.enabled is False
    client.set_token(SecretStr("new"))
    assert client.enabled is True
    client.set_token(None)
    assert client.enabled is False


# ── auth header ───────────────────────────────────────────────────────────


async def test_bearer_token_is_sent_on_requests(upstream: FakeDichvusocks) -> None:
    client = _client(upstream, token="SECRET-TOKEN")
    await client.user_info()
    assert upstream.seen_auth[-1] == "Bearer SECRET-TOKEN"


# ── documented error codes map to DichvusocksError ────────────────────────


@pytest.mark.parametrize(
    ("status", "snippet"),
    [
        (401, "Invalid"),
        (403, "banned"),
        (404, "not found"),
        (410, "removed"),
        (429, "Rate"),
        (500, "HTTP 500"),
    ],
)
async def test_http_errors_are_mapped(
    upstream: FakeDichvusocks, status: int, snippet: str
) -> None:
    upstream.set_status("/user/info", status)
    client = _client(upstream)
    with pytest.raises(DichvusocksError) as info:
        await client.user_info()
    assert snippet.lower() in str(info.value).lower()


async def test_non_json_body_is_rejected(upstream: FakeDichvusocks) -> None:
    upstream.set_response("/user/info", web.Response(body=b"not-json", status=200))
    client = _client(upstream)
    with pytest.raises(DichvusocksError) as info:
        await client.user_info()
    assert "non-json" in str(info.value).lower() or "JSON" in str(info.value)


async def test_non_dict_payload_is_rejected(upstream: FakeDichvusocks) -> None:
    upstream.set_response("/user/info", web.json_response([1, 2, 3]))
    client = _client(upstream)
    with pytest.raises(DichvusocksError):
        await client.user_info()


# ── happy paths ───────────────────────────────────────────────────────────


async def test_countries_accepts_list_payload(upstream: FakeDichvusocks) -> None:
    upstream.set_response(
        "/socks/countries", web.json_response({"data": ["FR", "US", "DE"]})
    )
    client = _client(upstream)
    assert set(await client.countries()) == {"FR", "US", "DE"}


async def test_list_socks_passes_filters_and_returns_entries(
    upstream: FakeDichvusocks,
) -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update({k: v for k, v in request.query.items()})
        return web.json_response(
            {"data": [{"id": 11, "ip": "1.2.3.4", "port": 1080}]}
        )

    upstream.set_inline_handler("/socks/data", handler)
    client = _client(upstream)
    rows = await client.list_socks(country="FR", limit=5, page=2)
    assert captured["country"] == "FR"
    assert captured["limit"] == "5"
    assert captured["page"] == "2"
    assert rows and rows[0]["id"] == 11


async def test_view_returns_a_provider_sock(upstream: FakeDichvusocks) -> None:
    upstream.set_response(
        "/socks/view",
        web.json_response(
            {
                "data": {
                    "id": 42,
                    "ip": "203.0.113.5",
                    "port": 1080,
                    "username": "u",
                    "password": "p",
                    "host": "node.example",
                    "country": "France",
                    "countryCode": "FR",
                }
            }
        ),
    )
    client = _client(upstream)
    sock = await client.view(42)
    assert isinstance(sock, ProviderSock)
    assert (sock.ip, sock.port, sock.username, sock.password) == (
        "203.0.113.5",
        1080,
        "u",
        "p",
    )
    assert sock.country_code == "FR"
