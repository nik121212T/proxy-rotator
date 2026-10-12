"""Serialized write-through pool with immutable, lock-free read snapshots."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TypeVar

import aiosqlite

from app import repository
from app.activity import GatewayActivity
from app.db import transaction
from app.events import EventBus, Subscription
from app.models import (
    CheckResult,
    ExitObservation,
    GatewayEvent,
    PoolStats,
    PoolStatsEvent,
    Proxy,
    ProxyEvent,
    RotationStrategy,
)
from app.parser import ImportReport, ParsedProxy, parse_bulk

logger = logging.getLogger(__name__)
T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(UTC)


def health_score(proxy: Proxy, now: datetime) -> float:
    """SPEC section 4; unmeasured proxies have zero freshness."""
    if proxy.last_checked_at is None:
        return 0.0
    ratio = proxy.success_count / max(1, proxy.success_count + proxy.fail_count)
    latency = proxy.latency_ms if proxy.latency_ms is not None else 2000
    penalty = min(1.0, max(0.0, latency / 2000))
    age_minutes = max(0.0, (now - proxy.last_checked_at).total_seconds() / 60)
    return 100 * ratio * (1 - 0.3 * penalty) * math.exp(-age_minutes / 60)


class Pool:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        key: bytes,
        *,
        clock: Callable[[], datetime] = utc_now,
        min_score: float = 40,
        quarantine_minutes: int = 10,
    ) -> None:
        self._conn = conn
        self._key = key
        self._clock = clock
        self._min_score = min_score
        self._quarantine_minutes = quarantine_minutes
        self._snapshot: tuple[Proxy, ...] = ()
        self._latencies: dict[int, tuple[int | None, ...]] = {}
        self._pinned: set[int] = set()
        self._exits: dict[int, ExitObservation] = {}
        self._countries: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._events = EventBus()
        self.activity = GatewayActivity()

    @classmethod
    async def load(
        cls,
        conn: aiosqlite.Connection,
        key: bytes,
        *,
        clock: Callable[[], datetime] = utc_now,
        min_score: float = 40,
        quarantine_minutes: int = 10,
    ) -> "Pool":
        pool = cls(
            conn, key, clock=clock, min_score=min_score, quarantine_minutes=quarantine_minutes
        )
        pool._snapshot = tuple(await repository.list_proxies(conn))
        for proxy in pool._snapshot:
            history = await repository.list_checks(conn, proxy.id, limit=50)
            pool._latencies[proxy.id] = tuple(check.latency_ms for check in reversed(history[:20]))
            for check in reversed(history):
                pool._observe_exit(check)
        return pool

    def _observe_exit(self, check: CheckResult) -> None:
        if not check.ok or check.exit_ip is None:
            return
        old = self._exits.get(check.proxy_id)
        changed = old is not None and old.ip != check.exit_ip
        self._exits[check.proxy_id] = ExitObservation(
            ip=check.exit_ip, checked_at=check.checked_at,
            previous_ip=old.ip if changed and old is not None else (old.previous_ip if old else None),
            changed_at=check.checked_at if changed else (old.changed_at if old else None),
        )

    def exit_observations(self) -> dict[int, ExitObservation]:
        return dict(self._exits)

    def set_country(self, ip: str, code: str, name: str) -> None:
        self._countries[ip] = (code, name)

    def country_code(self, proxy_id: int) -> str | None:
        observed = self._exits.get(proxy_id)
        country = self._countries.get(str(observed.ip)) if observed else None
        return country[0] if country else None

    def country_name(self, proxy_id: int) -> str | None:
        observed = self._exits.get(proxy_id)
        country = self._countries.get(str(observed.ip)) if observed else None
        return country[1] if country else None

    def all(self) -> tuple[Proxy, ...]:
        return self._snapshot

    def active(self) -> tuple[Proxy, ...]:
        now = self._clock()
        current = (p.model_copy(update={"score": health_score(p, now)}) for p in self._snapshot)
        return tuple(p for p in current if p.status == "alive" and p.score >= self._min_score)

    # ── pinning ──────────────────────────────────────────────────────────

    def pinned_ids(self) -> frozenset[int]:
        return frozenset(self._pinned)

    def is_pinned(self, proxy_id: int) -> bool:
        return proxy_id in self._pinned

    def pin(self, proxy_id: int) -> bool:
        if not any(p.id == proxy_id for p in self._snapshot):
            raise KeyError(proxy_id)
        added = proxy_id not in self._pinned
        self._pinned.add(proxy_id)
        return added

    def unpin(self, proxy_id: int) -> bool:
        removed = proxy_id in self._pinned
        self._pinned.discard(proxy_id)
        return removed

    def export_lines(self, status: str | None = None) -> tuple[str, ...]:
        """Return canonical ``scheme://user:pass@host:port`` lines, filterable by status."""
        from app.crypto import decrypt_password  # noqa: PLC0415

        selected = self._snapshot if status is None else tuple(
            p for p in self._snapshot if p.status == status
        )
        lines: list[str] = []
        for proxy in selected:
            try:
                password = decrypt_password(proxy.password_enc.get_secret_value(), self._key)
            except Exception as exc:  # noqa: BLE001  skip rows whose decryption fails
                logger.info("export_decrypt_skipped proxy_id=%s err=%s", proxy.id, exc)
                continue
            lines.append(
                f"{proxy.scheme}://{proxy.username}:{password.get_secret_value()}"
                f"@{proxy.host}:{proxy.port}"
            )
        return tuple(lines)

    def stats(self, strategy: RotationStrategy) -> PoolStats:
        snapshot = self._snapshot
        latencies = [p.latency_ms for p in snapshot if p.latency_ms is not None]
        return PoolStats(
            total=len(snapshot), alive=sum(p.status == "alive" for p in snapshot),
            quarantine=sum(p.status == "quarantine" for p in snapshot),
            dead=sum(p.status == "dead" for p in snapshot),
            avg_score=sum(p.score for p in snapshot) / len(snapshot) if snapshot else 0,
            avg_latency_ms=sum(latencies) / len(latencies) if latencies else None,
            strategy=strategy,
        )

    def publish_gateway_event(self) -> None:
        self._events.publish(GatewayEvent())

    def publish_stats(self, strategy: RotationStrategy) -> PoolStats:
        stats = self.stats(strategy)
        self._events.publish(PoolStatsEvent(payload=stats))
        return stats

    async def clear_dead(self) -> int:
        async def write() -> int:
            dead = tuple(p for p in self._snapshot if p.status == "dead")
            async with transaction(self._conn):
                for proxy in dead:
                    await repository.delete_proxy(self._conn, proxy.id)
            removed = {p.id for p in dead}
            self._snapshot = tuple(p for p in self._snapshot if p.id not in removed)
            for proxy_id in removed:
                self._latencies.pop(proxy_id, None)
                self._exits.pop(proxy_id, None)
                self._pinned.discard(proxy_id)
                self._events.publish(ProxyEvent(type="removed", proxy_id=proxy_id))
            return len(removed)

        return await self._mutate(write)

    async def clear_all(self) -> int:
        async def write() -> int:
            ids = tuple(p.id for p in self._snapshot)
            async with transaction(self._conn):
                for proxy_id in ids:
                    await repository.delete_proxy(self._conn, proxy_id)
            self._snapshot = ()
            self._latencies.clear()
            self._exits.clear()
            self._pinned.clear()
            for proxy_id in ids:
                self._events.publish(ProxyEvent(type="removed", proxy_id=proxy_id))
            return len(ids)

        return await self._mutate(write)

    @property
    def subscriber_count(self) -> int:
        return self._events.subscriber_count

    def subscribe(self, *, capacity: int | None = None, drop_oldest: bool = False) -> Subscription:
        return self._events.subscribe(capacity=capacity, drop_oldest=drop_oldest)

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

    async def import_text(self, text: str, *, label: str | None = None) -> ImportReport:
        report = await asyncio.to_thread(parse_bulk, text)

        async def write() -> ImportReport:
            seen = {(p.scheme, p.host, p.port, p.username) for p in self._snapshot}
            accepted = [p for p in report.proxies
                        if (p.scheme, p.host, p.port, p.username) not in seen]
            saved = []
            async with transaction(self._conn):
                for proxy in accepted:
                    saved.append(await repository.insert_proxy(
                        self._conn, proxy, self._key, label=label,
                    ))
            self._snapshot = (*self._snapshot, *saved)
            for stored in saved:
                self._events.publish(ProxyEvent(type="added", proxy_id=stored.id, payload=stored))
            return ImportReport(imported=len(saved),
                                skipped=report.skipped + report.imported - len(saved),
                                errors=report.errors)

        return await self._mutate(write)

    async def detail(self, proxy_id: int) -> tuple[Proxy, tuple[CheckResult, ...]]:
        async with self._lock:
            proxy = self._get(proxy_id)
            return proxy, await repository.list_checks(self._conn, proxy_id)

    async def soft_delete(self, proxy_id: int) -> Proxy:
        async def write() -> Proxy:
            old = self._get(proxy_id)
            updated = old.model_copy(update={"status": "dead", "quarantine_until": None})
            saved = await repository.update_proxy_health(self._conn, updated)
            self._publish_proxy(saved)
            return saved

        return await self._mutate(write)

    async def remove(self, proxy_id: int) -> Proxy | None:
        async def write() -> Proxy | None:
            removed = await repository.delete_proxy(self._conn, proxy_id)
            if removed is not None:
                self._snapshot = tuple(p for p in self._snapshot if p.id != proxy_id)
                self._latencies.pop(proxy_id, None)
                self._exits.pop(proxy_id, None)
                self._pinned.discard(proxy_id)
                self._events.publish(ProxyEvent(type="removed", proxy_id=proxy_id))
            return removed

        return await self._mutate(write)

    async def mark_success(self, proxy_id: int, latency_ms: int) -> Proxy:
        return await self._outcome(proxy_id, True, latency_ms)

    async def mark_fail(self, proxy_id: int, reason: str) -> Proxy:
        # Callers pass a short SPEC section 6 label, never a raw exception text.
        # Logging it makes gateway-driven burns diagnosable; persisting it would
        # need a schema change and is deliberately deferred.
        logger.info("pool_mark_fail proxy_id=%s reason=%s", proxy_id, reason)
        return await self._outcome(proxy_id, False, None)

    async def mark_tunnel_success(self, proxy_id: int) -> Proxy:
        """Record liveness for a tunnel without touching the latency history.

        A CONNECT tunnel has no meaningful request latency; feeding a zero into
        the checks median would collapse ``latency_ms`` and inflate the score.
        """
        return await self._outcome(proxy_id, True, None)

    async def quarantine(self, proxy_id: int, minutes: int | None = None) -> Proxy:
        minutes = self._quarantine_minutes if minutes is None else minutes
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
                until = now + timedelta(minutes=self._quarantine_minutes)
            history = self._latencies.get(proxy_id, ())
            if latency_ms is not None:
                history = (*history, latency_ms)[-20:]
            measured = [value for value in history if value is not None]
            median_ms = round(median(measured)) if measured else old.latency_ms
            updated = Proxy.model_validate(
                {
                    **old.__dict__,
                    "status": status,
                    "latency_ms": median_ms,
                    "success_count": old.success_count + int(ok),
                    "fail_count": old.fail_count + int(not ok),
                    "consec_fails": failures,
                    "last_checked_at": now,
                    "quarantine_until": until,
                }
            )
            score = health_score(updated, self._clock())
            if check is not None and old.status == "quarantine" and score < 10:
                updated = updated.model_copy(update={"status": "dead", "quarantine_until": None})
            updated = updated.model_copy(update={"score": score})
            async with transaction(self._conn):
                if check:
                    await repository.record_check(self._conn, check)
                saved = await repository.update_proxy_health(self._conn, updated)
            self._latencies[proxy_id] = history
            if check:
                self._observe_exit(check)
            self._publish_proxy(saved)
            if check:
                self._events.publish(
                    ProxyEvent(type="check_result", proxy_id=proxy_id, payload=check)
                )
            return saved

        return await self._mutate(write)
