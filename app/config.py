"""Validated application configuration, loaded at startup rather than import."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

from app.models import RotationStrategy
from app.security import is_loopback


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True, allow_inf_nan=False
    )

    master_secret: SecretStr
    db_path: Path = Path("data") / "proxies.db"
    health_check_interval_s: float = Field(default=300, gt=0)
    health_timeout_s: float = Field(default=8, gt=0)
    health_concurrency: int = Field(default=20, ge=1)
    health_probe_url: str = "https://api.ipify.org?format=json"
    pool_min_score: float = Field(default=40, ge=0, le=100)
    # ── Quarantine policy (SPEC §4) ────────────────────────────────────────
    # Minutes a proxy stays in quarantine after three consecutive failures.
    quarantine_minutes: int = Field(default=10, ge=1, le=1440)

    bind_host: str = "127.0.0.1"
    admin_port: int = Field(default=8080, ge=1, le=65535)
    # ── Exposure guards (see docs/SECURITY.md) ────────────────────────────
    # Binding beyond loopback publishes an unauthenticated admin UI and two
    # unauthenticated proxy gateways, so it takes three explicit settings.
    allow_remote_bind: bool = False
    admin_token: SecretStr | None = None
    admin_allowed_hosts: list[str] = Field(default_factory=list)
    # ``0`` means "let the OS pick an ephemeral port" (useful in tests + CI).
    gateway_http_port: int = Field(default=8888, ge=0, le=65535)
    gateway_socks_port: int = Field(default=1080, ge=0, le=65535)
    gateway_timeout_s: float = Field(default=15, gt=0)
    max_retry_per_request: int = Field(default=2, ge=0, le=10)
    # Admission control: each connection holds a task, two sockets and up to
    # 1 MiB of buffered body, so the gateways refuse rather than accumulate.
    gateway_max_connections: int = Field(default=256, ge=1)
    burn_inspect_body: bool = True
    rotation_strategy: RotationStrategy = "weighted"
    sticky_ttl_s: float = Field(default=900, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── Geolocation (opt-in; sends ONLY exit IPs to a third-party service) ─
    geolocation_enabled: bool = False
    geolocation_provider_url: str = "http://ip-api.com/json/{ip}?fields=status,country,countryCode"
    geolocation_cache_ttl_s: int = Field(default=30 * 24 * 3600, ge=60)
    geolocation_timeout_s: float = Field(default=4.0, gt=0)

    # ── Dichvusocks provider (opt-in SOCKS5 import source) ─────────────────
    dichvusocks_api_token: SecretStr | None = None
    dichvusocks_base_url: str = "https://dichvusocks.net/api/v1"
    dichvusocks_import_max: int = Field(default=50, ge=1, le=500)
    dichvusocks_timeout_s: float = Field(default=10.0, gt=0)

    @field_validator("master_secret")
    @classmethod
    def configured_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip() or value.get_secret_value() == "CHANGE_ME_NOW":
            raise ValueError("MASTER_SECRET must be configured")
        return value

    @field_validator("admin_allowed_hosts", mode="before")
    @classmethod
    def split_allowed_hosts(cls, value: object) -> object:
        """Accept a comma-separated ``ADMIN_ALLOWED_HOSTS`` value."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def guarded_remote_bind(self) -> "Settings":
        """Refuse to publish the service beyond loopback without explicit consent.

        A non-loopback bind exposes both the unauthenticated admin UI and the
        two proxy gateways to the network, so it requires three deliberate
        settings rather than one flag.
        """
        if is_loopback(self.bind_host):
            return self
        if not self.allow_remote_bind:
            raise ValueError(
                "BIND_HOST is not a loopback address; set ALLOW_REMOTE_BIND=true to "
                "publish the admin UI and the proxy gateways on the network"
            )
        if self.admin_token is None or not self.admin_token.get_secret_value().strip():
            raise ValueError("ADMIN_TOKEN is required when BIND_HOST is not a loopback address")
        if not self.admin_allowed_hosts:
            raise ValueError(
                "ADMIN_ALLOWED_HOSTS must list the host names or addresses you will use "
                "(for example 192.168.1.50,proxy.lan) when BIND_HOST is not loopback"
            )
        return self

    @field_validator("health_probe_url")
    @classmethod
    def valid_probe_url(cls, value: str) -> str:
        try:
            url = URL(value)
            valid = url.scheme in {"http", "https"} and url.host and url.user is None
            port = url.port
        except ValueError:
            raise ValueError("Invalid health probe URL") from None
        if not valid or port is None or not 1 <= port <= 65535:
            raise ValueError("Probe URL must be HTTP(S) without credentials")
        return value
