"""Async persistence operations; all external values use bound SQL parameters."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite

from app.crypto import encrypt_password
from app.models import CheckResult, Proxy, ProxyFilters
from app.parser import ParsedProxy


def _proxy_from_row(row: aiosqlite.Row) -> Proxy:
    values = dict(row)
    for field in ("created_at", "last_checked_at", "quarantine_until"):
        if values[field] is not None:
            value = datetime.fromisoformat(values[field])
            # SQLite datetime('now') is UTC but omits an explicit offset.
            values[field] = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return Proxy.model_validate(values)


async def insert_proxy(
    conn: aiosqlite.Connection, proxy: ParsedProxy, key: bytes, *, label: str | None = None
) -> Proxy:
    """Encrypt credentials and insert one record; a duplicate raises IntegrityError."""
    encrypted = await asyncio.to_thread(encrypt_password, proxy.password, key)
    async with conn.execute(
        "INSERT INTO proxies (scheme, host, port, username, password_enc, label) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
        (proxy.scheme, proxy.host, proxy.port, proxy.username, encrypted, label),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Insert did not return a proxy")
    return _proxy_from_row(row)


async def upsert_proxy(
    conn: aiosqlite.Connection, proxy: ParsedProxy, key: bytes, *, label: str | None = None
) -> Proxy:
    """Update credentials/label on conflict while preserving identity and health."""
    encrypted = await asyncio.to_thread(encrypt_password, proxy.password, key)
    async with conn.execute(
        "INSERT INTO proxies (scheme, host, port, username, password_enc, label) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (scheme, host, port, username) DO UPDATE SET "
        "password_enc = excluded.password_enc, label = excluded.label RETURNING *",
        (proxy.scheme, proxy.host, proxy.port, proxy.username, encrypted, label),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Upsert did not return a proxy")
    return _proxy_from_row(row)


async def list_proxies(
    conn: aiosqlite.Connection, filters: ProxyFilters | None = None
) -> list[Proxy]:
    """Return validated snapshots in stable ID order, including dead rows by default."""
    selected = filters or ProxyFilters()
    async with conn.execute(
        "SELECT * FROM proxies "
        "WHERE (? IS NULL OR status = ?) AND (? IS NULL OR score >= ?) "
        "ORDER BY id LIMIT ? OFFSET ?",
        (
            selected.status,
            selected.status,
            selected.min_score,
            selected.min_score,
            selected.limit if selected.limit is not None else -1,
            selected.offset,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_proxy_from_row(row) for row in rows]


async def delete_proxy(conn: aiosqlite.Connection, proxy_id: int) -> Proxy | None:
    """Delete a proxy and its checks after the domain has drained active tunnels."""
    async with conn.execute("DELETE FROM proxies WHERE id = ? RETURNING *", (proxy_id,)) as cursor:
        row = await cursor.fetchone()
    return _proxy_from_row(row) if row is not None else None


async def update_proxy_health(conn: aiosqlite.Connection, proxy: Proxy) -> Proxy:
    """Persist a domain-produced health snapshot without changing credentials."""
    async with conn.execute(
        "UPDATE proxies SET status=?, score=?, latency_ms=?, success_count=?, fail_count=?, "
        "consec_fails=?, last_checked_at=?, quarantine_until=? WHERE id=? RETURNING *",
        (
            proxy.status,
            proxy.score,
            proxy.latency_ms,
            proxy.success_count,
            proxy.fail_count,
            proxy.consec_fails,
            proxy.last_checked_at.isoformat() if proxy.last_checked_at else None,
            proxy.quarantine_until.isoformat() if proxy.quarantine_until else None,
            proxy.id,
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise KeyError("Proxy no longer exists")
    return _proxy_from_row(row)


@asynccontextmanager
async def _check_savepoint(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Make insert/prune atomic without committing an enclosing import batch."""
    async with conn.execute("SAVEPOINT record_check"):
        pass
    try:
        yield
        async with conn.execute("RELEASE SAVEPOINT record_check"):
            pass
    except BaseException:
        async with conn.execute("ROLLBACK TO SAVEPOINT record_check"):
            pass
        async with conn.execute("RELEASE SAVEPOINT record_check"):
            pass
        raise


async def record_check(conn: aiosqlite.Connection, check: CheckResult) -> CheckResult:
    """Persist an outcome and retain the latest 50 checks for this proxy only."""
    async with _check_savepoint(conn):
        async with conn.execute(
            "INSERT INTO proxy_checks "
            "(proxy_id, ok, status_code, latency_ms, exit_ip, error, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                check.proxy_id,
                int(check.ok),
                check.status_code,
                check.latency_ms,
                str(check.exit_ip) if check.exit_ip is not None else None,
                check.error,
                check.checked_at.isoformat(timespec="microseconds"),
            ),
        ):
            pass
        async with conn.execute(
            "DELETE FROM proxy_checks WHERE proxy_id = ? AND id NOT IN "
            "(SELECT id FROM proxy_checks WHERE proxy_id = ? "
            "ORDER BY checked_at DESC, id DESC LIMIT ?)",
            (check.proxy_id, check.proxy_id, 50),
        ):
            pass
    return check


async def list_checks(
    conn: aiosqlite.Connection, proxy_id: int, limit: int = 50
) -> tuple[CheckResult, ...]:
    if not 1 <= limit <= 50:
        raise ValueError("Check history limit must be between 1 and 50")
    async with conn.execute(
        "SELECT proxy_id, ok, status_code, latency_ms, exit_ip, error, checked_at "
        "FROM proxy_checks WHERE proxy_id=? ORDER BY checked_at DESC, id DESC LIMIT ?",
        (proxy_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_check_from_row(row) for row in rows)


def _check_from_row(row: aiosqlite.Row) -> CheckResult:
    values = dict(row)
    values["ok"] = bool(values["ok"])
    timestamp = datetime.fromisoformat(values["checked_at"])
    values["checked_at"] = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
    return CheckResult.model_validate(values)


async def iter_check_histories(
    conn: aiosqlite.Connection,
) -> AsyncIterator[tuple[int, tuple[CheckResult, ...]]]:
    """Stream histories in one query, retaining at most 50 records per proxy."""
    async with conn.execute(
        "SELECT proxy_id, ok, status_code, latency_ms, exit_ip, error, checked_at "
        "FROM proxy_checks ORDER BY proxy_id, checked_at DESC, id DESC"
    ) as cursor:
        identifier: int | None = None
        history: list[CheckResult] = []
        async for row in cursor:
            if identifier is not None and row["proxy_id"] != identifier:
                yield identifier, tuple(history)
                history = []
            identifier = row["proxy_id"]
            if len(history) < 50:
                history.append(_check_from_row(row))
        if identifier is not None:
            yield identifier, tuple(history)
