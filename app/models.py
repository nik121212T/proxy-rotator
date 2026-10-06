"""Pydantic data contracts only; no storage, network or clock access."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    SecretBytes,
    field_validator,
)

from app.parser import Scheme


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
Count = Annotated[int, Field(ge=0, strict=True)]
Identifier = Annotated[int, Field(gt=0, strict=True)]
Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
ProxyStatus = Literal["unknown", "alive", "quarantine", "dead"]
RotationStrategy = Literal["round_robin", "random", "weighted", "sticky"]


class _DTO(BaseModel):
    """Immutable snapshots with safe validation messages."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True, extra="forbid")


class Proxy(_DTO):
    """Persisted proxy snapshot with credentials excluded from public output."""

    id: Identifier
    scheme: Scheme = "http"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535, strict=True)
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password_enc: SecretBytes = Field(exclude=True, repr=False)
    label: str | None = None
    status: ProxyStatus = "unknown"
    score: Score = 0.0
    latency_ms: Count | None = None
    success_count: Count = 0
    fail_count: Count = 0
    consec_fails: Count = 0
    last_checked_at: UtcDatetime | None = None
    quarantine_until: UtcDatetime | None = None
    created_at: UtcDatetime

    @field_validator("host")
    @classmethod
    def lowercase_host(cls, value: str) -> str:
        return value.lower()


class CheckResult(_DTO):
    """Outcome of one health probe; error text must be sanitized by its producer."""

    proxy_id: Identifier
    ok: bool = Field(strict=True)
    status_code: Annotated[int, Field(ge=100, le=599, strict=True)] | None = None
    latency_ms: Count | None = None
    exit_ip: IPvAnyAddress | None = None
    error: str | None = None
    checked_at: UtcDatetime


class PoolStats(_DTO):
    """Dashboard snapshot; unknown proxies are included in total."""

    total: Count = 0
    alive: Count = 0
    quarantine: Count = 0
    dead: Count = 0
    avg_score: Score = 0.0
    avg_latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    strategy: RotationStrategy = "weighted"


class ProxyFilters(_DTO):
    """Repository filters; no implicit status exclusion or pagination cap."""

    status: ProxyStatus | None = None
    min_score: Score | None = None
    limit: Annotated[int, Field(ge=1, strict=True)] | None = None
    offset: Count = 0
