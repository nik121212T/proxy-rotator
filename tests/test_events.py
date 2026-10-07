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
