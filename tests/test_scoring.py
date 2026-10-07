"""Score formula, decay, median and quarantine re-test boundaries."""

import math
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from app.models import CheckResult, Proxy
from app.parser import parse_line
from app.pool import Pool, health_score

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def test_health_score_matches_spec_and_clamps_latency_and_future_age() -> None:
    proxy = Proxy(
        id=1,
        host="proxy.example",
        port=80,
        username="user",
        password_enc=b"synthetic",
        created_at=NOW,
        success_count=10,
        fail_count=10,
        latency_ms=1000,
        last_checked_at=NOW - timedelta(hours=1),
    )
    assert health_score(proxy, NOW) == pytest.approx(100 * 0.5 * 0.85 / math.e)
    assert health_score(proxy.model_copy(update={"latency_ms": 5000}), NOW) == pytest.approx(
        35 / math.e
    )
    assert (
        health_score(proxy.model_copy(update={"last_checked_at": NOW + timedelta(days=1)}), NOW)
        == 42.5
    )
    assert health_score(proxy.model_copy(update={"last_checked_at": None}), NOW) == 0


async def test_pool_median_decay_and_reload(db: aiosqlite.Connection) -> None:
    now = NOW
    key = Fernet.generate_key()
    pool = await Pool.load(db, key, clock=lambda: now)
    proxy = await pool.add(parse_line("user:pw@proxy.example:80"))
    for latency in [9999, *range(10, 30)]:
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id,
                ok=True,
                status_code=200,
                exit_ip="203.0.113.1",
                latency_ms=latency,
                checked_at=now,
            )
        )
    assert pool.all()[0].latency_ms == 20
    assert pool.active()[0].score == pytest.approx(99.7)
    restored = await Pool.load(db, key, clock=lambda: now)
    await restored.mark_success(proxy.id, 30)
    assert restored.all()[0].latency_ms == 20
    now += timedelta(hours=2)
    assert pool.active() == ()


async def test_low_score_after_quarantine_retest_becomes_dead(db: aiosqlite.Connection) -> None:
    pool = await Pool.load(db, Fernet.generate_key(), clock=lambda: NOW)
    proxy = await pool.add(parse_line("user:pw@proxy.example:80"))
    for _ in range(3):
        await pool.mark_fail(proxy.id, "Timeout")
    assert pool.all()[0].status == "quarantine"
    await pool.apply_check(
        CheckResult(
            proxy_id=proxy.id, ok=False, checked_at=NOW + timedelta(minutes=10), error="Timeout"
        )
    )
    assert pool.all()[0].status == "dead"
    assert pool.active() == ()
