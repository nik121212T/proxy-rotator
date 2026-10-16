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


def test_a_completed_response_is_never_reported_as_a_timeout() -> None:
    """The gateway bounds idle time, not total duration (see A2/A6)."""
    detector = BurnDetector()
    # A slow but complete response arrived: not a timeout.
    assert not detector.classify(status_code=200, elapsed_ms=60_000).burned
    # The elapsed rule still applies when nothing came back at all.
    assert detector.classify(elapsed_ms=60_000).reason == "Timeout"
    # A completed response can still be a latency spike.
    spike = detector.classify(status_code=200, elapsed_ms=400, median_latency_ms=100)
    assert spike.reason == "Latency spike"


@pytest.mark.parametrize("code", [10054, 10060, 10061])
def test_winsock_connection_errors_are_recognized(code):
    result = BurnDetector().classify(error=OSError(code, "synthetic-secret"))
    assert result.burned
    assert result.reason == ("Timeout" if code == 10060 else "Connection failure")
    assert "synthetic-secret" not in result.model_dump_json()


def test_windows_proactor_refusal_wrapped_by_aiohttp_is_recognized():
    from types import SimpleNamespace

    from aiohttp import ClientConnectorError

    error = OSError(errno.EINVAL, "synthetic-secret")
    error.winerror = 1225
    wrapped = ClientConnectorError(SimpleNamespace(), error)
    result = BurnDetector().classify(error=wrapped)
    assert result.reason == "Connection failure"
    assert "synthetic-secret" not in result.model_dump_json()
