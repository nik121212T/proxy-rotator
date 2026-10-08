"""Application-owned services shared by API dependencies."""

from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.gateway_http import HttpGateway
from app.gateway_socks import SocksGateway
from app.geolocation import GeolocationService
from app.health_checker import HealthChecker
from app.models import GatewayStatus, ListenerStatus
from app.pool import Pool
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
