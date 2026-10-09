"""Proxy REST behavior through ASGI, real SQLite and a local HTTP upstream."""

import asyncio
from pathlib import Path

import httpx
import pytest


async def test_import_skips_malformed_lines_and_existing(
    api: tuple[httpx.AsyncClient, int],
) -> None:
    client, port = api
    valid = f"tester:synthetic-password@127.0.0.1:{port}"
    response = await client.post(
        "/api/proxies/import",
        json={
            "text": f"# comment\n\n{valid}\nmalformed\n{valid}",
            "label": "paid-test",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "imported": 1,
        "skipped": 2,
        "errors": [{"line": 4, "reason": "Invalid proxy format"}],
    }
    assert "synthetic-password" not in response.text
    assert (await client.get("/api/proxies")).json()["total"] == 1
    clean = await client.post("/api/proxies/import", json={"text": valid})
    assert clean.json()["imported"] == 0
    again = await client.post("/api/proxies/import", json={"text": valid})
    assert again.json() == {"imported": 0, "skipped": 1, "errors": []}


async def test_list_filters_pagination_and_credentials(api: tuple[httpx.AsyncClient, int]) -> None:
    client, port = api
    await client.post(
        "/api/proxies/import",
        json={
            "text": "\n".join(f"user{i}:synthetic-password@127.0.0.1:{port}" for i in range(3)),
            "label": "paid-test",
        },
    )
    response = await client.get("/api/proxies", params={"limit": 1, "offset": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 1
    assert data["items"][0]["username"] == "user1"
    assert data["items"][0]["label"] == "paid-test"
    assert "password" not in response.text
    assert (await client.get("/api/proxies", params={"status": "dead"})).json()["total"] == 0
    assert (await client.get("/api/proxies", params={"offset": 99})).json() == {
        "items": [],
        "total": 3,
    }


@pytest.mark.parametrize(
    "params",
    [{"status": "invalid"}, {"min_score": -1}, {"min_score": "nan"}, {"limit": 0}, {"offset": -1}],
)
async def test_list_rejects_invalid_filters(
    api: tuple[httpx.AsyncClient, int], params: dict
) -> None:
    client, _ = api
    assert (await client.get("/api/proxies", params=params)).status_code == 422


async def test_one_off_check_and_detail_history(api: tuple[httpx.AsyncClient, int]) -> None:
    client, port = api
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    response = await client.post(f"/api/proxies/{proxy_id}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["exit_ip"] == "203.0.113.30"
    detail = await client.get(f"/api/proxies/{proxy_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == proxy_id
    assert detail.json()["checks"][0] == response.json()
    assert "password" not in detail.text
    assert (await client.get("/api/proxies/99999")).status_code == 404
    assert (await client.post("/api/proxies/99999/test")).status_code == 404


async def test_test_all_returns_queue_count(api: tuple[httpx.AsyncClient, int]) -> None:
    import asyncio

    client, port = api
    assert (await client.post("/api/proxies/test-all")).json() == {"queued": 0}
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    response = await client.post("/api/proxies/test-all")
    assert response.status_code == 200
    assert response.json() == {"queued": 1}
    async with asyncio.timeout(3):
        while (await client.get("/api/proxies")).json()["items"][0]["status"] != "alive":
            await asyncio.sleep(0.01)


async def test_soft_delete_preserves_history_but_excludes_rotation_list(
    api: tuple[httpx.AsyncClient, int],
) -> None:
    client, port = api
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    await client.post(f"/api/proxies/{proxy_id}/test")
    response = await client.delete(f"/api/proxies/{proxy_id}")
    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get("/api/proxies")).json() == {"items": [], "total": 0}
    detail = (await client.get(f"/api/proxies/{proxy_id}")).json()
    assert detail["status"] == "dead"
    assert detail["checks"]
    assert (await client.get("/api/proxies", params={"status": "dead"})).json()["total"] == 1
    assert (await client.delete(f"/api/proxies/{proxy_id}")).status_code == 204
    assert (await client.delete("/api/proxies/99999")).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"wrong_field": "tester:synthetic-password@host:80"},
        {"text": {"nested": "tester:synthetic-password@host:80"}},
        {"text": "tester:synthetic-password@host:80", "label": {"secret": "synthetic-password"}},
    ],
)
async def test_validation_does_not_echo_credentials(
    api: tuple[httpx.AsyncClient, int], body: dict
) -> None:
    client, _ = api
    response = await client.post("/api/proxies/import", json=body)
    assert response.status_code == 422
    assert "synthetic-password" not in response.text


async def test_delete_cancels_running_probe_and_test_all_is_nonblocking(
    api: tuple[httpx.AsyncClient, int],
    probe_control: tuple[asyncio.Event, asyncio.Event],
) -> None:
    client, port = api
    release, entered = probe_control
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    await client.post(f"/api/proxies/{proxy_id}/test")
    release.clear()
    entered.clear()
    response = await asyncio.wait_for(client.post("/api/proxies/test-all"), 1)
    assert response.json() == {"queued": 1}
    await asyncio.wait_for(entered.wait(), 1)
    pending = asyncio.create_task(client.post(f"/api/proxies/{proxy_id}/test"))
    await asyncio.sleep(0.01)
    try:
        assert (await client.delete(f"/api/proxies/{proxy_id}")).status_code == 204
        assert (await pending).status_code == 404
        release.set()
        await asyncio.sleep(0.05)
        assert (await client.get(f"/api/proxies/{proxy_id}")).json()["status"] == "dead"
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_delete_during_import_probe_keeps_scheduler_running(
    api: tuple[httpx.AsyncClient, int],
    probe_control: tuple[asyncio.Event, asyncio.Event],
) -> None:
    client, port = api
    release, entered = probe_control
    release.clear()
    await client.post("/api/proxies/import", json={"text": f"first:fake@127.0.0.1:{port}"})
    await asyncio.wait_for(entered.wait(), 1)
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    await client.delete(f"/api/proxies/{proxy_id}")
    release.set()
    await client.post("/api/proxies/import", json={"text": f"second:fake@127.0.0.1:{port}"})
    async with asyncio.timeout(2):
        while (await client.get("/api/proxies")).json()["items"][0]["status"] != "alive":
            await asyncio.sleep(0.01)
    assert (await client.get(f"/api/proxies/{proxy_id}")).json()["status"] == "dead"


async def test_import_line_cap_and_malformed_json(api: tuple[httpx.AsyncClient, int]) -> None:
    client, port = api
    text = "# comment\n" * 1999 + f"tester:fake@127.0.0.1:{port}\nother:fake@host:80"
    report = (await client.post("/api/proxies/import", json={"text": text})).json()
    assert report["imported"] == 1
    assert report["skipped"] == 1
    assert report["errors"][0]["line"] == 2001
    response = await client.post(
        "/api/proxies/import",
        content='{"text":"synthetic-password',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "synthetic-password" not in response.text


async def test_detail_retains_latest_fifty_checks(
    api: tuple[httpx.AsyncClient, int], tmp_path: Path
) -> None:
    client, port = api
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    for _ in range(60):
        response = await client.post(f"/api/proxies/{proxy_id}/test")
        assert response.status_code == 200
    detail = (await client.get(f"/api/proxies/{proxy_id}")).json()
    assert len(detail["checks"]) == 50
    assert detail["checks"][0] == response.json()
    assert detail["checks"][0]["checked_at"] >= detail["checks"][-1]["checked_at"]
    import aiosqlite

    async with (
        aiosqlite.connect(tmp_path / "proxies.db") as conn,
        conn.execute("SELECT count(*) FROM proxy_checks WHERE proxy_id = ?", (proxy_id,)) as cursor,
    ):
        assert (await cursor.fetchone())[0] == 50


async def test_malformed_and_unauthenticated_lines_leave_only_valid_database_rows(
    api: tuple[httpx.AsyncClient, int], tmp_path: Path
) -> None:
    import aiosqlite

    client, port = api
    text = f"first:fake@127.0.0.1:{port}\n127.0.0.1:8080\nbroken\nsecond:fake@127.0.0.1:{port}"
    report = (await client.post("/api/proxies/import", json={"text": text})).json()
    assert report == {
        "imported": 2,
        "skipped": 2,
        "errors": [
            {"line": 2, "reason": "Invalid proxy format"},
            {"line": 3, "reason": "Invalid proxy format"},
        ],
    }
    async with (
        aiosqlite.connect(tmp_path / "proxies.db") as conn,
        conn.execute("SELECT count(*) FROM proxies") as cursor,
    ):
        assert (await cursor.fetchone())[0] == 2


async def test_test_all_ui_tracks_one_batch_until_completion(api, running_app, probe_control):
    client, port = api
    app, _ = running_app
    await app.state.runtime.checker.stop()
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    release, entered = probe_control
    release.clear()
    try:
        response = await client.post("/api/proxies/test-all", headers={"HX-Request": "true"})
        assert 'id="check-controls"' in response.text
        assert "disabled" in response.text
        await asyncio.wait_for(entered.wait(), 1)
        assert (await client.post("/api/proxies/test-all")).json() == {"queued": 0}
        pending = await client.get("/ui/check-status")
        assert "0 / 1" in pending.text
        assert '<progress id="batch-progress" value="0" max="1"' in pending.text
        release.set()
        async with asyncio.timeout(2):
            while app.state.runtime.checker.batch_running:
                await asyncio.sleep(0.01)
        complete = await client.get("/ui/check-status")
        assert "1 / 1" in complete.text
        assert '<progress id="batch-progress" value="1" max="1"' in complete.text
        assert "100 %" in complete.text
        assert "terminée" in complete.text
        assert " disabled" not in complete.text.replace("hx-disabled-elt", "")
        assert app.state.pool.all()[0].success_count == 1
    finally:
        release.set()
        await app.state.runtime.checker.stop()


async def test_gateway_panel_shows_live_activity_and_top_position(api, running_app):
    client, _ = api
    app, _ = running_app
    page = (await client.get("/")).text
    settings_page = (await client.get("/parametres")).text
    assert settings_page.index('id="strategy-form"') < settings_page.index('id="gateway-panel"')
    assert 'id="topnav-gateways"' in page
    assert 'id="gateway-panel"' not in page
    port = int(app.state.runtime.gateway.http.bound.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        async with asyncio.timeout(2):
            while app.state.runtime.gateway.http.active_conns != 1:
                await asyncio.sleep(0.01)
        snapshot = (await client.get("/ui/snapshot")).text
        assert ">En utilisation</span>" in snapshot
        assert "1 connexion(s) active(s)" in snapshot
        writer.write(b"GET http://target.invalid/ HTTP/1.1\r\nHost: target.invalid\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        await writer.wait_closed()
    async with asyncio.timeout(2):
        while app.state.runtime.gateway.http.active_conns:
            await asyncio.sleep(0.01)
    snapshot = (await client.get("/ui/snapshot")).text
    assert ">En utilisation</span>" not in snapshot
    assert "En écoute" in snapshot
    assert "1 requête(s) reçue(s)" in snapshot


async def test_activity_and_selection_api_exposes_only_public_proxy_metadata(api, running_app):
    from datetime import UTC, datetime

    from app.models import CheckResult

    client, port = api
    app, _ = running_app
    runtime = app.state.runtime
    await runtime.checker.stop()
    await client.post(
        "/api/proxies/import", json={"text": f"tester:synthetic-password@127.0.0.1:{port}"}
    )
    proxy = runtime.pool.all()[0]
    await runtime.pool.apply_check(
        CheckResult(
            proxy_id=proxy.id,
            ok=True,
            exit_ip="203.0.113.8",
            latency_ms=10,
            checked_at=datetime.now(UTC),
        )
    )
    runtime.pool.set_country("203.0.113.8", "FR", "France")
    with runtime.pool.activity.track(proxy.id, "HTTP"):
        data = await client.get("/api/gateway/activity")
        assert data.status_code == 200
        assert data.json()["active"][0]["proxy"]["id"] == proxy.id
        assert data.json()["active"][0]["country"] == "France"
        assert data.json()["active"][0]["exit"]["ip"] == "203.0.113.8"
        assert "synthetic-password" not in data.text
        assert "password_enc" not in data.text
        html = (await client.get("/ui/snapshot")).text
        assert "203.0.113.8" in html and "France" in html
    response = await client.post(
        "/api/pool/selection", json={"country": "FR", "min_score": 80, "pinned_only": True}
    )
    assert response.status_code == 200
    assert runtime.rotator.pick(runtime.pool) is None
    runtime.pool.pin(proxy.id)
    assert runtime.rotator.pick(runtime.pool).id == proxy.id
    assert (await client.post("/api/pool/selection", json={"min_score": 101})).status_code == 422


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_gateway_connection_probe(api, running_app, kind):
    client, port = api
    app, _ = running_app
    await app.state.runtime.checker.stop()
    empty = await client.post(f"/api/gateway/{kind}/test")
    assert empty.status_code == 200
    assert empty.json()["ok"] is False
    from tests.test_gateway_http import FakeUpstream

    upstream = FakeUpstream()
    await upstream.start()
    try:
        app.state.runtime.checker.probe_url = f"http://127.0.0.1:{port}/probe"
        await client.post(
            "/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{upstream.port}"}
        )
        proxy = app.state.runtime.pool.all()[0]
        await app.state.runtime.pool.mark_success(proxy.id, 10)
        result = await client.post(f"/api/gateway/{kind}/test")
        assert result.status_code == 200
        assert result.json()["ok"] is True
        assert result.json()["exit_ip"] == "203.0.113.30"
        assert result.json()["latency_ms"] >= 0
        async with asyncio.timeout(2):
            while app.state.runtime.pool.activity.active():
                await asyncio.sleep(0.01)
        assert app.state.runtime.pool.activity.recent()[0].gateway == kind.upper()
    finally:
        await upstream.stop()
    page = await client.get("/parametres")
    assert f'data-gateway-test="{kind}"' in page.text
