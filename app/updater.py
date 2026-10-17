"""GitHub-release-based update checker (audit evolution, option A).

The checker only READS the GitHub Releases API: it never performs the
upgrade itself. Applying an update is a separate shell script, triggered
from the UI, so the user keeps control and no silent writes to the project
tree are ever done.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from app import __version__ as CURRENT_VERSION  # noqa: N812

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_version(raw: str) -> tuple[int, int, int] | None:
    """Return a (major, minor, patch) tuple or ``None`` when unparseable."""
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str | None
    latest_tag: str | None
    release_url: str | None
    published_at: str | None
    checked_at: float
    error: str | None = None

    @property
    def available(self) -> bool:
        if self.latest is None:
            return False
        local = parse_version(self.current)
        remote = parse_version(self.latest)
        if local is None or remote is None:
            return False
        return remote > local


class UpdateChecker:
    """Polls GitHub Releases on demand, caches the latest answer."""

    def __init__(
        self,
        repo: str,
        *,
        cache_ttl_s: float = 24 * 3600,
        timeout_s: float = 5.0,
        api_base: str = "https://api.github.com",
    ) -> None:
        self._repo = repo
        self._cache_ttl_s = cache_ttl_s
        self._timeout_s = timeout_s
        self._api_base = api_base.rstrip("/")
        self._cache: UpdateInfo | None = None
        self._lock = asyncio.Lock()

    @property
    def repo(self) -> str:
        return self._repo

    def current(self) -> str:
        return CURRENT_VERSION

    def cached(self) -> UpdateInfo | None:
        if self._cache is None:
            return None
        if time.monotonic() - self._cache.checked_at > self._cache_ttl_s:
            return None
        return self._cache

    async def latest(self, *, force: bool = False) -> UpdateInfo:
        """Return the latest-release info, cached for ``cache_ttl_s`` seconds."""
        if not force:
            cached = self.cached()
            if cached is not None:
                return cached
        async with self._lock:
            if not force:
                cached = self.cached()
                if cached is not None:
                    return cached
            info = await self._fetch()
            self._cache = info
            return info

    async def _fetch(self) -> UpdateInfo:
        url = f"{self._api_base}/repos/{self._repo}/releases/latest"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"proxy-rotator/{CURRENT_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        now = time.monotonic()
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                ) as session,
                session.get(url, headers=headers) as response,
            ):
                if response.status == 404:
                    logger.info("updater_no_release_yet repo=%s", self._repo)
                    return UpdateInfo(
                        current=CURRENT_VERSION,
                        latest=None,
                        latest_tag=None,
                        release_url=None,
                        published_at=None,
                        checked_at=now,
                        error="no release",
                    )
                if response.status != 200:
                    logger.info(
                        "updater_github_http_error repo=%s status=%s",
                        self._repo,
                        response.status,
                    )
                    return UpdateInfo(
                        current=CURRENT_VERSION,
                        latest=None,
                        latest_tag=None,
                        release_url=None,
                        published_at=None,
                        checked_at=now,
                        error=f"HTTP {response.status}",
                    )
                payload: dict[str, Any] = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.info("updater_transport_failure repo=%s err=%s", self._repo, exc)
            return UpdateInfo(
                current=CURRENT_VERSION,
                latest=None,
                latest_tag=None,
                release_url=None,
                published_at=None,
                checked_at=now,
                error=exc.__class__.__name__,
            )
        tag = str(payload.get("tag_name") or "").strip()
        version = tag.lstrip("v") if tag else None
        release_url = payload.get("html_url")
        published = payload.get("published_at")
        return UpdateInfo(
            current=CURRENT_VERSION,
            latest=version,
            latest_tag=tag or None,
            release_url=str(release_url) if isinstance(release_url, str) else None,
            published_at=str(published) if isinstance(published, str) else None,
            checked_at=now,
        )
