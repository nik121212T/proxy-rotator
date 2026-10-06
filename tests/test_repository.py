"""Repository contracts exercised against the real SQLite schema."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from cryptography.fernet import Fernet

from app.crypto import decrypt_password
from app.db import get_db, transaction
from app.models import CheckResult, Proxy, ProxyFilters
from app.parser import parse_line
from app.repository import delete_proxy, insert_proxy, list_proxies, record_check, upsert_proxy


@pytest.fixture
def key() -> bytes:
    return Fernet.generate_key()


async def test_insert_proxy_returns_dto_and_encrypts_at_rest(
    db: aiosqlite.Connection, key: bytes, tmp_path: Path
) -> None:
    parsed = parse_line("user:synthetic-private-password@PROXY.example:8080")

    stored = await insert_proxy(db, parsed, key, label="paid")

    assert isinstance(stored, Proxy)
    assert stored.host == "proxy.example"
    assert stored.label == "paid"
    assert stored.status == "unknown"
    assert stored.created_at.tzinfo == UTC
    assert decrypt_password(stored.password_enc.get_secret_value(), key) == parsed.password
    async with db.execute("SELECT password_enc FROM proxies WHERE id = ?", (stored.id,)) as cursor:
        assert (await cursor.fetchone())[0] == stored.password_enc.get_secret_value()
    assert not db.in_transaction
    for path in tmp_path.glob("proxies.db*"):
        assert b"synthetic-private-password" not in path.read_bytes()


async def test_upsert_preserves_identity_and_health_updates_credentials(
    db: aiosqlite.Connection, key: bytes
) -> None:
    first = await upsert_proxy(db, parse_line("user:first@proxy.example:80"), key, label="old")
    async with db.execute(
        "UPDATE proxies SET status = ?, score = ?, success_count = ? WHERE id = ?",
        ("alive", 75.0, 4, first.id),
    ):
        pass
    label = "paid'); DROP TABLE proxies; --"
    updated = await upsert_proxy(db, parse_line("user:second@PROXY.example:80"), key, label=label)

    assert updated.id == first.id
    assert updated.created_at == first.created_at
    assert updated.status == "alive"
    assert updated.score == 75
    assert updated.success_count == 4
    assert updated.label == label
    assert decrypt_password(updated.password_enc.get_secret_value(), key).get_secret_value() == (
        "second"
    )
    assert await list_proxies(db) == [updated]
    with pytest.raises(aiosqlite.IntegrityError):
        await insert_proxy(db, parse_line("user:third@proxy.example:80"), key)


async def test_list_proxies_filters_and_paginates_dtos(
    db: aiosqlite.Connection, key: bytes
) -> None:
    for port, status, score in [(80, "alive", 90), (81, "alive", 30), (82, "dead", 5)]:
        proxy = await insert_proxy(db, parse_line(f"user:pw@proxy.example:{port}"), key)
        async with db.execute(
            "UPDATE proxies SET status = ?, score = ? WHERE id = ?", (status, score, proxy.id)
        ):
            pass

    assert len(await list_proxies(db)) == 3
    selected = await list_proxies(db, ProxyFilters(status="alive", min_score=40))
    assert len(selected) == 1
    assert isinstance(selected[0], Proxy)
    assert selected[0].port == 80
    page = await list_proxies(db, ProxyFilters(limit=1, offset=1))
    assert [proxy.port for proxy in page] == [81]
    assert await list_proxies(db, ProxyFilters(offset=20)) == []


async def test_delete_proxy_returns_removed_dto_and_cascades_checks(
    db: aiosqlite.Connection, key: bytes
) -> None:
    stored = await insert_proxy(db, parse_line("user:pw@proxy.example:80"), key)
    check = CheckResult(proxy_id=stored.id, ok=False, error="Timeout", checked_at=datetime.now(UTC))
    assert await record_check(db, check) == check

    assert await delete_proxy(db, stored.id) == stored
    assert await delete_proxy(db, stored.id) is None
    assert await list_proxies(db) == []
    async with db.execute("SELECT count(*) FROM proxy_checks") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_record_check_keeps_latest_50_per_proxy(db: aiosqlite.Connection, key: bytes) -> None:
    first = await insert_proxy(db, parse_line("user:pw@proxy.example:80"), key)
    second = await insert_proxy(db, parse_line("user:pw@proxy.example:81"), key)
    start = datetime(2026, 10, 6, tzinfo=UTC)
    for proxy_id in (first.id, second.id):
        for index in reversed(range(55)):
            check = CheckResult(
                proxy_id=proxy_id,
                ok=True,
                status_code=200,
                latency_ms=index,
                exit_ip="203.0.113.10",
                checked_at=start + timedelta(seconds=index),
            )
            assert await record_check(db, check) == check
        async with db.execute(
            "SELECT latency_ms FROM proxy_checks WHERE proxy_id = ? ORDER BY checked_at",
            (proxy_id,),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == list(range(5, 55))
    assert not db.in_transaction


async def test_record_check_rejects_missing_proxy(db: aiosqlite.Connection) -> None:
    with pytest.raises(aiosqlite.IntegrityError):
        await record_check(db, CheckResult(proxy_id=999, ok=False, checked_at=datetime.now(UTC)))
    assert not db.in_transaction


async def test_bulk_transaction_commits_once_and_is_invisible_until_commit(
    db: aiosqlite.Connection, key: bytes, tmp_path: Path
) -> None:
    statements: list[str] = []
    await db.set_trace_callback(statements.append)
    async with get_db(tmp_path / "proxies.db") as observer:
        async with transaction(db):
            first = await insert_proxy(db, parse_line("user:pw@proxy.example:80"), key)
            await insert_proxy(db, parse_line("user:pw@proxy.example:81"), key)
            await record_check(
                db, CheckResult(proxy_id=first.id, ok=False, checked_at=datetime.now(UTC))
            )
            assert await list_proxies(observer) == []
            assert db.in_transaction
        assert len(await list_proxies(observer)) == 2
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1


async def test_bulk_transaction_failure_rolls_back_every_record(
    db: aiosqlite.Connection, key: bytes
) -> None:
    parsed = parse_line("user:pw@proxy.example:80")
    with pytest.raises(aiosqlite.IntegrityError):
        async with transaction(db):
            await insert_proxy(db, parsed, key)
            await insert_proxy(db, parsed, key)
    assert await list_proxies(db) == []
    assert not db.in_transaction


async def test_prune_failure_rolls_back_check_but_preserves_outer_transaction(
    db: aiosqlite.Connection, key: bytes
) -> None:
    proxy = await insert_proxy(db, parse_line("user:pw@proxy.example:80"), key)
    check = CheckResult(proxy_id=proxy.id, ok=False, checked_at=datetime.now(UTC))
    for _ in range(50):
        await record_check(db, check)
    async with db.execute(
        "CREATE TRIGGER reject_prune BEFORE DELETE ON proxy_checks "
        "BEGIN SELECT RAISE(ABORT, 'synthetic prune failure'); END"
    ):
        pass

    async with transaction(db):
        survivor = await insert_proxy(db, parse_line("user:pw@proxy.example:81"), key)
        with pytest.raises(aiosqlite.IntegrityError, match="synthetic prune failure"):
            await record_check(db, check)
        assert db.in_transaction
    assert survivor in await list_proxies(db)
    async with db.execute("SELECT count(*) FROM proxy_checks") as cursor:
        assert (await cursor.fetchone())[0] == 50
