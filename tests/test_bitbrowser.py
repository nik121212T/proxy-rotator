import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.bitbrowser import BitBrowserClient, BitBrowserError


async def test_configuration_is_encrypted_and_profile_is_reused(db, monkeypatch):
    key = Fernet.generate_key()
    client = await BitBrowserClient.load(db, key)
    calls = []
    profiles = {}

    async def request(path, body, **kwargs):
        calls.append((path, body))
        if path == "/browser/list":
            return {"list": []}
        if path == "/browser/update":
            profiles["owned-id"] = dict(body, id="owned-id")
            return {"id": "owned-id"}
        if path == "/browser/detail":
            return profiles[body["id"]]
        if path == "/browser/pids":
            return {}
        return {}

    monkeypatch.setattr(client, "request", request)
    await client.configure(54345, SecretStr("private-api-key"))
    first = await client.prepare(None, 12345, "https://example.invalid")
    second = await client.prepare(None, 12346, "https://example.invalid")
    assert first == second == "owned-id"
    assert sum(path == "/browser/update" for path, _ in calls) == 1
    create = next(body for path, body in calls if path == "/browser/update")
    assert create["host"] == "127.0.0.1" and create["proxyType"] == "http"
    assert not create["proxyPassword"]
    async with db.execute("SELECT token_enc FROM provider_tokens") as cursor:
        rows = await cursor.fetchall()
    assert all(b"private-api-key" not in bytes(row[0]) for row in rows)
    reloaded = await BitBrowserClient.load(db, key)
    assert reloaded.configured and reloaded.owner == client.owner
    profiles["owned-id"]["remark"] = "someone else"
    with pytest.raises(BitBrowserError, match="appartient"):
        await client.prepare(None, 12347, "https://example.invalid")


@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {}),
        (200, {"success": False, "msg": "secret-token"}),
        (200, {"success": True, "data": []}),
        (302, {}),
    ],
)
async def test_local_api_rejects_errors_without_exposing_payload(db, status, payload):
    from aiohttp import web

    async def handler(request):
        assert request.headers["x-api-key"] == "secret-token"
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_post("/browser/list", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = await BitBrowserClient.load(db, Fernet.generate_key())
    try:
        with pytest.raises(BitBrowserError) as error:
            await client.configure(port, SecretStr("secret-token"))
        assert "secret-token" not in str(error.value)
        assert not client.configured
    finally:
        await runner.cleanup()


async def test_bitbrowser_session_closure_stops_gateway_and_keeps_profile(
    db, tmp_path, monkeypatch
):
    import asyncio

    from app.browser_manager import BrowserError, BrowserManager
    from app.parser import parse_line
    from app.pool import Pool
    from app.rotator import Rotator

    key = Fernet.generate_key()
    client = await BitBrowserClient.load(db, key)
    client.configured = True
    profiles = {}
    running = set()
    fail_close = False

    async def request(path, body, **kwargs):
        if path == "/browser/update":
            profiles["profile-1"] = body
            return {"id": "profile-1"}
        if path == "/browser/detail":
            return profiles[body["id"]]
        if path == "/browser/pids":
            return {identifier: 123 for identifier in running}
        if path == "/browser/open":
            running.add(body["id"])
        if path == "/browser/close":
            if fail_close:
                raise BitBrowserError("API inaccessible")
            running.discard(body["id"])
        return {}

    monkeypatch.setattr(client, "request", request)
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line("proxy.invalid:80"))
    await pool.mark_success(proxy.id, 10)
    manager = BrowserManager(pool, Rotator(), key, tmp_path / "profiles", bitbrowser=client)
    try:
        identifier = await manager.launch(
            "bitbrowser", "bitbrowser", proxy.id, "https://example.invalid"
        )
        port = profiles["profile-1"]["port"]
        with pytest.raises(BrowserError, match="déjà"):
            await manager.launch("bitbrowser", "bitbrowser", proxy.id, "https://example.invalid")
        fail_close = True
        with pytest.raises(BrowserError, match="inaccessible"):
            await manager.close_session(identifier)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        assert manager.sessions[identifier].close_error
        fail_close = False
        await manager.close_session(identifier)
        assert not manager.sessions and profiles
        async with db.execute("SELECT profile_id FROM bitbrowser_profiles") as cursor:
            assert tuple(await cursor.fetchone()) == ("profile-1",)
    finally:
        fail_close = False
        await manager.stop()
