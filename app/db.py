"""Async SQLite connections and explicit transaction boundaries."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

MIGRATIONS: tuple[tuple[str, ...], ...] = ()


class DatabaseVersionError(ValueError):
    """The database schema does not match the supported migration sequence."""


@asynccontextmanager
async def transaction(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Commit a batch once, or roll it back on failure or cancellation.

    The caller exclusively owns this connection for the entire block.
    Nested transactions are rejected rather than accidentally committing
    another caller's work.
    """
    if conn.in_transaction:
        raise ValueError("A transaction is already active on this connection")
    async with conn.execute("BEGIN IMMEDIATE"):
        pass
    try:
        yield
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


async def migrate(
    conn: aiosqlite.Connection,
    *,
    migrations: tuple[tuple[str, ...], ...] | None = None,
) -> None:
    """Apply trusted, static SQL migrations, each ending in its version PRAGMA."""
    registered = MIGRATIONS if migrations is None else migrations
    async with transaction(conn):
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        if row is None or not 0 <= row[0] <= len(registered):
            raise DatabaseVersionError("Unsupported database schema version")
        for expected_version, statements in enumerate(registered[row[0]:], start=row[0] + 1):
            for statement in statements:
                async with conn.execute(statement):
                    pass
            async with conn.execute("PRAGMA user_version") as cursor:
                version = await cursor.fetchone()
            if version is None or version[0] != expected_version:
                raise DatabaseVersionError("Migration did not set its expected schema version")


@asynccontextmanager
async def get_db(path: Path = Path("data") / "proxies.db") -> AsyncIterator[aiosqlite.Connection]:
    """Own a connection for one task; do not share a transaction across tasks."""
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    async with aiosqlite.connect(path, isolation_level=None) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("PRAGMA foreign_keys = ON"):
            pass
        async with conn.execute("PRAGMA journal_mode = WAL"):
            pass
        await migrate(conn)
        yield conn
