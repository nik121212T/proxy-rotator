"""Single-event-loop fanout with bounded subscriber queues."""

import asyncio
from collections.abc import AsyncIterator

from app.models import Event


class Subscription(AsyncIterator[Event]):
    def __init__(self, bus: "EventBus", capacity: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(capacity)
        self._closed = False

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await self._queue.get()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        if event is None:
            if self._closed:
                raise StopAsyncIteration
            self._closed = True
            raise RuntimeError("Event subscriber fell behind; reload the pool snapshot")
        return event

    async def aclose(self) -> None:
        self._closed = True
        self._bus._subscribers.discard(self)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class EventBus:
    def __init__(self, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("Subscriber capacity must be positive")
        self._capacity = capacity
        self._subscribers: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        subscriber = Subscription(self, self._capacity)
        self._subscribers.add(subscriber)
        return subscriber

    def publish(self, event: Event) -> None:
        for subscriber in tuple(self._subscribers):
            if subscriber._queue.full():
                self._subscribers.discard(subscriber)
                while not subscriber._queue.empty():
                    subscriber._queue.get_nowait()
                subscriber._queue.put_nowait(None)
            else:
                subscriber._queue.put_nowait(event)
