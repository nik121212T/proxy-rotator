"""Synchronous selection from immutable active snapshots on one event loop."""

import hashlib
import random
import time
from collections.abc import Callable
from typing import Protocol

from cachetools import TTLCache  # type: ignore[import-untyped]

from app.models import Proxy, RotationSelection, RotationStrategy


class ActivePool(Protocol):
    def active(self) -> tuple[Proxy, ...]: ...
    def pinned_ids(self) -> frozenset[int]: ...


class Rotator:
    def __init__(
        self,
        strategy: RotationStrategy = "weighted",
        *,
        rng: random.Random | None = None,
        sticky_ttl_s: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if strategy not in {"round_robin", "random", "weighted", "sticky", "timed"}:
            raise ValueError("Unsupported rotation strategy")
        if sticky_ttl_s <= 0:
            raise ValueError("Sticky TTL must be positive")
        self._clock = clock
        self.interval_minutes = 1
        self._timed_id: int | None = None
        self._deadline = 0.0
        self.selection = RotationSelection()
        self.strategy = strategy
        self._last_id: int | None = None
        self._rr_cursor = 0
        self._rng = rng or random.SystemRandom()
        self._sticky: TTLCache[tuple[str, str], int] = TTLCache(4096, sticky_ttl_s, timer=clock)

    def configure(self, strategy: RotationStrategy, *, interval_minutes: int = 1) -> None:
        if not 1 <= interval_minutes <= 1440:
            raise ValueError("Interval must be between 1 and 1440 minutes")
        self.strategy = strategy
        self.interval_minutes = interval_minutes
        self._timed_id = None
        self._deadline = 0.0

    def pick(
        self,
        pool: ActivePool,
        *,
        client_ip: str = "",
        target_domain: str = "",
    ) -> Proxy | None:
        country_of = getattr(pool, "country_code", lambda proxy_id: None)
        active = tuple(
            p
            for p in pool.active()
            if p.score >= self.selection.min_score
            and (self.selection.country is None or country_of(p.id) == self.selection.country)
        )
        if not active:
            return None
        # Pinned-first override: if any pinned proxy is active, pick among those.
        pinned_fn = getattr(pool, "pinned_ids", None)
        if callable(pinned_fn):
            pinned = pinned_fn()
            if pinned or self.selection.pinned_only:
                narrowed = tuple(p for p in active if p.id in pinned)
                if narrowed or self.selection.pinned_only:
                    active = narrowed
        if not active:
            return None
        if self.strategy == "timed":
            match = next((p for p in active if p.id == self._timed_id), None)
            if match is not None and self._clock() < self._deadline:
                return match
            previous = next((i for i, p in enumerate(active) if p.id == self._timed_id), -1)
            selected = active[(previous + 1) % len(active)]
            self._timed_id = selected.id
            self._deadline = self._clock() + self.interval_minutes * 60
            return selected
        if self.strategy == "random":
            return self._rng.choice(active)
        if self.strategy == "weighted":
            weights = [proxy.score for proxy in active]
            return self._rng.choices(active, weights=weights, k=1)[0] if sum(weights) > 0 else None
        if self.strategy == "sticky":
            if not client_ip or not target_domain:
                raise ValueError("Sticky rotation requires client IP and target domain")
            key = (client_ip, target_domain.lower().rstrip("."))
            cached = self._sticky.get(key)
            match = next((proxy for proxy in active if proxy.id == cached), None)
            if match is not None:
                return match
            digest = hashlib.sha256((key[0] + "\0" + key[1]).encode()).digest()
            selected = active[int.from_bytes(digest, "big") % len(active)]
            self._sticky[key] = selected.id
            return selected
        # Monotonic cursor so a proxy leaving the active set does not collapse
        # the rotation to index 0 on the next tick (audit B4). We still prefer
        # resuming right after the previously-picked proxy when it is still
        # present, which keeps the natural ordering intact.
        previous = next((i for i, proxy in enumerate(active) if proxy.id == self._last_id), None)
        if previous is not None:
            index = (previous + 1) % len(active)
        else:
            index = self._rr_cursor % len(active)
        selected = active[index]
        self._rr_cursor = (index + 1) % max(len(active), 1)
        self._last_id = selected.id
        return selected
