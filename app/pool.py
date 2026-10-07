"""Serialized write-through pool with immutable, lock-free read snapshots."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import aiosqlite

from app import repository
from app.db import transaction
from app.events import EventBus, Subscription
from app.models import CheckResult, Proxy, ProxyEvent
from app.parser import ParsedProxy

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Pool:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        key: bytes,
        *,
        clock: Callable[[], datetime] = utc_now,
        min_score: float = 40,
    ) -> None:
        self._conn = conn
        self._key = key
        self._clock = clock
        self._min_score = min_score
        self._snapshot: tuple[Proxy, ...] = ()
        self._lock = asyncio.Lock()
        self._events = EventBus()

    @classmethod
    async def load(
        cls,
        conn: aiosqlite.Connection,
        key: bytes,
        *,
        clock: Callable[[], datetime] = utc_now,
        min_score: float = 40,
    ) -> "Pool":
        pool = cls(conn, key, clock=clock, min_score=min_score)
        pool._snapshot = tuple(await repository.list_proxies(conn))
        return pool

    def all(self) -> tuple[Proxy, ...]:
        return self._snapshot

    def active(self) -> tuple[Proxy, ...]:
        return tuple(p for p in self.all() if p.status == "alive" and p.score >= self._min_score)

    def subscribe(self) -> Subscription:
        return self._events.subscribe()

    def _get(self, proxy_id: int) -> Proxy:
        for proxy in self._snapshot:
            if proxy.id == proxy_id:
                return proxy
        raise KeyError("Proxy is not in the pool")

    def _publish_proxy(self, proxy: Proxy) -> None:
        self._snapshot = tuple(proxy if p.id == proxy.id else p for p in self._snapshot)
        self._events.publish(ProxyEvent(type="updated", proxy_id=proxy.id, payload=proxy))

    async def _mutate(self, operation: Callable[[], Awaitable[T]]) -> T:
        async def run() -> T:
            async with self._lock:
                return await operation()

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish the write and snapshot swap before releasing cancellation.
            await task
            raise

    async def add(self, proxy: ParsedProxy) -> Proxy:
        async def write() -> Proxy:
            saved = await repository.insert_proxy(self._conn, proxy, self._key)
            self._snapshot = (*self._snapshot, saved)
            self._events.publish(ProxyEvent(type="added", proxy_id=saved.id, payload=saved))
            return saved

        return await self._mutate(write)

    async def remove(self, proxy_id: int) -> Proxy | None:
        async def write() -> Proxy | None:
            removed = await repository.delete_proxy(self._conn, proxy_id)
            if removed is not None:
                self._snapshot = tuple(p for p in self._snapshot if p.id != proxy_id)
                self._events.publish(ProxyEvent(type="removed", proxy_id=proxy_id))
            return removed

        return await self._mutate(write)

    async def mark_success(self, proxy_id: int, latency_ms: int) -> Proxy:
        return await self._outcome(proxy_id, True, latency_ms)

    async def mark_fail(self, proxy_id: int, reason: str) -> Proxy:
        # Gateway reasons are classified by the caller; never persist raw exceptions.
        return await self._outcome(proxy_id, False, None)

    async def quarantine(self, proxy_id: int, minutes: int = 10) -> Proxy:
        if minutes < 1:
            raise ValueError("Quarantine duration must be positive")

        async def write() -> Proxy:
            old = self._get(proxy_id)
            updated = Proxy.model_validate(
                {
                    **old.__dict__,
                    "status": "quarantine",
                    "quarantine_until": self._clock() + timedelta(minutes=minutes),
                }
            )
            saved = await repository.update_proxy_health(self._conn, updated)
            self._publish_proxy(saved)
            return saved

        return await self._mutate(write)

    async def apply_check(self, check: CheckResult) -> Proxy:
        return await self._outcome(check.proxy_id, check.ok, check.latency_ms, check)

    async def _outcome(
        self,
        proxy_id: int,
        ok: bool,
        latency_ms: int | None,
        check: CheckResult | None = None,
    ) -> Proxy:
        async def write() -> Proxy:
            old = self._get(proxy_id)
            now = check.checked_at if check else self._clock()
            failures = 0 if ok else old.consec_fails + 1
            status = "alive" if ok else ("quarantine" if failures >= 3 else old.status)
            until = None if ok else old.quarantine_until
            if not ok and failures >= 3:
                until = now + timedelta(minutes=10)
            updated = Proxy.model_validate(
                {
                    **old.__dict__,
                    "status": status,
                    "latency_ms": latency_ms if latency_ms is not None else old.latency_ms,
                    "success_count": old.success_count + int(ok),
                    "fail_count": old.fail_count + int(not ok),
                    "consec_fails": failures,
                    "last_checked_at": now,
                    "quarantine_until": until,
                }
            )
            async with transaction(self._conn):
                if check:
                    await repository.record_check(self._conn, check)
                saved = await repository.update_proxy_health(self._conn, updated)
            self._publish_proxy(saved)
            if check:
                self._events.publish(
                    ProxyEvent(type="check_result", proxy_id=proxy_id, payload=check)
                )
            return saved

        return await self._mutate(write)
