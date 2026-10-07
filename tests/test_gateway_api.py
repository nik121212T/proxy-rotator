from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_gateway_status_reflects_http_listening_and_socks5_pending(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        master_secret=SecretStr("test-master"),
        db_path=tmp_path / "proxies.db",
        gateway_http_port=0,  # ephemeral so the port is always free
        gateway_socks_port=9002,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/gateway/status")
        assert response.status_code == 200
        body = response.json()
        # HTTP gateway (T5.1) is bound and listening.
        assert body["http"]["configured"] == "127.0.0.1:0"
        assert body["http"]["bound"] is not None
        assert body["http"]["bound"].startswith("127.0.0.1:")
        assert body["http"]["active_conns"] == 0
        assert body["http"]["requests_total"] == 0
        # SOCKS5 (T5.3) is still pending; configured but not bound.
        assert body["socks5"] == {
            "configured": "127.0.0.1:9002",
            "bound": None,
            "active_conns": 0,
            "requests_total": 0,
        }
