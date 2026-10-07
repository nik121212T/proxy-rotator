"""Proxy REST behavior through ASGI, real SQLite and a local HTTP upstream."""

import asyncio
from pathlib import Path

import httpx
import pytest


async def test_import_rejects_malformed_batch_and_skips_existing(
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
        "imported": 0,
        "skipped": 3,
        "errors": [{"line": 4, "reason": "Invalid proxy format"}],
    }
    assert "synthetic-password" not in response.text
    assert (await client.get("/api/proxies")).json()["total"] == 0
    clean = await client.post("/api/proxies/import", json={"text": valid})
    assert clean.json()["imported"] == 1
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
    assert report["imported"] == 0
    assert report["skipped"] == 2
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


async def test_malformed_batch_leaves_no_partial_database_rows(
    api: tuple[httpx.AsyncClient, int], tmp_path: Path
) -> None:
    import aiosqlite

    client, port = api
    text = f"first:fake@127.0.0.1:{port}\nbroken\nsecond:fake@127.0.0.1:{port}"
    report = (await client.post("/api/proxies/import", json={"text": text})).json()
    assert report == {
        "imported": 0,
        "skipped": 3,
        "errors": [{"line": 2, "reason": "Invalid proxy format"}],
    }
    async with (
        aiosqlite.connect(tmp_path / "proxies.db") as conn,
        conn.execute("SELECT count(*) FROM proxies") as cursor,
    ):
        assert (await cursor.fetchone())[0] == 0
