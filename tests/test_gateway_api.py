from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


def test_configured_gateway_status_does_not_claim_listening(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("test-master"),
        db_path=tmp_path / "proxies.db",
        gateway_http_port=9001,
        gateway_socks_port=9002,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/gateway/status")
        assert response.status_code == 200
        assert response.json() == {
            "http": {
                "configured": "127.0.0.1:9001",
                "bound": None,
                "active_conns": 0,
                "requests_total": 0,
            },
            "socks5": {
                "configured": "127.0.0.1:9002",
                "bound": None,
                "active_conns": 0,
                "requests_total": 0,
            },
        }
