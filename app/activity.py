"""Credential-free, per-connection gateway activity; bounded completed history."""

import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ConnectionActivity(BaseModel):
    model_config = ConfigDict(frozen=True)
    connection_id: int
    proxy_id: int
    gateway: Literal["HTTP", "SOCKS5"]
    started_at: datetime
    duration_s: float = 0


class GatewayActivity:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._clock = clock
        self._utc_clock = utc_clock
        self._next = 0
        self._active: dict[int, tuple[ConnectionActivity, float]] = {}
        self._recent: deque[ConnectionActivity] = deque(maxlen=20)

    @contextmanager
    def track(self, proxy_id: int, gateway: Literal["HTTP", "SOCKS5"]) -> Iterator[None]:
        self._next += 1
        ident = self._next
        record = ConnectionActivity(
            connection_id=ident, proxy_id=proxy_id, gateway=gateway, started_at=self._utc_clock()
        )
        start = self._clock()
        self._active[ident] = (record, start)
        try:
            yield
        finally:
            self._active.pop(ident, None)
            self._recent.appendleft(
                record.model_copy(update={"duration_s": max(0, self._clock() - start)})
            )

    def active(self) -> tuple[ConnectionActivity, ...]:
        return tuple(
            record.model_copy(update={"duration_s": max(0, self._clock() - start)})
            for record, start in self._active.values()
        )

    def active_count_for(self, proxy_id: int) -> int:
        return sum(1 for record, _ in self._active.values() if record.proxy_id == proxy_id)

    def recent(self) -> tuple[ConnectionActivity, ...]:
        return tuple(self._recent)
