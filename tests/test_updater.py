"""UpdateChecker + /api/updates/* routes, driven by a local aiohttp fake."""

from __future__ import annotations

import stat as _stat
from collections.abc import AsyncIterator
from pathlib import Path as _Path

import pytest
from aiohttp import web

from app import __version__ as CURRENT_VERSION  # noqa: N812
from app.routes import updates as _updates
from app.updater import UpdateChecker, UpdateInfo, parse_version


class FakeGitHub:
    def __init__(self) -> None:
        self._status = 200
        self._payload: dict[str, object] | None = {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/nik121212T/proxy-rotator/releases/tag/v9.9.9",
            "published_at": "2026-10-17T12:00:00Z",
        }
        self.hits = 0
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{path:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def set_status(self, status: int) -> None:
        self._status = status
        self._payload = None

    def set_payload(self, payload: dict[str, object]) -> None:
        self._status = 200
        self._payload = payload

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.hits += 1
        if self._status != 200:
            return web.Response(status=self._status)
        return web.json_response(self._payload)


@pytest.fixture
async def github() -> AsyncIterator[FakeGitHub]:
    server = FakeGitHub()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _checker_against(github: FakeGitHub, **kwargs: object) -> UpdateChecker:
    kwargs.setdefault("cache_ttl_s", 60)
    kwargs.setdefault("timeout_s", 2.0)
    return UpdateChecker(
        "nik121212T/proxy-rotator",
        api_base=f"http://127.0.0.1:{github.port}",
        **kwargs,  # type: ignore[arg-type]
    )


# ── parse_version ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("v1.2.3-beta", (1, 2, 3)),
        ("v0.1.0", (0, 1, 0)),
        ("", None),
        ("1.2", None),
        ("latest", None),
    ],
)
def test_parse_version_accepts_semver_and_rejects_garbage(raw, expected) -> None:
    assert parse_version(raw) == expected


# ── UpdateInfo.available ───────────────────────────────────────────────────


def test_update_info_available_requires_remote_greater_than_local() -> None:
    assert UpdateInfo("1.0.0", "1.1.0", "v1.1.0", None, None, 0).available
    assert not UpdateInfo("1.1.0", "1.0.0", "v1.0.0", None, None, 0).available
    assert not UpdateInfo("1.0.0", "1.0.0", "v1.0.0", None, None, 0).available
    assert not UpdateInfo("1.0.0", None, None, None, None, 0, "offline").available


# ── UpdateChecker end-to-end against the fake server ───────────────────────


async def test_latest_hits_github_and_reports_a_newer_release(github: FakeGitHub) -> None:
    checker = _checker_against(github)
    info = await checker.latest()
    assert info.latest == "9.9.9"
    assert info.latest_tag == "v9.9.9"
    assert info.release_url is not None and "nik121212T" in info.release_url
    local = parse_version(CURRENT_VERSION) or (0, 0, 0)
    assert info.available == (local < (9, 9, 9))
    assert github.hits == 1


async def test_latest_is_cached_within_the_ttl(github: FakeGitHub) -> None:
    checker = _checker_against(github, cache_ttl_s=60)
    await checker.latest()
    await checker.latest()
    await checker.latest()
    assert github.hits == 1


async def test_force_bypasses_cache(github: FakeGitHub) -> None:
    checker = _checker_against(github, cache_ttl_s=60)
    await checker.latest()
    await checker.latest(force=True)
    assert github.hits == 2


async def test_latest_returns_an_error_when_github_rate_limits(github: FakeGitHub) -> None:
    checker = _checker_against(github)
    github.set_status(403)
    info = await checker.latest()
    assert info.latest is None
    assert info.error == "HTTP 403"
    assert not info.available


async def test_latest_handles_no_release_yet(github: FakeGitHub) -> None:
    checker = _checker_against(github)
    github.set_status(404)
    info = await checker.latest()
    assert info.error == "no release"
    assert info.latest is None


# ── routes ─────────────────────────────────────────────────────────────────


async def test_status_route_returns_disabled_when_updater_absent(api, running_app) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.updater = None
    response = await client.get("/api/updates/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "current": None, "available": False}


async def test_check_route_requires_updater(api, running_app) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.updater = None
    response = await client.post("/api/updates/check")
    assert response.status_code == 404


async def test_status_route_reports_live_data(api, running_app, github: FakeGitHub) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.updater = _checker_against(github)
    response = await client.get("/api/updates/status")
    body = response.json()
    assert response.status_code == 200
    assert body["enabled"] is True
    assert body["current"] == CURRENT_VERSION
    assert body["latest"] == "9.9.9"
    assert body["repo"] == "nik121212T/proxy-rotator"


async def test_apply_route_rejects_when_updater_absent(api, running_app) -> None:
    client, _ = api
    app, _ = running_app
    app.state.runtime.updater = None
    response = await client.post("/api/updates/apply")
    assert response.status_code == 404


# ── /apply: happy, failure, timeout, missing, concurrency ─────────────────


def _install_fake_script(monkeypatch, tmp_path: _Path, body: str) -> _Path:
    script = tmp_path / "fake_update.sh"
    script.write_text("#!/usr/bin/env bash\n" + body + "\n")
    script.chmod(script.stat().st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setattr(_updates, "UPDATE_SCRIPT", script)
    return script


def _attach_updater(app) -> None:
    app.state.runtime.updater = UpdateChecker(
        "nik121212T/proxy-rotator", cache_ttl_s=60, timeout_s=2.0
    )


async def test_apply_reports_success_when_script_exits_zero(
    api, running_app, tmp_path, monkeypatch
) -> None:
    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    _install_fake_script(monkeypatch, tmp_path, 'echo "ok"\nexit 0\n')
    response = await client.post("/api/updates/apply")
    assert response.status_code == 200
    body = response.json()
    assert body["returncode"] == 0
    assert body["ok"] is True
    assert "ok" in body["output"]


async def test_apply_reports_failure_when_script_exits_nonzero(
    api, running_app, tmp_path, monkeypatch
) -> None:
    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    _install_fake_script(monkeypatch, tmp_path, 'echo "oops" >&2\nexit 7\n')
    response = await client.post("/api/updates/apply")
    assert response.status_code == 200
    body = response.json()
    assert body["returncode"] == 7
    assert body["ok"] is False
    assert "oops" in body["output"]


async def test_apply_returns_500_when_script_missing(
    api, running_app, tmp_path, monkeypatch
) -> None:
    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    monkeypatch.setattr(_updates, "UPDATE_SCRIPT", tmp_path / "does-not-exist.sh")
    response = await client.post("/api/updates/apply")
    assert response.status_code == 500


async def test_apply_times_out_long_running_script(
    api, running_app, tmp_path, monkeypatch
) -> None:
    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    _install_fake_script(monkeypatch, tmp_path, "sleep 30\n")
    monkeypatch.setattr(_updates, "APPLY_TIMEOUT_S", 0.5)
    response = await client.post("/api/updates/apply")
    assert response.status_code == 504


async def test_apply_rejects_concurrent_calls_with_409(
    api, running_app, tmp_path, monkeypatch
) -> None:
    import asyncio as _asyncio

    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    _install_fake_script(monkeypatch, tmp_path, "sleep 0.5\nexit 0\n")
    first = _asyncio.create_task(client.post("/api/updates/apply"))
    await _asyncio.sleep(0.1)  # let the lock be acquired
    second = await client.post("/api/updates/apply")
    assert second.status_code == 409
    done = await first
    assert done.status_code == 200
    assert done.json()["ok"] is True


async def test_apply_passes_whitelisted_env_only_to_subprocess(
    api, running_app, tmp_path, monkeypatch
) -> None:
    """Secrets like MASTER_SECRET must not reach the pip install step."""
    import os as _os

    client, _ = api
    app, _ = running_app
    _attach_updater(app)
    monkeypatch.setenv("MASTER_SECRET", "super-secret-should-not-leak")
    monkeypatch.setenv("DICHVUSOCKS_API_TOKEN", "token-should-not-leak")
    _install_fake_script(monkeypatch, tmp_path, 'env | sort\nexit 0\n')
    response = await client.post("/api/updates/apply")
    body = response.json()
    assert response.status_code == 200
    assert "MASTER_SECRET" not in body["output"]
    assert "DICHVUSOCKS_API_TOKEN" not in body["output"]
    # The expected repo must be forwarded so the shell check can validate it.
    assert "PROXY_ROTATOR_UPDATE_REPO=nik121212T/proxy-rotator" in body["output"]
    # Sanity: PATH is passed (needed for `env` itself to resolve) but no secret.
    _os.environ.pop("MASTER_SECRET", None)
    _os.environ.pop("DICHVUSOCKS_API_TOKEN", None)
