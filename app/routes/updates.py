"""Routes for the git-based self-update flow (option A, bêta amis).

- ``GET /api/updates/status`` returns the cached latest-release info.
- ``POST /api/updates/check`` forces a fresh GitHub call.
- ``POST /api/updates/apply`` runs ``scripts/update.sh`` and streams back
  its combined output. The script does the real work (git fetch, ff-only
  pull, pip install) with safety rails of its own.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.routes.proxies import Services

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/updates", tags=["updates"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT = PROJECT_ROOT / "scripts" / "update.sh"
APPLY_TIMEOUT_S = 300.0  # Five minutes: git + pip install must stay inside.
# Serialize apply calls so two concurrent POSTs cannot run two parallel
# git/pip chains against the same working tree.
_apply_lock = asyncio.Lock()
# Never let secrets loaded from .env (MASTER_SECRET, DICHVUSOCKS_API_TOKEN, …)
# leak into the pip-install step, which executes third-party setup/entry-point
# code. Pass a minimal whitelist to the subprocess environment.
_SAFE_ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")


def _info_payload(info: object | None) -> dict[str, Any]:
    if info is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "current": info.current,  # type: ignore[attr-defined]
        "latest": info.latest,  # type: ignore[attr-defined]
        "latest_tag": info.latest_tag,  # type: ignore[attr-defined]
        "release_url": info.release_url,  # type: ignore[attr-defined]
        "published_at": info.published_at,  # type: ignore[attr-defined]
        "available": info.available,  # type: ignore[attr-defined]
        "error": info.error,  # type: ignore[attr-defined]
    }


@router.get("/status")
async def status(runtime: Services) -> dict[str, Any]:
    if runtime.updater is None:
        return {"enabled": False, "current": None, "available": False}
    cached = runtime.updater.cached()
    if cached is None:
        cached = await runtime.updater.latest()
    return _info_payload(cached) | {"repo": runtime.updater.repo}


@router.post("/check")
async def force_check(runtime: Services) -> dict[str, Any]:
    if runtime.updater is None:
        raise HTTPException(status_code=404, detail="Updates disabled")
    info = await runtime.updater.latest(force=True)
    return _info_payload(info) | {"repo": runtime.updater.repo}


@router.post("/apply")
async def apply_update(runtime: Services) -> dict[str, Any]:
    """Run scripts/update.sh and return its combined output + exit code."""
    import os  # noqa: PLC0415 - local to keep the import graph narrow

    if runtime.updater is None:
        raise HTTPException(status_code=404, detail="Updates disabled")
    if not UPDATE_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="Update script missing")
    if shutil.which("git") is None:
        raise HTTPException(status_code=503, detail="git is required to apply updates")
    if _apply_lock.locked():
        # An update is already running — refuse rather than race.
        raise HTTPException(status_code=409, detail="An update is already in progress")

    async with _apply_lock:
        child_env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
        child_env["PROXY_ROTATOR_UPDATE_REPO"] = runtime.updater.repo

        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                str(UPDATE_SCRIPT),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
            )
        except OSError as exc:
            logger.info("updater_apply_spawn_failed err=%s", exc)
            raise HTTPException(status_code=500, detail="Could not start update script") from exc

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                process.communicate(), timeout=APPLY_TIMEOUT_S
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(status_code=504, detail="Update script timed out") from None

        text = (stdout_bytes or b"").decode("utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-200:])
        return {
            "returncode": process.returncode,
            "ok": process.returncode == 0,
            "output": tail,
            "message": (
                "Mise à jour appliquée. Relancez l'application pour utiliser la nouvelle version."
                if process.returncode == 0
                else "Mise à jour échouée. Consultez la sortie et corrigez l'état du dépôt."
            ),
        }
