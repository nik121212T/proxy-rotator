"""Bounded live events with explicit task/subscription ownership."""

import asyncio
import contextlib
from typing import cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events import Subscription
from app.models import CheckResult, Event, GatewayEvent, PoolStatsEvent, Proxy
from app.runtime import Runtime

router = APIRouter()
STATS_INTERVAL_S = 2.0


def public_event(event: Event) -> dict[str, object]:
    if isinstance(event, PoolStatsEvent | GatewayEvent):
        return event.model_dump(mode="json")
    if event.type == "removed":
        return {"type": "proxy_removed", "payload": {"proxy_id": event.proxy_id}}
    if isinstance(event.payload, CheckResult):
        return {
            "type": "check_result",
            "payload": event.payload.model_dump(mode="json", exclude={"exit_ip", "checked_at"}),
        }
    proxy = cast(Proxy, event.payload)
    return {
        "type": "proxy_updated",
        "payload": {
            "proxy_id": event.proxy_id,
            "status": proxy.status,
            "score": proxy.score,
            "consec_fails": proxy.consec_fails,
        },
    }


async def send_events(ws: WebSocket, runtime: Runtime, stream: Subscription) -> None:
    async def stats() -> None:
        await ws.send_json(
            {
                "type": "pool_stats",
                "payload": runtime.pool.stats(runtime.rotator.strategy).model_dump(mode="json"),
            }
        )

    event = asyncio.create_task(anext(stream))
    try:
        await stats()
        while True:
            done, _ = await asyncio.wait({event}, timeout=STATS_INTERVAL_S)
            if done:
                message = event.result()
                await ws.send_json(public_event(message))
                event = asyncio.create_task(anext(stream))
                if not isinstance(message, PoolStatsEvent):
                    await stats()
            else:
                await stats()
    finally:
        event.cancel()
        await asyncio.gather(event, return_exceptions=True)


@router.websocket("/ws/events")
async def events(ws: WebSocket) -> None:
    runtime: Runtime | None = ws.app.state.runtime
    if runtime is None:
        await ws.close(code=1013)
        return
    stream = runtime.pool.subscribe(capacity=100, drop_oldest=True)
    tasks: list[asyncio.Task[None]] = []
    try:
        await ws.accept()

        async def disconnected() -> None:
            while True:
                if (await ws.receive())["type"] == "websocket.disconnect":
                    return

        tasks = [
            asyncio.create_task(send_events(ws, runtime, stream)),
            asyncio.create_task(disconnected()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, OSError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        # The client may vanish while this teardown runs, cancelling the handler
        # mid-cleanup. These tasks are ours to reap, so the bounded cleanup must
        # not itself be cancelled (same pattern as HealthChecker.stop);
        # otherwise CancelledError escapes the handler instead of the route
        # finishing its teardown.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        await stream.aclose()
