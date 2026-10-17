from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from app import repository
from app.models import CheckResult
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator
from app.views import render


async def test_pool_load_batches_histories_and_preserves_latest(db):
    key = Fernet.generate_key()
    for i in range(3):
        proxy = await repository.insert_proxy(db, parse_line(f"user{i}:fake@proxy.invalid:80"), key)
        for j in range(3):
            await repository.record_check(
                db,
                CheckResult(
                    proxy_id=proxy.id,
                    ok=j != 2,
                    latency_ms=j + 10,
                    exit_ip="192.0.2.1" if j != 2 else None,
                    checked_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )
    await db.commit()
    statements = []
    await db.set_trace_callback(statements.append)
    pool = await Pool.load(db, key)
    await db.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) <= 2
    for proxy in pool.all():
        assert pool.latest_check(proxy.id).ok is False
        assert pool._latencies[proxy.id] == (10, 11, 12)
        assert str(pool.exit_observations()[proxy.id].ip) == "192.0.2.1"


async def test_single_row_does_not_build_global_dashboard_context(db, monkeypatch):
    pool = await Pool.load(db, Fernet.generate_key())
    first = await pool.add(parse_line("one.invalid:80"))
    second = await pool.add(parse_line("two.invalid:80"))

    def unexpected(*args):
        pytest.fail("A single-row update must not prepare global dashboard data")

    monkeypatch.setattr(pool, "stats", unexpected)
    runtime = SimpleNamespace(pool=pool, rotator=Rotator(), activity=unexpected)
    request = Request({"type": "http", "query_string": b"", "headers": []})
    response = render(request, "_row_oob.html", runtime, {"proxy_id": first.id, "append": False})
    assert f'id="proxy-{first.id}"' in response.body.decode()
    assert second.host not in response.body.decode()


async def test_rotation_copies_only_selected_proxy_with_current_score(db, monkeypatch):
    from app.models import Proxy
    from app.pool import health_score

    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = await Pool.load(db, Fernet.generate_key(), clock=lambda: now)
    for i in range(8):
        proxy = await pool.add(parse_line(f"proxy{i}.invalid:80"))
        await pool.mark_success(proxy.id, 100)
    original = Proxy.model_copy
    copies = []

    def copy(self, **kwargs):
        copies.append(self.id)
        return original(self, **kwargs)

    monkeypatch.setattr(Proxy, "model_copy", copy)
    selected = Rotator().pick(pool)
    assert selected.score == health_score(pool._get(selected.id), now)
    assert copies == [selected.id]


async def test_rotation_rechecks_decay_and_quarantine_without_stale_cache(db):
    from datetime import timedelta

    now = datetime(2026, 1, 1, tzinfo=UTC)
    pool = await Pool.load(db, Fernet.generate_key(), clock=lambda: now)
    proxy = await pool.add(parse_line('proxy.invalid:80'))
    await pool.mark_success(proxy.id, 100)
    saved = pool.all()[0]
    rotator = Rotator('timed')
    assert rotator.pick(pool).id == proxy.id
    now += timedelta(hours=2)
    assert rotator.pick(pool) is None
    assert pool.all()[0] is saved
    await pool.mark_success(proxy.id, 100)
    assert rotator.pick(pool).id == proxy.id
    await pool.quarantine(proxy.id)
    assert rotator.pick(pool) is None
