"""Migrations 001 and 002 must match SPEC section 9 exactly."""

from pathlib import Path

import aiosqlite
import pytest

from app.db import migrate, transaction


async def schema(conn: aiosqlite.Connection) -> list[tuple[str, str, str]]:
    async with conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def test_migrations_match_spec_and_are_idempotent(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    spec = (Path(__file__).parents[1] / "SPEC.md").read_text(encoding="utf-8")
    specified_sql = spec.split("```sql\n", 1)[1].split("```", 1)[0]
    async with aiosqlite.connect(tmp_path / "reference.db") as reference:
        await reference.executescript(specified_sql)
        assert await schema(db) == await schema(reference)

    async with db.execute("PRAGMA user_version") as cursor:
        assert (await cursor.fetchone())[0] == 2
    async with db.execute("PRAGMA schema_version") as cursor:
        before = (await cursor.fetchone())[0]
    await migrate(db)
    async with db.execute("PRAGMA schema_version") as cursor:
        assert (await cursor.fetchone())[0] == before


@pytest.mark.parametrize(
    ("scheme", "port", "status"),
    [("ftp", 80, "unknown"), ("http", 0, "unknown"), ("http", 65536, "unknown"),
     ("http", 80, "invalid")],
)
async def test_schema_enforces_proxy_constraints(
    db: aiosqlite.Connection, scheme: str, port: int, status: str
) -> None:
    with pytest.raises(aiosqlite.IntegrityError):
        async with db.execute(
            "INSERT INTO proxies (scheme, host, port, username, password_enc, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scheme, "proxy.example", port, "user", b"synthetic-token", status),
        ):
            pass


async def test_schema_enforces_identity_and_check_constraints(db: aiosqlite.Connection) -> None:
    insert = (
        "INSERT INTO proxies (scheme, host, port, username, password_enc) VALUES (?, ?, ?, ?, ?)"
    )
    values = ("http", "proxy.example", 80, "user", b"synthetic-token")
    async with db.execute(insert, values) as cursor:
        proxy_id = cursor.lastrowid
    with pytest.raises(aiosqlite.IntegrityError):
        async with db.execute(insert, values):
            pass
    for parent, ok in [(999999, 1), (proxy_id, 2)]:
        with pytest.raises(aiosqlite.IntegrityError):
            async with db.execute("INSERT INTO proxy_checks (proxy_id, ok) VALUES (?, ?)", (parent, ok)):
                pass
    async with db.execute("INSERT INTO proxy_checks (proxy_id, ok) VALUES (?, ?)", (proxy_id, 1)):
        pass
    await migrate(db)
    async with db.execute("SELECT count(*) FROM proxies") as cursor:
        assert (await cursor.fetchone())[0] == 1
    async with db.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,)):
        pass
    async with db.execute("SELECT count(*) FROM proxy_checks") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_up_to_date_migration_does_not_block_wal_reader(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    async with (
        transaction(db),
        aiosqlite.connect(tmp_path / "proxies.db", isolation_level=None, timeout=0.05) as reader,
    ):
        await migrate(reader)
        async with reader.execute("SELECT count(*) FROM proxies") as cursor:
            assert (await cursor.fetchone())[0] == 0
