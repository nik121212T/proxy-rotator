"""GitHub-release-based update checker (audit evolution, option A).

The checker only READS the GitHub Releases API: it never performs the
upgrade itself. Applying an update uses the isolated Python installer, triggered
from the UI, so the user keeps control and no silent writes to the project
tree are ever done.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import SecretStr

from app import __version__ as CURRENT_VERSION  # noqa: N812
from app.update_install import UpdateError, run

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
        timeout_s: float = 15.0,
        api_base: str = "https://api.github.com",
        token: SecretStr | None = None,
    ) -> None:
        self._token = token
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
        token = self._token
        if token is None and self._api_base == "https://api.github.com" and shutil.which("gh"):
            try:
                async with asyncio.timeout(self._timeout_s):
                    token = SecretStr(
                        await run(["gh", "auth", "token", "--hostname", "github.com"], Path.cwd())
                    )
            except (UpdateError, OSError, TimeoutError):
                token = None
        if token and token.get_secret_value():
            headers["Authorization"] = "Bearer " + token.get_secret_value()
        now = time.monotonic()
        try:
            async with (
                # trust_env=True picks up HTTP(S)_PROXY from Windows / corporate
                # networks; without it, machines behind a mandatory proxy time
                # out trying to reach api.github.com directly.
                aiohttp.ClientSession(
                    trust_env=True,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                ) as session,
                session.get(url, headers=headers, allow_redirects=False) as response,
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
                        error="Aucune release publiée ou accès au dépôt privé refusé.",
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
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
            logger.info("updater_transport_failure repo=%s type=%s", self._repo, type(exc).__name__)
            if isinstance(exc, TimeoutError):
                message = (
                    "Délai dépassé en contactant GitHub. Vérifiez votre connexion "
                    "ou votre proxy, puis réessayez."
                )
            elif isinstance(exc, aiohttp.ClientSSLError | aiohttp.ClientConnectorCertificateError):
                message = (
                    "Certificat TLS refusé par api.github.com. Un antivirus "
                    "ou un proxy fait de l'inspection SSL — ajoutez une "
                    "exception pour github.com."
                )
            elif isinstance(exc, aiohttp.ClientConnectorError | OSError):
                message = (
                    "Connexion à api.github.com impossible. Vérifiez votre "
                    "accès Internet puis réessayez."
                )
            else:
                message = "Vérification impossible : réponse GitHub invalide."
            return UpdateInfo(
                current=CURRENT_VERSION,
                latest=None,
                latest_tag=None,
                release_url=None,
                published_at=None,
                checked_at=now,
                error=message,
            )
        if not isinstance(payload, dict):
            payload = {}
        tag = str(payload.get("tag_name") or "").strip()
        if (
            not re.fullmatch(r"v?\d+\.\d+\.\d+", tag)
            or payload.get("draft")
            or payload.get("prerelease")
        ):
            return UpdateInfo(
                CURRENT_VERSION, None, None, None, None, now, "Release stable invalide."
            )
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
