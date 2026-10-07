"""Bounded fanout lifecycle without blocking pool mutations."""

import asyncio

import pytest

from app.events import EventBus
from app.models import ProxyEvent


async def test_slow_subscriber_resyncs_without_blocking_other_subscriber() -> None:
    bus = EventBus(capacity=1)
    slow, fast = bus.subscribe(), bus.subscribe()
    event = ProxyEvent(type="removed", proxy_id=1)
    bus.publish(event)
    assert await anext(fast) == event
    bus.publish(event)
    assert await anext(fast) == event
    with pytest.raises(RuntimeError, match="reload"):
        await anext(slow)
    await fast.aclose()


async def test_close_wakes_pending_subscriber() -> None:
    stream = EventBus().subscribe()
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await stream.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, 0.1)


async def test_slow_websocket_subscription_keeps_latest_100_events() -> None:
    bus = EventBus()
    stream = bus.subscribe(capacity=100, drop_oldest=True)
    for proxy_id in range(1, 151):
        bus.publish(ProxyEvent(type='removed', proxy_id=proxy_id))
    ids = [(await anext(stream)).proxy_id for _ in range(100)]
    assert ids == list(range(51, 151))
    await stream.aclose()
    assert bus.subscriber_count == 0
