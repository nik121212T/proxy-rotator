import asyncio

import httpx2 as httpx


async def test_stats_and_strategy(api: tuple[httpx.AsyncClient, int]) -> None:
    client, port = api
    response = await client.get("/api/pool/stats")
    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "alive": 0,
        "quarantine": 0,
        "dead": 0,
        "avg_score": 0,
        "avg_latency_ms": None,
        "strategy": "weighted",
    }
    await client.post("/api/proxies/import", json={"text": f"tester:fake@127.0.0.1:{port}"})
    proxy_id = (await client.get("/api/proxies")).json()["items"][0]["id"]
    await client.post(f"/api/proxies/{proxy_id}/test")
    stats = (await client.get("/api/pool/stats")).json()
    assert stats["total"] == stats["alive"] == 1
    assert 0 <= stats["avg_score"] <= 100
    assert stats["avg_latency_ms"] >= 0
    changed = await client.post("/api/pool/strategy", json={"strategy": "round_robin"})
    assert changed.status_code == 200
    assert changed.json()["strategy"] == "round_robin"
    assert (
        await client.post("/api/pool/strategy", json={"strategy": "invalid"})
    ).status_code == 422
    assert (await client.get("/api/pool/stats")).json()["strategy"] == "round_robin"


async def test_strategy_publishes_stats_event(api, running_app) -> None:
    import asyncio

    client, _ = api
    app, _ = running_app
    stream = app.state.runtime.pool.subscribe()
    try:
        await client.post("/api/pool/strategy", json={"strategy": "sticky"})
        event = await asyncio.wait_for(anext(stream), 0.2)
        assert event.type == "pool_stats"
        assert event.payload.strategy == "sticky"
    finally:
        await stream.aclose()


async def test_clear_dead_preserves_alive_and_quarantine(api, running_app) -> None:
    client, port = api
    app, _ = running_app
    await client.post(
        "/api/proxies/import",
        json={
            "text": "\n".join(f"user{i}:fake@127.0.0.1:{port}" for i in range(3)),
        },
    )
    ids = [p["id"] for p in (await client.get("/api/proxies")).json()["items"]]
    for proxy_id in ids:
        await client.post(f"/api/proxies/{proxy_id}/test")
    await app.state.runtime.pool.quarantine(ids[1])
    await client.delete(f"/api/proxies/{ids[2]}")
    response = await client.post("/api/pool/clear-dead")
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    stats = (await client.get("/api/pool/stats")).json()
    assert (stats["total"], stats["alive"], stats["quarantine"], stats["dead"]) == (2, 1, 1, 0)
    assert (await client.get(f"/api/proxies/{ids[2]}")).status_code == 404
    assert (await client.post("/api/pool/clear-dead")).json() == {"deleted": 0}


async def test_clear_all_wipes_pool(api, running_app) -> None:
    client, port = api
    app, _ = running_app
    await client.post(
        "/api/proxies/import",
        json={"text": "\n".join(f"user{i}:fake@127.0.0.1:{port}" for i in range(3))},
    )
    assert (await client.get("/api/pool/stats")).json()["total"] == 3
    response = await client.post("/api/pool/clear-all")
    assert response.status_code == 200
    assert response.json() == {"deleted": 3}
    stats = (await client.get("/api/pool/stats")).json()
    assert (stats["total"], stats["alive"], stats["quarantine"], stats["dead"]) == (0, 0, 0, 0)
    assert app.state.runtime.pool.pinned_ids() == set()
    assert (await client.post("/api/pool/clear-all")).json() == {"deleted": 0}


async def test_clear_all_marks_dead_before_dropping_rows(api, running_app) -> None:
    """B8: clear_all emits an 'updated' event with status='dead' before the
    final 'removed' event for the same proxy, giving in-flight tunnels a
    window to finish naturally."""
    client, port = api
    app, _ = running_app
    await client.post(
        "/api/proxies/import",
        json={"text": f"userA:fake@127.0.0.1:{port}"},
    )
    pool = app.state.runtime.pool
    proxy_id = pool.all()[0].id
    stream = pool.subscribe(capacity=16, drop_oldest=False)
    seen: list[tuple[str, str | None]] = []
    try:
        await pool.clear_all(drain_timeout_s=0.0)
        for _ in range(8):
            try:
                event = await asyncio.wait_for(anext(stream), 0.5)
            except (TimeoutError, StopAsyncIteration):
                break
            payload = getattr(event, "payload", None)
            status = getattr(payload, "status", None) if payload is not None else None
            seen.append((event.type, status))
            if event.type == "removed":
                break
    finally:
        await stream.aclose()
    types_for_proxy = [
        (event_type, status)
        for event_type, status in seen
    ]
    updated_dead = next(
        (i for i, (t, s) in enumerate(types_for_proxy) if t == "updated" and s == "dead"),
        None,
    )
    removed = next(
        (i for i, (t, _) in enumerate(types_for_proxy) if t == "removed"),
        None,
    )
    assert updated_dead is not None, seen
    assert removed is not None and removed > updated_dead, seen
    assert pool.all() == ()
    assert proxy_id not in pool.pinned_ids()
