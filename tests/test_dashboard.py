from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


def test_dashboard_and_local_assets(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            master_secret=SecretStr("ui-test"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
        )
    )
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="proxies-table"' in response.text
        assert "/static/htmx.min.js" in response.text
        assert "cdn." not in response.text
        assert "Aucun proxy" in response.text
        for path in ("htmx.min.js", "app.js", "styles.css"):
            assert client.get(f"/static/{path}").status_code == 200


def test_htmx_import_returns_html_and_escapes_labels(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            master_secret=SecretStr("ui-test"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
        )
    )
    with TestClient(app) as client:
        client.portal.call(app.state.runtime.checker.stop)
        response = client.post(
            "/api/proxies/import",
            headers={"HX-Request": "true"},
            json={
                "text": "tester:synthetic-password@proxy.invalid:80",
                "label": "<script>alert(1)</script>",
            },
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "1 proxy" in response.text
        assert response.headers["HX-Trigger"] == "pool-refresh"
        assert "synthetic-password" not in response.text
        page = client.get("/")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
        assert "<script>alert(1)</script>" not in page.text
        fragment = client.get("/ui/proxies/1?append=true")
        assert "hx-swap-oob=" in fragment.text
        assert 'id="proxy-1"' in fragment.text
        snapshot = client.get("/ui/snapshot")
        assert 'id="proxy-rows"' in snapshot.text
        assert "hx-swap-oob=" in snapshot.text
        assert "password" not in snapshot.text


def test_htmx_controls_and_error_feedback(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            master_secret=SecretStr("ui-test"),
            gateway_http_port=0,
            gateway_socks_port=0,
            db_path=tmp_path / "proxies.db",
        )
    )
    with TestClient(app) as client:
        headers = {"HX-Request": "true"}
        response = client.post("/api/proxies/import", headers=headers, json={"text": "invalid"})
        assert "Ligne 1" in response.text
        assert response.headers.get("X-Clear-Import") is None
        for path, body in (
            ("/api/proxies/test-all", None),
            ("/api/pool/clear-dead", None),
            ("/api/pool/strategy", {"strategy": "random"}),
        ):
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.headers["HX-Trigger"] == "pool-refresh"
        assert (client.get("/api/pool/stats")).json()["strategy"] == "random"
