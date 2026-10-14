"""Real aiohttp client requests through a loopback fake upstream proxy."""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography.fernet import Fernet

from app.crypto import CryptoConfigError
from app.health_checker import HealthChecker
from app.parser import parse_line
from app.pool import Pool


@dataclass
class FakeProxy:
    port: int = 0
    status: int = 200
    payload: object = field(default_factory=lambda: {"ip": "203.0.113.10"})
    delay: float = 0
    active: int = 0
    peak: int = 0
    requests: int = 0
    authenticated: bool = False
    raw_body: bytes | None = None

    async def handle(self, request: web.Request) -> web.Response:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.requests += 1
        self.authenticated = request.headers.get("Proxy-Authorization", "").startswith("Basic ")
        try:
            await asyncio.sleep(self.delay)
            if self.raw_body is not None:
                return web.Response(body=self.raw_body, content_type="application/json")
            return web.json_response(self.payload, status=self.status)
        finally:
            self.active -= 1


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[FakeProxy]:
    fake = FakeProxy()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1, handler_cancellation=True)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        fake.port = runner.addresses[0][1]
        yield fake
    finally:
        await runner.cleanup()


async def test_probe_uses_upstream_auth_and_records_real_result(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:synthetic-secret@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid/?format=json")

    result = await checker.check_one(proxy.id)

    assert result.ok
    assert str(result.exit_ip) == "203.0.113.10"
    assert result.latency_ms is not None
    assert upstream.authenticated
    assert pool.all()[0].status == "alive"
    async with db.execute("SELECT count(*) FROM proxy_checks") as cursor:
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"ip": "203.0.113.10"}, 429),
        ({"ip": "invalid"}, 200),
        ({"other": "value"}, 200),
        (["not a mapping"], 200),
        ({"ip": "203.0.113.10", "padding": "x" * 70000}, 200),
    ],
)
@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_probe_rejects_bad_responses(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
    payload: object,
    status: int,
) -> None:
    upstream.payload, upstream.status = payload, status
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:synthetic-secret@127.0.0.1:{upstream.port}"))
    result = await HealthChecker(pool, key, probe_url="http://probe.invalid").check_one(proxy.id)
    assert not result.ok
    assert result.error
    assert "synthetic-secret" not in result.model_dump_json()
    assert pool.all()[0].fail_count == 1


async def test_timeout_is_safe_and_requests_for_same_proxy_coalesce(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    upstream.delay = 0.1
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid", timeout_s=0.02)
    first, second = await asyncio.gather(checker.check_one(proxy.id), checker.check_one(proxy.id))
    assert first == second
    assert not first.ok
    assert first.error == "Timeout"
    assert upstream.requests == 1
    assert pool.all()[0].fail_count == 1


async def test_50_proxies_complete_under_10_seconds_with_semaphore(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    upstream.delay = 0.02
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    for index in range(50):
        await pool.add(parse_line(f"user{index}:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid")
    started = time.perf_counter()
    results = await asyncio.wait_for(checker.check_all(), 10)
    assert time.perf_counter() - started < 10
    assert len(results) == 50
    assert all(result.ok for result in results)
    assert 1 < upstream.peak <= 20
    assert len(pool.active()) == 50


async def test_queued_probes_receive_their_full_timeout(db, upstream):
    upstream.delay = 3.2
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    for index in range(3):
        await pool.add(parse_line(f"user{index}:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid", concurrency=1, timeout_s=4)
    results = await asyncio.wait_for(checker.check_all(), 15)
    assert all(result.ok for result in results)
    assert upstream.requests == 3
    assert all(proxy.fail_count == 0 for proxy in pool.all())


async def test_background_import_interval_and_quarantine_retest(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    key = Fernet.generate_key()
    pool = await Pool.load(db, key, clock=lambda: now)
    checker = HealthChecker(
        pool,
        key,
        probe_url="http://probe.invalid",
        clock=lambda: now,
        poll_interval_s=0.01,
    )
    checker.start()
    try:
        proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
        async with asyncio.timeout(2):
            while pool.all()[0].success_count < 1:
                await asyncio.sleep(0.01)
        now += timedelta(seconds=301)
        async with asyncio.timeout(2):
            while pool.all()[0].success_count < 2:
                await asyncio.sleep(0.01)
        await pool.quarantine(proxy.id)
        assert checker.due() == ()
        now += timedelta(minutes=10)
        async with asyncio.timeout(2):
            while pool.all()[0].success_count < 3:
                await asyncio.sleep(0.01)
        assert pool.all()[0].status == "alive"
    finally:
        await checker.stop()
    requests = upstream.requests
    now += timedelta(hours=1)
    await asyncio.sleep(0.03)
    assert upstream.requests == requests


async def test_deleted_inflight_proxy_is_not_resurrected(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    upstream.delay = 0.05
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid")
    pending = asyncio.create_task(checker.check_one(proxy.id))
    async with asyncio.timeout(1):
        while not upstream.requests:
            await asyncio.sleep(0.001)
    await pool.remove(proxy.id)
    assert (await pending).ok
    assert pool.all() == ()


async def test_stop_cancels_inflight_network_without_counting_failure(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    upstream.delay = 0.3
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid")
    checker.start()
    async with asyncio.timeout(1):
        while not upstream.requests:
            await asyncio.sleep(0.001)
    await checker.stop()
    assert pool.all()[0].success_count == pool.all()[0].fail_count == 0


@pytest.mark.parametrize(
    "body",
    [b"not JSON", b"\xff", b"[" * 1500 + b"0" + b"]" * 1500],
    ids=["invalid-json", "invalid-encoding", "excessive-nesting"],
)
async def test_malformed_json_cannot_crash_checker(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
    body: bytes,
) -> None:
    upstream.raw_body = body
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    result = await HealthChecker(pool, key, probe_url="http://probe.invalid/").check_one(proxy.id)
    assert result.error == "Invalid probe response"
    assert not result.ok


async def test_wrong_key_is_config_failure_not_proxy_failure(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    pool = await Pool.load(db, Fernet.generate_key())
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, Fernet.generate_key(), probe_url="http://probe.invalid/")
    with pytest.raises(CryptoConfigError) as error:
        await checker.check_one(proxy.id)
    assert error.value.exit_code == 78
    assert pool.all()[0].fail_count == 0
    assert upstream.requests == 0


async def test_test_all_does_not_restart_finished_proxies_while_batch_runs(db, upstream):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    for index in range(3):
        await pool.add(parse_line(f"user{index}:fake@127.0.0.1:{upstream.port}"))
    upstream.delay = 0.08
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid", concurrency=1)
    try:
        assert checker.queue_all() == 3
        async with asyncio.timeout(2):
            while pool.all()[0].success_count != 1:
                await asyncio.sleep(0.005)
        assert checker.queue_all() == 0
        async with asyncio.timeout(2):
            while any(p.success_count == 0 for p in pool.all()):
                await asyncio.sleep(0.005)
        assert upstream.requests == 3
        assert all(p.success_count == 1 for p in pool.all())
    finally:
        await checker.stop()


async def test_proxy_that_failed_its_first_check_is_still_scheduled(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    """An ``unknown`` proxy with a past check must not be stranded forever.

    Before the fix, the failed check set ``last_checked_at`` while leaving the
    status at ``unknown``, which matched no branch of ``due()``: the proxy was
    never retested and never reached quarantine without manual action.
    """
    upstream.status = 403
    now = datetime(2026, 10, 7, tzinfo=UTC)
    key = Fernet.generate_key()
    pool = await Pool.load(db, key, clock=lambda: now)
    checker = HealthChecker(
        pool, key, probe_url="http://probe.invalid", clock=lambda: now, interval_s=300
    )
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))

    assert [p.id for p in checker.due()] == [proxy.id]
    await checker.check_all(checker.due())
    assert pool.all()[0].status == "unknown"
    assert pool.all()[0].fail_count == 1

    # The next sweep must still select it, every interval, until it quarantines.
    for _ in range(2):
        now += timedelta(seconds=301)
        due = checker.due()
        assert [p.id for p in due] == [proxy.id]
        await checker.check_all(due)

    final = pool.all()[0]
    assert final.consec_fails == 3
    assert final.status == "quarantine"
    assert final.quarantine_until == now + timedelta(minutes=10)


async def test_latency_spike_fails_the_probe(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    """SPEC section 6 latency spike: measured against the proxy's own median."""
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid")

    first = await checker.check_one(proxy.id)
    assert first.ok
    median = pool.all()[0].latency_ms
    assert median is not None

    # A response far slower than this proxy's own median is a burn signal.
    upstream.delay = max(0.3, (median * 4) / 1000)
    slow = await checker.check_one(proxy.id)

    assert not slow.ok
    assert slow.error == "Latency spike"
    assert pool.all()[0].fail_count == 1


async def test_a_steady_proxy_is_never_a_latency_spike(
    db: aiosqlite.Connection,
    upstream: FakeProxy,
) -> None:
    """The rule needs a ratio, not a fixed budget: a steady proxy stays alive."""
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"user:pw@127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid")
    upstream.delay = 0.05

    for _ in range(3):
        result = await checker.check_one(proxy.id)
        assert result.ok, result.error

    assert pool.all()[0].fail_count == 0


async def test_probe_without_credentials_sends_no_authorization(db, upstream):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line(f"127.0.0.1:{upstream.port}"))
    checker = HealthChecker(pool, key, probe_url="http://probe.invalid/?format=json")
    result = await checker.check_one(proxy.id)
    assert result.ok
    assert not upstream.authenticated
