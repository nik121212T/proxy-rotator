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
        if status_code in {401, 403, 407, 429, 451, 503}:
            return BurnResult(burned=True, reason=f"HTTP {status_code}")
        if isinstance(error, TimeoutError) or elapsed_ms > self.timeout_s * 1000:
            return BurnResult(burned=True, reason="Timeout")
        if isinstance(error, OSError) and error.errno in {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ETIMEDOUT,
        }:
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
