"""Connection and migration behavior against real disposable SQLite files."""

from pathlib import Path

import aiosqlite
import pytest

from app.db import DatabaseVersionError, get_db, migrate, transaction


async def test_get_db_configures_wal_foreign_keys_and_autocommit(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "proxies.db"
    async with get_db(path) as conn:
        async with conn.execute("PRAGMA foreign_keys") as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with conn.execute("PRAGMA journal_mode") as cursor:
            assert (await cursor.fetchone())[0] == "wal"
        assert conn.isolation_level is None
        async with conn.execute("CREATE TABLE connection_probe (value TEXT)"):
            pass
        async with conn.execute("INSERT INTO connection_probe VALUES (?)", ("saved",)):
            pass

    async with (
        get_db(path) as reopened,
        reopened.execute("SELECT value FROM connection_probe") as cursor,
    ):
        assert (await cursor.fetchone())[0] == "saved"
    with pytest.raises(ValueError, match="no active connection"):
        await conn.execute("SELECT 1")


async def test_migrations_are_atomic_and_versioned(tmp_path: Path) -> None:
    migrations = (
        (
            "CREATE TABLE migration_probe (value INTEGER)",
            "INSERT INTO migration_probe VALUES (7)",
            "PRAGMA user_version = 1",
        ),
    )
    async with aiosqlite.connect(tmp_path / "migration.db", isolation_level=None) as conn:
        await migrate(conn, migrations=migrations)
        changes = conn.total_changes
        await migrate(conn, migrations=migrations)
        assert conn.total_changes == changes
        async with conn.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with conn.execute("SELECT value FROM migration_probe") as cursor:
            assert await cursor.fetchall() == [(7,)]


async def test_failed_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    broken = (("CREATE TABLE partial (value INTEGER)", "INVALID SQL", "PRAGMA user_version = 1"),)
    async with aiosqlite.connect(tmp_path / "migration.db", isolation_level=None) as conn:
        with pytest.raises(aiosqlite.OperationalError):
            await migrate(conn, migrations=broken)
        async with conn.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with conn.execute("SELECT name FROM sqlite_master WHERE name = ?", ("partial",)) as cur:
            assert await cur.fetchone() is None
        assert not conn.in_transaction


async def test_get_db_rejects_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    async with (
        aiosqlite.connect(path, isolation_level=None) as conn,
        conn.execute("PRAGMA user_version = 999"),
    ):
        pass

    with pytest.raises(DatabaseVersionError):
        async with get_db(path):
            pytest.fail("A newer database must not be opened by an older application")


async def test_transaction_rolls_back_on_cancellation_and_connection_closes(tmp_path: Path) -> None:
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        async with get_db(tmp_path / "cancelled.db") as conn:
            async with conn.execute("CREATE TABLE batch_probe (value INTEGER)"):
                pass
            async with transaction(conn):
                async with conn.execute("INSERT INTO batch_probe VALUES (?)", (1,)):
                    pass
                raise asyncio.CancelledError

    async with (
        get_db(tmp_path / "cancelled.db") as reopened,
        reopened.execute("SELECT count(*) FROM batch_probe") as cursor,
    ):
        assert (await cursor.fetchone())[0] == 0
    with pytest.raises(ValueError, match="no active connection"):
        await conn.execute("SELECT 1")
