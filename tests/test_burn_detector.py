"""Every SPEC section 6 signal and its inspection boundaries."""

import errno

import pytest

from app.burn_detector import BurnDetector


@pytest.mark.parametrize("status", [401, 403, 407, 429, 451, 503])
def test_burn_status_codes(status: int) -> None:
    result = BurnDetector().classify(status_code=status)
    assert result.burned
    assert result.reason == f"HTTP {status}"


@pytest.mark.parametrize("status", [200, 301, 404, 500, 502])
def test_other_status_codes_are_not_burn_signals(status: int) -> None:
    assert not BurnDetector().classify(status_code=status).burned


@pytest.mark.parametrize("marker", [b"CF-RAY", b"CAPTCHA", b"Access Denied", b"Unusual Traffic"])
def test_body_signals_are_case_insensitive(marker: bytes) -> None:
    assert BurnDetector().classify(body=marker, content_type="text/html; charset=utf-8").burned
    assert BurnDetector().classify(body=marker, content_type="application/json").burned


def test_body_inspection_gates_and_exact_size_limit() -> None:
    assert (
        not BurnDetector(inspect_body=False)
        .classify(body=b"captcha", content_type="text/html")
        .burned
    )
    assert not BurnDetector().classify(body=b"captcha", content_type="image/png").burned
    assert not BurnDetector().classify(body=b"captcha", content_type=None).burned
    boundary = b"captcha" + b"x" * (128 * 1024 - 7)
    assert not BurnDetector().classify(body=boundary, content_type="text/html").burned
    assert BurnDetector().classify(body=boundary[:-1], content_type="text/html").burned


def test_timeout_and_latency_spike_are_strict_boundaries() -> None:
    detector = BurnDetector()
    assert not detector.classify(elapsed_ms=15000).burned
    assert detector.classify(elapsed_ms=15001).reason == "Timeout"
    assert detector.classify(error=TimeoutError()).reason == "Timeout"
    assert not detector.classify(elapsed_ms=300, median_latency_ms=100).burned
    assert detector.classify(elapsed_ms=301, median_latency_ms=100).reason == "Latency spike"
    assert not detector.classify(elapsed_ms=100, median_latency_ms=None).burned


@pytest.mark.parametrize("code", [errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT])
def test_connection_errors_never_expose_exception_text(code: int) -> None:
    result = BurnDetector().classify(error=OSError(code, "user:synthetic-secret@proxy.example"))
    assert result.burned
    assert "synthetic-secret" not in result.model_dump_json()
    assert not BurnDetector().classify(error=OSError(errno.ENOENT, "unrelated")).burned
