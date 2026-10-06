"""Public data contracts for persisted proxies and health snapshots."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models import CheckResult, PoolStats, Proxy


def proxy_record() -> dict[str, object]:
    return {
        "id": 42,
        "scheme": "http",
        "host": "PROXY.Example",
        "port": 8080,
        "username": "user",
        "password_enc": b"synthetic-fernet-token",
        "created_at": "2026-10-06T14:12:03Z",
    }


def test_proxy_defaults_and_public_serialization_exclude_credentials() -> None:
    proxy = Proxy.model_validate(proxy_record())

    assert proxy.host == "proxy.example"
    assert proxy.status == "unknown"
    assert proxy.score == 0
    assert proxy.latency_ms is None
    assert proxy.success_count == proxy.fail_count == proxy.consec_fails == 0
    assert proxy.created_at == datetime(2026, 10, 6, 14, 12, 3, tzinfo=UTC)
    assert proxy.password_enc.get_secret_value() == b"synthetic-fernet-token"
    assert "password_enc" not in proxy.model_dump()
    assert "synthetic-fernet-token" not in proxy.model_dump_json()
    assert "synthetic-fernet-token" not in repr(proxy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 0),
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("port", 80.5),
        ("scheme", "ftp"),
        ("status", "healthy"),
        ("host", ""),
        ("username", "ab"),
        ("username", "u" * 65),
        ("username", "user:name"),
        ("score", -1),
        ("score", 101),
        ("score", float("nan")),
        ("latency_ms", -1),
        ("success_count", -1),
        ("fail_count", -1),
        ("consec_fails", -1),
        ("created_at", "2026-10-06T14:12:03"),
        ("last_checked_at", "2026-10-06T14:12:03"),
        ("quarantine_until", "2026-10-06T14:12:03"),
        ("password", "synthetic-plaintext-password"),
    ],
)
def test_proxy_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        Proxy.model_validate(proxy_record() | {field: value})

    assert "synthetic-plaintext-password" not in str(error.value)


def test_proxy_normalizes_dates_to_utc() -> None:
    proxy = Proxy.model_validate(
        proxy_record()
        | {
            "created_at": "2026-10-06T16:12:03+02:00",
            "last_checked_at": "2026-10-06T16:12:03+02:00",
            "quarantine_until": "2026-10-06T16:12:03+02:00",
        }
    )

    expected = "2026-10-06T14:12:03Z"
    for field in ("created_at", "last_checked_at", "quarantine_until"):
        assert proxy.model_dump(mode="json")[field] == expected


def check_record() -> dict[str, object]:
    return {
        "proxy_id": 42,
        "ok": True,
        "status_code": 200,
        "latency_ms": 412,
        "exit_ip": "203.0.113.10",
        "error": None,
        "checked_at": "2026-10-06T14:12:03Z",
    }


def test_check_result_matches_spec_json_and_supports_failure() -> None:
    check = CheckResult.model_validate(check_record())

    assert check.model_dump(mode="json") == check_record()
    failed = CheckResult.model_validate(
        {"proxy_id": 42, "ok": False, "error": "Timeout", "checked_at": check.checked_at}
    )
    assert failed.status_code is None
    assert failed.latency_ms is None
    assert failed.exit_ip is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proxy_id", 0),
        ("ok", "false"),
        ("status_code", 99),
        ("status_code", 600),
        ("latency_ms", -1),
        ("exit_ip", "not-an-ip"),
        ("checked_at", "2026-10-06T14:12:03"),
    ],
)
def test_check_result_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        CheckResult.model_validate(check_record() | {field: value})


def pool_record() -> dict[str, object]:
    return {
        "total": 50,
        "alive": 34,
        "quarantine": 4,
        "dead": 12,
        "avg_score": 61.2,
        "avg_latency_ms": 480,
        "strategy": "weighted",
    }


def test_pool_stats_matches_api_contract_and_empty_pool() -> None:
    stats = PoolStats.model_validate(pool_record())

    assert stats.model_dump(mode="json") == pool_record()
    empty = PoolStats()
    assert empty.total == empty.alive == empty.quarantine == empty.dead == 0
    assert empty.avg_latency_ms is None
    assert empty.avg_score == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", -1),
        ("alive", -1),
        ("quarantine", -1),
        ("dead", -1),
        ("avg_score", -1),
        ("avg_score", 101),
        ("avg_score", float("inf")),
        ("avg_latency_ms", -1),
        ("avg_latency_ms", float("nan")),
        ("strategy", "unsupported"),
    ],
)
def test_pool_stats_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        PoolStats.model_validate(pool_record() | {field: value})


@pytest.mark.parametrize("strategy", ["round_robin", "random", "weighted", "sticky"])
def test_pool_stats_accepts_all_rotation_strategies(strategy: str) -> None:
    assert PoolStats.model_validate(pool_record() | {"strategy": strategy}).strategy == strategy
