"""Read release metadata and apply only the explicitly selected stable release."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routes.proxies import Services
from app.update_install import UpdateError, install

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/updates", tags=["updates"])
PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPLY_TIMEOUT_S = 300.0
_apply_lock = asyncio.Lock()


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


class ApplyRequest(BaseModel):
    tag: str = Field(pattern=r"^v?\d+\.\d+\.\d+$")


@router.post("/apply")
async def apply_update(body: ApplyRequest, runtime: Services) -> dict[str, Any]:
    if runtime.updater is None:
        raise HTTPException(404, "Mises à jour désactivées.")
    if _apply_lock.locked():
        raise HTTPException(409, "Une mise à jour est déjà en cours.")
    async with _apply_lock:
        if runtime.browsers and runtime.browsers.sessions:
            raise HTTPException(409, "Fermez les sessions navigateur avant de mettre à jour.")
        info = await runtime.updater.latest(force=True)
        if info.error or not info.available or info.latest_tag != body.tag:
            raise HTTPException(
                409, "La release choisie n’est plus disponible. Vérifiez les versions."
            )
        try:
            async with asyncio.timeout(APPLY_TIMEOUT_S):
                message = await install(PROJECT_ROOT, runtime.updater.repo, body.tag)
        except TimeoutError:
            raise HTTPException(
                504,
                "Délai dépassé. Processus arrêtés ; relancez une vérification avant de réessayer.",
            ) from None
        except (UpdateError, OSError) as exc:
            message = (
                str(exc)
                if isinstance(exc, UpdateError)
                else "Installation impossible. Vérifiez Git et les droits du dossier."
            )
            raise HTTPException(409, message) from None
        return {"ok": True, "returncode": 0, "output": "", "message": message}
