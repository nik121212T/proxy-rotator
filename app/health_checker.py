"""Health probes routed exclusively through the selected upstream proxy."""

import asyncio
import ipaddress
import json
import ssl
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyError  # type: ignore[import-untyped]
from pydantic import SecretStr
from yarl import URL

from app.crypto import decrypt_password
from app.events import Subscription
from app.models import CheckResult, Proxy
from app.pool import Pool, utc_now


class HealthChecker:
    def __init__(
        self,
        pool: Pool,
        key: bytes,
        *,
        probe_url: str = "https://api.ipify.org?format=json",
        concurrency: int = 20,
        timeout_s: float = 8,
        batch_timeout_s: float = 10,
        interval_s: float = 300,
        poll_interval_s: float = 0.2,
        clock: Callable[[], datetime] = utc_now,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if concurrency < 1 or min(timeout_s, batch_timeout_s, interval_s, poll_interval_s) <= 0:
            raise ValueError("Health checker limits must be positive")
        url = URL(probe_url)
        if url.scheme not in {"http", "https"} or not url.host or url.user is not None:
            raise ValueError("Probe URL must be HTTP(S) without credentials")
        self.pool = pool
        self._key = key
        self.probe_url = str(url.with_path(url.raw_path, encoded=True).with_query(url.query))
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout_s = timeout_s
        self._batch_timeout_s = batch_timeout_s
        self._interval_s = interval_s
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._tls_context = tls_context
        self._jobs: dict[int, asyncio.Task[CheckResult]] = {}
        self._runner: asyncio.Task[None] | None = None
        self._suppressed: set[int] = set()

    async def _exchange(
        self, proxy: Proxy, password: SecretStr
    ) -> tuple[int, str | None, str | None]:
        connector: aiohttp.BaseConnector | None = None
        proxy_url: URL | None = None
        auth: aiohttp.BasicAuth | None = None
        if proxy.scheme == "socks5":
            connector = ProxyConnector(
                host=proxy.host,
                port=proxy.port,
                username=proxy.username,
                password=password.get_secret_value(),
                rdns=True,
            )
        else:
            proxy_url = URL.build(scheme=proxy.scheme, host=proxy.host, port=proxy.port)
            auth = aiohttp.BasicAuth(proxy.username, password.get_secret_value())
        async with (
            aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as session,
            session.get(
                self.probe_url,
                proxy=proxy_url,
                proxy_auth=auth,
                allow_redirects=False,
                ssl=self._tls_context if self._tls_context is not None else True,
            ) as response,
        ):
            if response.status != 200:
                return response.status, None, f"HTTP {response.status}"
            body = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                body.extend(chunk)
                if len(body) > 65536:
                    return 200, None, "Invalid probe response"
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or not isinstance(data.get("ip"), str):
                    raise ValueError("Invalid IP response")
                address = str(ipaddress.ip_address(data["ip"]))
            except (ValueError, RecursionError):
                return 200, None, "Invalid probe response"
            return 200, address, None

    async def probe(self, proxy: Proxy) -> CheckResult:
        async with self._semaphore:
            started = time.perf_counter()
            # Configuration failures propagate; never label them proxy failures.
            password = await asyncio.to_thread(
                decrypt_password,
                proxy.password_enc.get_secret_value(),
                self._key,
            )
            status: int | None = None
            address: str | None = None
            error: str | None = None
            try:
                async with asyncio.timeout(self._timeout_s):
                    status, address, error = await self._exchange(proxy, password)
            except TimeoutError:
                error = "Timeout"
            except (aiohttp.ClientError, ProxyError, OSError):
                error = "Proxy connection failed"
            return CheckResult(
                proxy_id=proxy.id,
                ok=error is None,
                status_code=status,
                latency_ms=round((time.perf_counter() - started) * 1000),
                exit_ip=address,
                error=error,
                checked_at=self._clock(),
            )

    async def _persist(self, result: CheckResult) -> None:
        try:
            await self.pool.apply_check(result)
        except KeyError:
            # A proxy deleted during its probe must never be resurrected.
            return

    async def _job(self, proxy: Proxy, deadline: float | None) -> CheckResult:
        try:
            async with asyncio.timeout_at(deadline):
                result = await self.probe(proxy)
        except TimeoutError:
            result = CheckResult(
                proxy_id=proxy.id,
                ok=False,
                error="Batch deadline exceeded",
                checked_at=self._clock(),
            )
        persistence = asyncio.create_task(self._persist(result))
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            # A measured outcome exists: complete its atomic DB/snapshot write.
            await persistence
        return result

    def _start_job(self, proxy: Proxy, deadline: float | None = None) -> asyncio.Task[CheckResult]:
        existing = self._jobs.get(proxy.id)
        if existing is not None:
            return existing
        task = asyncio.create_task(self._job(proxy, deadline))
        self._jobs[proxy.id] = task

        def finished(done: asyncio.Task[CheckResult]) -> None:
            self._jobs.pop(proxy.id, None)
            if not done.cancelled():
                done.exception()  # Observe failures even if a caller disconnects.

        task.add_done_callback(finished)
        return task

    async def check_one(self, proxy_id: int) -> CheckResult:
        proxy = next((proxy for proxy in self.pool.all() if proxy.id == proxy_id), None)
        if proxy is None:
            raise KeyError("Proxy is not in the pool")
        self._suppressed.discard(proxy_id)
        try:
            return await asyncio.shield(self._start_job(proxy))
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            raise KeyError("Proxy check cancelled") from None

    def queue_all(self) -> int:
        selected = self.pool.all()
        deadline = asyncio.get_running_loop().time() + self._batch_timeout_s * 0.9
        for proxy in selected:
            self._suppressed.discard(proxy.id)
            self._start_job(proxy, deadline)
        return len(selected)

    async def cancel_one(self, proxy_id: int) -> None:
        if any(proxy.id == proxy_id for proxy in self.pool.all()):
            self._suppressed.add(proxy_id)
        task = self._jobs.get(proxy_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def check_all(self, proxies: tuple[Proxy, ...] | None = None) -> tuple[CheckResult, ...]:
        # Reserve 10% of the batch budget for durable writes and publication.
        deadline = asyncio.get_running_loop().time() + self._batch_timeout_s * 0.9
        selected = self.pool.all() if proxies is None else proxies
        tasks = [self._start_job(proxy, deadline) for proxy in selected]
        try:

            async def observed(task: asyncio.Task[CheckResult]) -> CheckResult | None:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    caller = asyncio.current_task()
                    if caller is not None and caller.cancelling():
                        raise
                    # Removing one proxy must not cancel the batch or its scheduler.
                    return None

            results = await asyncio.gather(*(observed(task) for task in tasks))
            return tuple(result for result in results if result is not None)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def due(self) -> tuple[Proxy, ...]:
        now = self._clock()
        return tuple(
            proxy
            for proxy in self.pool.all()
            if proxy.id not in self._jobs
            and proxy.id not in self._suppressed
            and (
                (proxy.status == "unknown" and proxy.last_checked_at is None)
                or (
                    proxy.status == "alive"
                    and (
                        proxy.last_checked_at is None
                        or (now - proxy.last_checked_at).total_seconds() >= self._interval_s
                    )
                )
                or (
                    proxy.status == "quarantine"
                    and proxy.quarantine_until is not None
                    and proxy.quarantine_until <= now
                )
            )
        )

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Health checker is already started")
        self._runner = asyncio.create_task(self._run(self.pool.subscribe()))

    async def _run(self, stream: Subscription) -> None:
        event = asyncio.create_task(anext(stream))
        try:
            while True:
                due = self.due()
                if due:
                    await self.check_all(due)
                completed, _ = await asyncio.wait({event}, timeout=self._poll_interval_s)
                if completed:
                    try:
                        event.result()
                    except RuntimeError:
                        # Overflow means resync the immutable pool snapshot on the next loop.
                        await stream.aclose()
                        stream = self.pool.subscribe()
                    event = asyncio.create_task(anext(stream))
        finally:
            event.cancel()
            await asyncio.gather(event, return_exceptions=True)
            await stream.aclose()

    async def stop(self) -> None:
        try:
            if self._runner is not None:
                self._runner.cancel()
                with suppress(asyncio.CancelledError):
                    await self._runner
        finally:
            self._runner = None
            jobs = tuple(self._jobs.values())
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
