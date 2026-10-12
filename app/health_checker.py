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

from app.burn_detector import BurnDetector
from app.crypto import decrypt_password
from app.events import Subscription
from app.models import CheckBatchStatus, CheckResult, Proxy
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
        interval_s: float = 300,
        poll_interval_s: float = 0.2,
        clock: Callable[[], datetime] = utc_now,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if concurrency < 1 or min(timeout_s, interval_s, poll_interval_s) <= 0:
            raise ValueError("Health checker limits must be positive")
        url = URL(probe_url)
        if url.scheme not in {"http", "https"} or not url.host or url.user is not None:
            raise ValueError("Probe URL must be HTTP(S) without credentials")
        self.pool = pool
        self._key = key
        self.probe_url = str(url.with_path(url.raw_path, encoded=True).with_query(url.query))
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout_s = timeout_s
        self._interval_s = interval_s
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._tls_context = tls_context
        # Single source of truth for SPEC section 6; only the latency-spike rule
        # is used here, because a probe is the same kind of measurement as the
        # median it is compared against.
        self._burn = BurnDetector(timeout_s=timeout_s)
        self._jobs: dict[int, asyncio.Task[CheckResult]] = {}
        self._batch_jobs: dict[int, asyncio.Task[CheckResult]] = {}
        self._runner: asyncio.Task[None] | None = None
        self._suppressed: set[int] = set()

    async def _exchange(
        self, proxy: Proxy, password: SecretStr
    ) -> tuple[int, str | None, str | None]:
        connector: aiohttp.BaseConnector | None = None
        proxy_url: URL | None = None
        if proxy.scheme == "socks5":
            connector = ProxyConnector(
                host=proxy.host,
                port=proxy.port,
                username=proxy.username,
                password=password.get_secret_value(),
                rdns=True,
            )
        else:
            # Credentials travel inside the proxy URL: aiohttp 3.14 deprecates
            # both ``proxy_auth`` and ``aiohttp.BasicAuth``, and yarl encodes the
            # userinfo correctly (aiohttp derives Proxy-Authorization from it).
            proxy_url = URL.build(
                scheme=proxy.scheme,
                host=proxy.host,
                port=proxy.port,
                user=proxy.username,
                password=password.get_secret_value(),
            )
        async with (
            aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as session,
            session.get(
                self.probe_url,
                proxy=proxy_url,
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
            except aiohttp.ClientHttpProxyError as exc:
                status = exc.status if 100 <= exc.status <= 599 else None
                error = (
                    "HTTP 407: proxy authentication refused"
                    if status == 407
                    else f"Proxy CONNECT refused (HTTP {status})"
                )
            except aiohttp.ClientConnectorCertificateError:
                error = "TLS certificate verification failed"
            except (aiohttp.ClientError, ProxyError, OSError):
                error = "Proxy connection failed"
            latency_ms = round((time.perf_counter() - started) * 1000)
            if error is None:
                # SPEC section 6 latency spike, measured against this proxy's own
                # running median. Only that rule is taken: a timeout would have
                # raised above, and comparing a forwarded download to a probe
                # median would be meaningless.
                verdict = self._burn.classify(
                    elapsed_ms=latency_ms, median_latency_ms=proxy.latency_ms
                )
                if verdict.burned and verdict.reason == "Latency spike":
                    error = verdict.reason
            return CheckResult(
                proxy_id=proxy.id,
                ok=error is None,
                status_code=status,
                latency_ms=latency_ms,
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

    async def _job(self, proxy: Proxy) -> CheckResult:
        # probe() acquires the semaphore before starting its network timeout.
        result = await self.probe(proxy)
        persistence = asyncio.create_task(self._persist(result))
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            # A measured outcome exists: complete its atomic DB/snapshot write.
            await persistence
        return result

    def _start_job(self, proxy: Proxy) -> asyncio.Task[CheckResult]:
        existing = self._jobs.get(proxy.id)
        if existing is not None:
            return existing
        task = asyncio.create_task(self._job(proxy))
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
            task = self._batch_jobs.get(proxy_id) if self.batch_running else None
            return await asyncio.shield(task if task is not None else self._start_job(proxy))
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            raise KeyError("Proxy check cancelled") from None

    @property
    def batch_running(self) -> bool:
        return any(not task.done() for task in self._batch_jobs.values())

    @property
    def batch_status(self) -> CheckBatchStatus:
        completed = failed = cancelled = 0
        errors: dict[str, int] = {}
        for task in self._batch_jobs.values():
            if not task.done():
                continue
            completed += 1
            if task.cancelled():
                cancelled += 1
                continue
            if task.exception() is not None:
                reason = "Internal check error"
                failed += 1
            else:
                result = task.result()
                if result.ok:
                    continue
                failed += 1
                reason = result.error or "Check failed"
            errors[reason] = errors.get(reason, 0) + 1
        return CheckBatchStatus(
            running=self.batch_running,
            total=len(self._batch_jobs),
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            errors=errors,
        )

    def queue_all(self) -> int:
        if self.batch_running:
            return 0
        self._batch_jobs = {}
        selected = self.pool.all()
        for proxy in selected:
            self._suppressed.discard(proxy.id)
            self._batch_jobs[proxy.id] = self._start_job(proxy)
        return len(selected)

    async def cancel_one(self, proxy_id: int) -> None:
        if any(proxy.id == proxy_id for proxy in self.pool.all()):
            self._suppressed.add(proxy_id)
        task = self._jobs.get(proxy_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def check_all(self, proxies: tuple[Proxy, ...] | None = None) -> tuple[CheckResult, ...]:
        selected = self.pool.all() if proxies is None else proxies
        tasks = [self._start_job(proxy) for proxy in selected]
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
                (
                    proxy.status == "unknown"
                    and (
                        proxy.last_checked_at is None
                        or (now - proxy.last_checked_at).total_seconds() >= self._interval_s
                    )
                )
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
