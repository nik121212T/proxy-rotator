"""Application-owned services shared by API dependencies."""

import asyncio
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.gateway_http import HttpGateway
from app.gateway_socks import SocksGateway
from app.geolocation import GeolocationService
from app.health_checker import HealthChecker
from app.models import GatewayStatus, ListenerStatus
from app.pool import Pool
from app.providers.dichvusocks import DichvusocksClient
from app.rotator import Rotator


@dataclass(repr=False)
class Runtime:
    pool: Pool
    checker: HealthChecker
    rotator: Rotator
    http_configured: str
    socks5_configured: str
    http_gateway: HttpGateway | None = field(default=None)
    socks5_gateway: SocksGateway | None = field(default=None)
    geo: GeolocationService | None = field(default=None)
    dichvusocks: DichvusocksClient | None = field(default=None)
    gateway_test_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def activity(self) -> dict[str, object]:
        proxies = {proxy.id: proxy for proxy in self.pool.all()}
        exits = self.pool.exit_observations()
        from app.activity import ConnectionActivity

        def view(item: ConnectionActivity) -> dict[str, object]:
            proxy = proxies.get(item.proxy_id)
            observed = exits.get(item.proxy_id)
            return {
                **item.model_dump(mode="json"),
                "proxy": proxy.model_dump(mode="json") if proxy else None,
                "exit": observed.model_dump(mode="json") if observed else None,
                "country": self.pool.country_name(item.proxy_id),
                "country_code": self.pool.country_code(item.proxy_id),
            }

        return {
            "active": [view(item) for item in self.pool.activity.active()],
            "recent": [view(item) for item in self.pool.activity.recent()],
        }

    @property
    def gateway(self) -> GatewayStatus:
        """Live snapshot of listener state for the admin UI and API."""
        if self.http_gateway is not None:
            http_status = self.http_gateway.status(self.http_configured)
        else:
            http_status = ListenerStatus(configured=self.http_configured)
        if self.socks5_gateway is not None:
            socks5_status = self.socks5_gateway.status(self.socks5_configured)
        else:
            socks5_status = ListenerStatus(configured=self.socks5_configured)
        return GatewayStatus(http=http_status, socks5=socks5_status)


async def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = request.app.state.runtime
    if runtime is None:
        raise HTTPException(status_code=503, detail="Application is not ready")
    return runtime
