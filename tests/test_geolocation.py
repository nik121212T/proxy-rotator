import asyncio

from aiohttp import web

from app.geolocation import GeolocationService


async def test_geolocation_coalesces_and_survives_caller_cancellation(db):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def provider(request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return web.json_response(
            {
                "status": "success",
                "country": "France",
                "countryCode": "FR",
                "city": "Paris",
                "regionName": "Île-de-France",
            }
        )

    app = web.Application()
    app.router.add_get("/{ip}", provider)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    service = GeolocationService(
        db,
        enabled=True,
        provider_url=f"http://127.0.0.1:{port}/{{ip}}",
        cache_ttl_s=60,
        timeout_s=2,
    )
    await service.ensure_schema()
    first = asyncio.create_task(service.lookup("8.8.8.8"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(service.lookup("8.8.8.8"))
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        result = await asyncio.wait_for(second, 0.5)
        assert result.country_code == "FR"
        assert result.city == "Paris"
        assert result.region == "Île-de-France"
        cached = await service.lookup("8.8.8.8")
        assert (cached.city, cached.region) == ("Paris", "Île-de-France")
        assert (await service.lookup("8.8.8.8")).source == "cache"
        assert calls == 1
    finally:
        release.set()
        first.cancel()
        if second:
            second.cancel()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await runner.cleanup()
