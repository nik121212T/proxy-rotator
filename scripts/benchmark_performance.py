"""Isolated synthetic benchmark. Run: PYTHONPATH=. .venv/bin/python scripts/benchmark_performance.py."""

import asyncio
import json
import statistics
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import ClientSession, web
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.db import get_db
from app.gateway_http import HttpGateway
from app.health_checker import HealthChecker
from app.pool import Pool
from app.rotator import Rotator
from app.runtime import Runtime
from app.views import render


def measure(call, repeats):
    call()  # warm templates
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 2), result


async def benchmark(count, port):
    with tempfile.TemporaryDirectory(prefix="proxy-benchmark-") as directory:
        async with get_db(Path(directory) / "synthetic.db") as conn:
            key = Fernet.generate_key()
            encrypted = Fernet(key).encrypt(b"")
            now = datetime.now(UTC).isoformat()
            await conn.executemany(
                "INSERT INTO proxies (scheme,host,port,username,password_enc,status,score,success_count,latency_ms,last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("http", "127.0.0.1", port, f"user{i}", encrypted, "alive", 90, 1, 50, now)
                    for i in range(count)
                ],
            )
            await conn.executemany(
                "INSERT INTO proxy_checks (proxy_id,ok,latency_ms,exit_ip,checked_at) VALUES (?,1,50,?,?)",
                [(i + 1, "192.0.2.1", now) for i in range(count)],
            )
            await conn.commit()
            start = time.perf_counter()
            pool = await Pool.load(conn, key)
            load_ms = (time.perf_counter() - start) * 1000
            rotator = Rotator()
            runtime = Runtime(pool, HealthChecker(pool, key), rotator, "", "")
            request = Request(
                {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}
            )
            page_ms, page = measure(lambda: render(request, "dashboard.html", runtime), 3)
            row_ms, _ = measure(
                lambda: render(request, "_row_oob.html", runtime, {"proxy_id": 1, "append": False}),
                20,
            )
            pick_ms, _ = measure(lambda: rotator.pick(pool), 100)
            tracemalloc.start()
            render(request, "dashboard.html", runtime)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            gateway = HttpGateway(pool, rotator, key, port=0)
            await gateway.start()
            try:
                proxy_url = "http://" + gateway.status("").bound
                start = time.perf_counter()
                async with ClientSession() as client:
                    semaphore = asyncio.Semaphore(10)

                    async def get():
                        async with (
                            semaphore,
                            client.get("http://benchmark.invalid/", proxy=proxy_url) as response,
                        ):
                            assert response.status == 200
                            assert await response.read() == b"benchmark-ok"

                    await asyncio.gather(*(get() for _ in range(100)))
                rps = 100 / (time.perf_counter() - start)
            finally:
                await gateway.stop()
            return {
                "proxies": count,
                "load_ms": round(load_ms, 2),
                "dashboard_ms": page_ms,
                "row_ms": row_ms,
                "pick_ms": pick_ms,
                "html_bytes": len(page.body),
                "render_peak_mib": round(peak / 1024**2, 2),
                "gateway_local_rps": round(rps, 1),
            }


async def main():
    async def upstream(request):
        return web.Response(body=b"benchmark-ok")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", upstream)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        for count in (100, 1000, 10000):
            print(json.dumps(await benchmark(count, port)), flush=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
