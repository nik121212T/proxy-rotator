"""Selection policies over the pool's already-filtered snapshot."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.models import Proxy
from app.rotator import Rotator


def proxy(identifier: int, score: float = 80) -> Proxy:
    return Proxy(
        id=identifier,
        host="proxy.example",
        port=80,
        username="user",
        password_enc=b"synthetic",
        created_at=datetime.now(UTC),
        status="alive",
        score=score,
    )


@dataclass
class ActivePool:
    proxies: tuple[Proxy, ...]

    def active(self) -> tuple[Proxy, ...]:
        return self.proxies


@pytest.mark.parametrize("strategy", ["round_robin", "random", "weighted", "sticky"])
def test_empty_pool_always_fails_closed(strategy: str) -> None:
    assert Rotator(strategy).pick(ActivePool(())) is None


def test_round_robin_cycles_and_handles_removed_proxies() -> None:
    pool = ActivePool((proxy(1), proxy(2), proxy(3)))
    rotator = Rotator("round_robin")
    assert [rotator.pick(pool).id for _ in range(5)] == [1, 2, 3, 1, 2]
    pool.proxies = (proxy(1), proxy(3))
    assert rotator.pick(pool).id in {1, 3}


def test_random_and_weighted_follow_injected_rng() -> None:
    pool = ActivePool((proxy(1, 90), proxy(2, 10)))
    rotator = Rotator("random", rng=random.Random(7))  # noqa: S311 - deterministic test oracle
    expected = random.Random(7)  # noqa: S311 - deterministic test oracle
    assert [rotator.pick(pool).id for _ in range(50)] == [
        expected.choice(pool.proxies).id for _ in range(50)
    ]
    weighted = Rotator("weighted", rng=random.Random(7))  # noqa: S311 - deterministic test oracle
    assert sum(weighted.pick(pool).id == 1 for _ in range(2000)) > 1700


def test_sticky_key_expiry_and_removed_proxy() -> None:
    now = 0.0
    pool = ActivePool((proxy(1), proxy(2)))
    rotator = Rotator("sticky", clock=lambda: now, sticky_ttl_s=10)
    first = rotator.pick(pool, client_ip="127.0.0.1", target_domain="Example.COM")
    pool.proxies = tuple(reversed(pool.proxies))
    assert rotator.pick(pool, client_ip="127.0.0.1", target_domain="example.com.") == first
    now = 11
    expired = rotator.pick(pool, client_ip="127.0.0.1", target_domain="example.com")
    assert expired != first
    pool.proxies = (first,)
    assert rotator.pick(pool, client_ip="127.0.0.1", target_domain="example.com") == first


def test_invalid_strategy_is_not_silently_accepted() -> None:
    with pytest.raises(ValueError):
        Rotator("unsupported")


def test_selection_restricts_country_score_and_pins_without_fallback():
    class CountryPool(ActivePool):
        def country_code(self, proxy_id):
            return {1: "FR", 2: "US", 3: "FR"}.get(proxy_id)

        def pinned_ids(self):
            return frozenset({3})

    pool = CountryPool((proxy(1, 90), proxy(2, 95), proxy(3, 60)))
    rotator = Rotator("round_robin")
    from app.models import RotationSelection

    rotator.selection = RotationSelection(country="FR", min_score=80)
    assert rotator.pick(pool).id == 1
    rotator.selection = RotationSelection(country="FR", min_score=80, pinned_only=True)
    assert rotator.pick(pool) is None


def test_timed_rotation_holds_until_deadline_and_replaces_unavailable_proxy():
    now = 0.0
    pool = ActivePool((proxy(1), proxy(2), proxy(3)))
    rotator = Rotator("timed", clock=lambda: now)
    rotator.configure("timed", interval_minutes=2)
    assert rotator.pick(pool).id == 1
    now = 119.9
    assert rotator.pick(pool).id == 1
    now = 120
    assert rotator.pick(pool).id == 2
    pool.proxies = (proxy(1), proxy(3))
    assert rotator.pick(pool).id in {1, 3}
    pool.proxies = ()
    assert rotator.pick(pool) is None
