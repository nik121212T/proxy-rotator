"""Pure classification of observable burn signals; never log response bodies."""

import errno

from pydantic import BaseModel, ConfigDict


class BurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    burned: bool
    reason: str | None = None


class BurnDetector:
    def __init__(self, *, timeout_s: float = 15, inspect_body: bool = True) -> None:
        if timeout_s <= 0:
            raise ValueError("Timeout must be positive")
        self.timeout_s = timeout_s
        self.inspect_body = inspect_body

    def classify(
        self,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        content_type: str | None = None,
        elapsed_ms: float = 0,
        median_latency_ms: int | None = None,
        error: BaseException | None = None,
    ) -> BurnResult:
        """Apply every SPEC section 6 signal, in priority order.

        ``median_latency_ms`` is the proxy's own running median, so the latency
        spike is only meaningful against measurements of the same kind (health
        probes), never against a forwarded download.
        """
        if status_code in {401, 403, 407, 429, 451, 503}:
            return BurnResult(burned=True, reason=f"HTTP {status_code}")
        # The elapsed-time rule only applies when no response arrived. The
        # gateway bounds a request by idle time rather than total duration, so a
        # slow but completed transfer must not be reported as a timeout.
        # aiohttp preserves errno but keeps Windows winerror only on os_error.
        underlying = getattr(error, "os_error", None)
        transport_error = underlying if isinstance(underlying, OSError) else error
        code = transport_error.errno if isinstance(transport_error, OSError) else None
        winerror = getattr(transport_error, "winerror", None)
        if isinstance(transport_error, TimeoutError) or code in {errno.ETIMEDOUT, 10060} or winerror == 10060 or (
            status_code is None and elapsed_ms > self.timeout_s * 1000
        ):
            return BurnResult(burned=True, reason="Timeout")
        if isinstance(transport_error, OSError) and (
            code in {errno.ECONNREFUSED, errno.ECONNRESET, 10054, 10061}
            or winerror in {10054, 10061, 1225, 1236}
            or isinstance(transport_error, ConnectionRefusedError | ConnectionResetError)
        ):
            return BurnResult(burned=True, reason="Connection failure")
        media_type = (content_type or "").partition(";")[0].strip().lower()
        if (
            self.inspect_body
            and len(body) < 128 * 1024
            and (media_type.startswith("text/") or media_type == "application/json")
        ):
            lowered = body.lower()
            for marker in (b"cf-ray", b"captcha", b"access denied", b"unusual traffic"):
                if marker in lowered:
                    return BurnResult(burned=True, reason="Body marker: " + marker.decode("ascii"))
        if median_latency_ms is not None and elapsed_ms > 3 * median_latency_ms:
            return BurnResult(burned=True, reason="Latency spike")
        return BurnResult(burned=False)
