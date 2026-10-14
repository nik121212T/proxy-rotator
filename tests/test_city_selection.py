from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from app.models import CheckResult, RotationSelection
from app.parser import parse_line
from app.pool import Pool
from app.rotator import Rotator


@pytest.mark.parametrize("strategy", ["round_robin", "random", "weighted", "sticky", "timed"])
async def test_city_selection_fails_closed_and_tracks_exit_changes(db, strategy):
    pool = await Pool.load(db, Fernet.generate_key())
    for index, ip in enumerate(["8.8.8.8", "1.1.1.1", "9.9.9.9"]):
        proxy = await pool.add(parse_line(f"tester:fake@host{index}.invalid:80"))
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id,
                ok=True,
                exit_ip=ip,
                latency_ms=10,
                checked_at=datetime.now(UTC),
            )
        )
    pool.set_country("8.8.8.8", "FR", "France", city="Paris", region="Île-de-France")
    pool.set_country("1.1.1.1", "US", "United States", city="Paris", region="Texas")
    rotator = Rotator(strategy)
    rotator.selection = RotationSelection(country="FR", city=" paris ", region="île-de-france")
    selected = rotator.pick(pool, client_ip="local", target_domain="example.com")
    assert selected.id == pool.all()[0].id
    await pool.apply_check(
        CheckResult(
            proxy_id=selected.id,
            ok=True,
            exit_ip="4.2.2.2",
            latency_ms=10,
            checked_at=datetime.now(UTC),
        )
    )
    assert rotator.pick(pool, client_ip="local", target_domain="example.com") is None


async def test_imported_location_survives_service_restart_without_network(db):
    from app.geolocation import GeolocationService

    def service():
        return GeolocationService(
            db,
            enabled=False,
            provider_url="https://unused.invalid/{ip}",
            cache_ttl_s=60,
            timeout_s=1,
        )

    await service().remember_provider_location(
        "8.8.8.8",
        "France",
        "FR",
        city="Paris",
        region="Île-de-France",
    )
    hit = await service().lookup("8.8.8.8")
    assert (hit.country_code, hit.city, hit.region) == ("FR", "Paris", "Île-de-France")
    assert await service().lookup("1.1.1.1") is None


async def test_migration_preserves_proxies_and_refreshes_old_country_cache(tmp_path):
    import aiosqlite

    from app.db import migrate
    from app.migrations import MIGRATION_001, MIGRATION_002

    async with aiosqlite.connect(tmp_path / "old.db", isolation_level=None) as conn:
        await conn.executescript(MIGRATION_001 + MIGRATION_002 + "PRAGMA user_version = 2;")
        await conn.execute(
            "INSERT INTO proxies (scheme,host,port,username,password_enc) "
            "VALUES ('http','host.invalid',80,'tester',?)",
            (b"encrypted",),
        )
        await conn.execute(
            "INSERT INTO geo_cache VALUES ('8.8.8.8','France','FR','2026-01-01','provider')"
        )
        await migrate(conn)
        await migrate(conn)
        async with conn.execute("SELECT username FROM proxies") as cursor:
            assert (await cursor.fetchone())[0] == "tester"
        async with conn.execute("SELECT city, region FROM geo_cache") as cursor:
            assert await cursor.fetchall() == []


@pytest.mark.parametrize(
    "payload",
    [
        b'{"countryCode":"FR","country":"France","city":"Paris","regionName":"IDF"}',
        b'{"country_code":"FR","country_name":"France","city":"Paris","region":"IDF"}',
    ],
)
def test_location_provider_schemas(payload):
    from app.geolocation import _parse_city_payload

    assert _parse_city_payload(payload) == ("Paris", "IDF")


@pytest.mark.parametrize(
    "payload", [b"[]", b"bad", b'{"status":"fail","city":"Paris"}', b'{"city":123,"region":[]}']
)
def test_invalid_location_is_unknown(payload):
    from app.geolocation import _parse_city_payload

    assert _parse_city_payload(payload) == (None, None)


async def test_selection_api_and_settings_without_listeners(db):
    import httpx2 as httpx
    from fastapi import FastAPI

    from app.health_checker import HealthChecker
    from app.routes import pool as pool_routes
    from app.routes import ui
    from app.runtime import Runtime

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    checker = HealthChecker(pool, key)
    app = FastAPI()
    runtime = Runtime(pool, checker, Rotator(), "127.0.0.1:8888", "127.0.0.1:1080")
    app.state.runtime = runtime
    app.include_router(pool_routes.router)
    app.include_router(ui.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/pool/selection",
            json={
                "country": "FR",
                "city": " Paris ",
                "region": " Île-de-France ",
            },
        )
        assert response.status_code == 200
        assert response.json()["city"] == "Paris"
        assert runtime.rotator.pick(pool) is None
        page = await client.get("/parametres")
        assert page.status_code == 200
        assert 'id="selection-city"' in page.text
        assert 'value="Paris"' in page.text
        assert 'id="dichvusocks-city"' in page.text
        invalid = await client.post("/api/pool/selection", json={"city": "x" * 129})
        assert invalid.status_code == 422
        assert runtime.rotator.selection.city == "Paris"
        reset = await client.post("/api/pool/selection", json={"city": " ", "region": ""})
        assert reset.json()["city"] is None
        assert reset.json()["region"] is None


async def test_city_import_passes_filters_and_keeps_provider_location(db, monkeypatch):
    import httpx2 as httpx
    from fastapi import FastAPI

    from app.geolocation import GeolocationService
    from app.health_checker import HealthChecker
    from app.providers.dichvusocks import ProviderSock
    from app.routes import providers
    from app.runtime import Runtime

    class Provider:
        enabled = True
        filters = None

        async def list_socks(self, **filters):
            self.filters = filters
            return [{"id": 1}]

        async def view(self, sock_id):
            return ProviderSock(
                sock_id,
                "8.8.8.8",
                1080,
                "tester",
                "fake",
                None,
                "France",
                "FR",
                "IDF",
                "Paris",
                None,
                None,
            )

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    checker = HealthChecker(pool, key)
    monkeypatch.setattr(checker, "queue_all", lambda: 0)
    geo = GeolocationService(
        db, enabled=False, provider_url="https://unused.invalid/{ip}", cache_ttl_s=60, timeout_s=1
    )
    provider = Provider()
    runtime = Runtime(pool, checker, Rotator(), "", "", geo=geo, dichvusocks=provider)
    app = FastAPI()
    app.state.runtime = runtime
    app.include_router(providers.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/providers/dichvusocks/import",
            params={
                "country": "FR",
                "region": "IDF",
                "city": "Paris",
                "limit": 1,
            },
        )
        assert response.status_code == 200
        assert response.json()["imported"] == 1
        assert provider.filters["city"] == "Paris"
        assert provider.filters["region"] == "IDF"
        location = await geo.lookup("8.8.8.8")
        assert (location.city, location.region) == ("Paris", "IDF")
        proxy = pool.all()[0]
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id,
                ok=True,
                exit_ip="8.8.8.8",
                latency_ms=10,
                checked_at=datetime.now(UTC),
            )
        )
        geo.start(pool)
        try:
            import asyncio

            async with asyncio.timeout(1):
                while pool.city_name(proxy.id) is None:
                    await asyncio.sleep(0.01)
            runtime.rotator.selection = RotationSelection(country="FR", city="Paris")
            assert runtime.rotator.pick(pool).id == proxy.id
        finally:
            await geo.stop()


async def test_dashboard_city_sort_and_live_rows(db):
    import re

    import httpx2 as httpx
    from fastapi import FastAPI

    from app.health_checker import HealthChecker
    from app.routes import ui
    from app.runtime import Runtime

    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    for index, city in enumerate([None, "Zurich", "Évry"]):
        proxy = await pool.add(parse_line(f"tester:fake@city{index}.invalid:80"))
        ip = f"8.8.8.{index + 1}"
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id, ok=True, exit_ip=ip, latency_ms=10, checked_at=datetime.now(UTC)
            )
        )
        pool.set_country(ip, "FR", "France", city=city)
    app = FastAPI()
    app.state.runtime = Runtime(pool, HealthChecker(pool, key), Rotator(), "", "")
    app.include_router(ui.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for url in ["/?sort=city", "/ui/snapshot?sort=city"]:
            response = await client.get(url)
            assert response.status_code == 200
            assert re.findall(r'<tr id="proxy-(\d+)"', response.text) == ["3", "2", "1"]
            assert "Évry · France" in response.text
            assert "ville inconnue · France" in response.text
        page = await client.get("/?sort=city")
        assert 'value="city" selected' in page.text
        assert "Ville : A à Z" in page.text
        pool.set_country("8.8.8.2", "FR", "France", city="Amiens")
        refreshed = await client.get("/ui/snapshot?sort=city")
        assert re.findall(r'<tr id="proxy-(\d+)"', refreshed.text) == ["2", "3", "1"]
        row = await client.get("/ui/proxies/2")
        assert "Amiens · France" in row.text
