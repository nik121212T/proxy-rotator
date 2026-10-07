"""Pool snapshots and event delivery against real persistence."""

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from app.models import CheckResult
from app.parser import parse_line
from app.pool import Pool
from app.repository import insert_proxy, list_proxies


async def test_pool_loads_snapshot_and_publishes_success(db: aiosqlite.Connection) -> None:
    key = Fernet.generate_key()
    stored = await insert_proxy(db, parse_line("user:pw@proxy.example:80"), key)
    pool = await Pool.load(db, key)
    before = pool.all()
    first, second = pool.subscribe(), pool.subscribe()

    updated = await pool.mark_success(stored.id, 120)

    assert isinstance(before, tuple)
    assert before[0].success_count == 0
    assert pool.all() == (updated,)
    assert updated.success_count == 1
    assert updated.status == "alive"
    assert await list_proxies(db) == [updated]
    for subscriber in (first, second):
        event = await asyncio.wait_for(anext(subscriber), 1)
        assert event.type == "updated"
        assert event.proxy_id == stored.id
        assert event.payload == updated
        assert "password_enc" not in event.model_dump_json()
        await subscriber.aclose()


async def test_pool_parallel_mutations_and_lifecycle(db: aiosqlite.Connection) -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    pool = await Pool.load(db, Fernet.generate_key(), clock=lambda: now)
    stream = pool.subscribe()
    proxy = await pool.add(parse_line("user:pw@proxy.example:80"))
    assert (await anext(stream)).type == "added"
    await asyncio.gather(*(pool.mark_success(proxy.id, n) for n in range(100)))
    assert pool.all()[0].success_count == 100
    assert await list_proxies(db) == list(pool.all())
    events = [await anext(stream) for _ in range(100)]
    assert [event.payload.success_count for event in events] == list(range(1, 101))
    for _ in range(3):
        await pool.mark_fail(proxy.id, "Timeout")
    assert pool.all()[0].status == "quarantine"
    assert pool.all()[0].quarantine_until == now + timedelta(minutes=10)
    assert pool.active() == ()
    await pool.quarantine(proxy.id, minutes=2)
    assert pool.all()[0].quarantine_until == now + timedelta(minutes=2)
    await pool.remove(proxy.id)
    assert pool.all() == ()
    assert await list_proxies(db) == []
    assert (await anext(stream)).type == "updated"
    await stream.aclose()


async def test_failed_pool_write_preserves_snapshot(db: aiosqlite.Connection) -> None:
    pool = await Pool.load(db, Fernet.generate_key())
    proxy = await pool.add(parse_line("user:pw@proxy.example:80"))
    before = pool.all()
    async with db.execute(
        "CREATE TRIGGER reject_update BEFORE UPDATE ON proxies "
        "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
    ):
        pass
    with pytest.raises(aiosqlite.IntegrityError):
        await pool.mark_success(proxy.id, 10)
    assert pool.all() == before


async def test_check_result_is_persisted_and_fanned_out(db: aiosqlite.Connection) -> None:
    pool = await Pool.load(db, Fernet.generate_key())
    proxy = await pool.add(parse_line("user:pw@proxy.example:80"))
    stream = pool.subscribe()
    check = CheckResult(proxy_id=proxy.id, ok=False, error="Timeout", checked_at=datetime.now(UTC))
    await pool.apply_check(check)
    assert (await anext(stream)).type == "updated"
    event = await anext(stream)
    assert event.type == "check_result"
    assert event.payload == check
    async with db.execute("SELECT count(*) FROM proxy_checks") as cursor:
        assert (await cursor.fetchone())[0] == 1
    await stream.aclose()
