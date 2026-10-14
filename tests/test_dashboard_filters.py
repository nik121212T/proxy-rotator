from datetime import UTC, datetime

import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.health_checker import HealthChecker
from app.models import CheckResult
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator
from app.routes import pool as pool_routes
from app.routes import proxies, ui
from app.runtime import Runtime


@pytest.fixture
async def dashboard(db):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    app = FastAPI()
    runtime = Runtime(pool, HealthChecker(pool, key), Rotator(), "", "")
    app.state.runtime = runtime
    app.include_router(ui.router)
    app.include_router(proxies.router)
    app.include_router(pool_routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, pool, runtime, key


async def test_latest_check_error_survives_reload_and_clears_after_success(dashboard, db):
    client, pool, runtime, key = dashboard
    proxy = await pool.add(parse_line("tester:fake@proxy.invalid:80"))
    await pool.apply_check(
        CheckResult(
            proxy_id=proxy.id,
            ok=False,
            status_code=407,
            error="HTTP 407: proxy authentication refused",
            checked_at=datetime.now(UTC),
        )
    )
    for path in ["/", "/ui/snapshot", f"/ui/proxies/{proxy.id}"]:
        response = await client.get(path)
        assert "Identifiants refusés (HTTP 407)" in response.text
    runtime.pool = await Pool.load(db, key)
    assert "Identifiants refusés (HTTP 407)" in (await client.get("/")).text
    await runtime.pool.apply_check(
        CheckResult(
            proxy_id=proxy.id,
            ok=True,
            status_code=200,
            exit_ip="8.8.8.8",
            checked_at=datetime.now(UTC),
        )
    )
    assert "Identifiants refusés" not in (await client.get("/ui/snapshot")).text


async def test_unrecognized_check_errors_never_expose_raw_text(dashboard):
    client, pool, _, _ = dashboard
    proxy = await pool.add(parse_line("tester:fake@proxy.invalid:80"))
    await pool.apply_check(
        CheckResult(
            proxy_id=proxy.id,
            ok=False,
            error="secret-password-in-exception",
            checked_at=datetime.now(UTC),
        )
    )
    html = (await client.get("/")).text
    assert "secret-password-in-exception" not in html
    assert "Vérification échouée" in html


async def test_table_filters_combine_and_survive_live_refresh_without_changing_rotation(dashboard):
    import re

    client, pool, runtime, _ = dashboard
    for index, (city, country) in enumerate([("Évry", "FR"), ("Paris", "FR"), ("Paris", "US")]):
        proxy = await pool.add(parse_line(f"tester:fake@filter{index}.invalid:80"))
        ip = f"8.8.8.{index + 1}"
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id, ok=True, exit_ip=ip, latency_ms=10, checked_at=datetime.now(UTC)
            )
        )
        pool.set_country(ip, country, country, city=city)
    for path in ["/", "/ui/snapshot"]:
        response = await client.get(
            path, params={"city": "paris", "country": "fr", "status": "alive"}
        )
        assert re.findall(r'<tr id="proxy-(\d+)"', response.text) == ["2"]
        assert "1 proxy sur 3" in response.text
    assert runtime.rotator.selection.country is None
    assert len(pool.active()) == 3
    response = await client.get("/", params={"city": "evr"})
    assert re.findall(r'<tr id="proxy-(\d+)"', response.text) == ["1"]
    empty = await client.get("/ui/snapshot?city=Lyon")
    assert "Aucun proxy ne correspond aux filtres" in empty.text
    assert "Aucun proxy dans le pool" not in empty.text
    assert "Réinitialiser les filtres" in empty.text
    await pool.soft_delete(2)
    dead = await client.get("/ui/snapshot?status=dead")
    assert re.findall(r'<tr id="proxy-(\d+)"', dead.text) == ["2"]
    assert "Hors pool" in dead.text
    all_states = await client.get("/ui/snapshot?status=all")
    assert len(re.findall(r'<tr id="proxy-(\d+)"', all_states.text)) == 3


@pytest.mark.parametrize(
    ("error", "label"),
    [
        ("Timeout", "Délai dépassé"),
        ("Proxy connection failed", "Connexion au proxy impossible"),
        ("TLS certificate verification failed", "Certificat TLS non valide"),
        ("Invalid probe response", "Réponse de test invalide"),
        ("Latency spike", "Hausse importante de latence"),
    ],
)
async def test_known_failure_messages_are_actionable(dashboard, error, label):
    client, pool, _, _ = dashboard
    proxy = await pool.add(parse_line("tester:fake@proxy.invalid:80"))
    await pool.apply_check(
        CheckResult(proxy_id=proxy.id, ok=False, error=error, checked_at=datetime.now(UTC))
    )
    assert label in (await client.get("/ui/snapshot")).text
    await pool.remove(proxy.id)
    assert pool.latest_check(proxy.id) is None


async def test_row_fragments_honor_filters_and_explicit_dead_visibility(dashboard):
    client, pool, _, _ = dashboard
    proxy = await pool.add(parse_line("tester:fake@proxy.invalid:80"))
    await pool.soft_delete(proxy.id)
    visible = await client.get(f"/ui/proxies/{proxy.id}?status=dead")
    assert "Hors pool" in visible.text
    excluded = await client.get(f"/ui/proxies/{proxy.id}?status=alive")
    assert 'hx-swap-oob="delete"' in excluded.text
    empty = await client.get("/?city=%22%3E%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert(1)</script>" not in empty.text


async def test_priority_setting_belongs_to_rotation_and_survives_location_updates(dashboard):
    from app.models import RotationSelection

    client, _, runtime, _ = dashboard
    runtime.rotator.selection = RotationSelection(country="FR", city="Paris", min_score=70)
    response = await client.post(
        "/api/pool/strategy",
        json={
            "strategy": "round_robin",
            "pinned_only": True,
        },
    )
    assert response.status_code == 200
    assert runtime.rotator.selection.pinned_only is True
    assert runtime.rotator.selection.city == "Paris"
    assert runtime.rotator.selection.min_score == 70
    await client.post("/api/pool/selection", json={"country": "US", "city": "Boston"})
    assert runtime.rotator.selection.pinned_only is True
    await client.post("/api/pool/strategy", json={"strategy": "weighted"})
    assert runtime.rotator.selection.pinned_only is True
    assert runtime.rotator.strategy == "round_robin"
    html = (await client.get("/parametres")).text
    strategy_form = html.split('id="strategy-form"', 1)[1].split("</form>", 1)[0]
    location_form = html.split('id="selection-form"', 1)[1].split("</form>", 1)[0]
    assert 'name="pinned_only"' in strategy_form
    assert 'name="pinned_only"' not in location_form
    await client.post("/api/pool/strategy", json={"strategy": "weighted", "pinned_only": False})
    assert runtime.rotator.selection.pinned_only is False
    assert runtime.rotator.selection.city == "Boston"


async def test_mixed_authentication_survives_storage_export_and_live_rows(dashboard, db):
    client, pool, runtime, key = dashboard
    plain = await pool.add(parse_line("203.0.113.10:8080"))
    await pool.add(parse_line("tester:fake@203.0.113.10:8080"))
    runtime.pool = await Pool.load(db, key)
    assert len(runtime.pool.all()) == 2
    exported = runtime.pool.export_lines()
    assert "http://203.0.113.10:8080" in exported
    assert all(parse_line(line) for line in exported)
    for path in ["/", "/ui/snapshot"]:
        html = (await client.get(path)).text
        assert "Sans identifiants" in html
        assert "Avec identifiants" in html
    html = (await client.get(f"/ui/proxies/{plain.id}")).text
    assert "Sans identifiants" in html
    assert "Avec identifiants" not in html


async def test_import_reports_auth_counts_and_summary_refreshes(dashboard):
    client, pool, runtime, _ = dashboard
    text = "user:fake@proxy.invalid:80\nproxy.invalid:80\nproxy.invalid:80\nbroken"
    response = await client.post("/api/proxies/import", json={"text": text})
    report = response.json()
    assert report["imported"] == 2
    assert report["with_auth"] == 1
    assert report["without_auth"] == 1
    assert report["skipped"] == 2
    repeated = (await client.post("/api/proxies/import", json={"text": text})).json()
    assert repeated["with_auth"] == repeated["without_auth"] == 0
    for path in ["/", "/ui/snapshot"]:
        html = (await client.get(path)).text
        assert "<dt>Avec identifiants</dt><dd>1</dd>" in html
        assert "<dt>Sans identifiants</dt><dd>1</dd>" in html
    response = await client.post(
        "/api/proxies/import", json={"text": "another.invalid:8080"},
        headers={"HX-Request": "true"},
    )
    assert "0 avec identifiants" in response.text
    assert "1 sans identifiants" in response.text
    assert "<dt>Sans identifiants</dt><dd>2</dd>" in (await client.get("/ui/snapshot")).text
