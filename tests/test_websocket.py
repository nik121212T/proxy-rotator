import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.parser import parse_line


def test_websocket_mutation_and_disconnect_cleanup(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            master_secret=SecretStr("ws-test"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
        )
    )
    with TestClient(app) as client:
        runtime = app.state.runtime
        client.portal.call(runtime.checker.stop)
        baseline = runtime.pool.subscriber_count
        with client.websocket_connect("/ws/events") as ws:
            assert ws.receive_json()["type"] == "pool_stats"
            proxy = client.portal.call(runtime.pool.add, parse_line("tester:fake@proxy.invalid:80"))
            event = ws.receive_json()
            assert event["type"] == "proxy_updated"
            assert event["payload"]["proxy_id"] == proxy.id
            assert "password" not in str(event)
            assert ws.receive_json()["payload"]["total"] == 1
            from datetime import UTC, datetime

            from app.models import CheckResult

            result = CheckResult(
                proxy_id=proxy.id, ok=True, latency_ms=12, checked_at=datetime.now(UTC)
            )
            client.portal.call(runtime.pool.apply_check, result)
            assert ws.receive_json()["type"] == "proxy_updated"
            assert ws.receive_json()["type"] == "pool_stats"
            assert ws.receive_json()["type"] == "check_result"
            assert ws.receive_json()["type"] == "pool_stats"
            client.portal.call(runtime.pool.remove, proxy.id)
            assert ws.receive_json() == {"type": "proxy_removed", "payload": {"proxy_id": proxy.id}}
            assert ws.receive_json()["payload"]["total"] == 0

        async def cleaned() -> None:
            async with asyncio.timeout(1):
                while runtime.pool.subscriber_count != baseline:
                    await asyncio.sleep(0.001)

        client.portal.call(cleaned)
        assert runtime.pool.subscriber_count == baseline


def test_websocket_periodic_stats_and_gateway_hook(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            master_secret=SecretStr("ws-test"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
        )
    )
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "pool_stats"
        assert ws.receive_json()["type"] == "pool_stats"
        client.portal.call(app.state.runtime.pool.publish_gateway_event)
        assert ws.receive_json() == {"type": "gateway_event", "payload": {}}
