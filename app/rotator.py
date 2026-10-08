"""Synchronous selection from immutable active snapshots on one event loop."""

import hashlib
import random
import time
from collections.abc import Callable
from typing import Protocol

from cachetools import TTLCache  # type: ignore[import-untyped]

from app.models import Proxy, RotationStrategy


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
        if strategy not in {"round_robin", "random", "weighted", "sticky"}:
            raise ValueError("Unsupported rotation strategy")
        if sticky_ttl_s <= 0:
            raise ValueError("Sticky TTL must be positive")
        self.strategy = strategy
        self._last_id: int | None = None
        self._rng = rng or random.SystemRandom()
        self._sticky: TTLCache[tuple[str, str], int] = TTLCache(4096, sticky_ttl_s, timer=clock)

    def pick(
        self,
        pool: ActivePool,
        *,
        client_ip: str = "",
        target_domain: str = "",
    ) -> Proxy | None:
        active = pool.active()
        if not active:
            return None
        # Pinned-first override: if any pinned proxy is active, pick among those.
        pinned_fn = getattr(pool, "pinned_ids", None)
        if callable(pinned_fn):
            pinned = pinned_fn()
            if pinned:
                narrowed = tuple(p for p in active if p.id in pinned)
                if narrowed:
                    active = narrowed
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
        previous = next((i for i, proxy in enumerate(active) if proxy.id == self._last_id), -1)
        selected = active[(previous + 1) % len(active)]
        self._last_id = selected.id
        return selected
