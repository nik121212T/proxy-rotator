"""Explicit local browser launch and engine download actions."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from playwright.async_api import Error
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.browser_manager import BrowserError, BrowserManager
from app.routes.proxies import Services
from app.runtime import Runtime
from app.views import render

router = APIRouter(tags=["browsers"])


def manager(runtime: Runtime) -> BrowserManager:
    if runtime.browsers is None:
        raise HTTPException(503, "Service navigateurs indisponible.")
    return runtime.browsers


class LaunchBrowser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["installed", "dedicated"]
    browser_id: str = Field(pattern=r"^[a-z]+$", max_length=32)
    mode: Literal["rotation", "fixed"] = "rotation"
    proxy_ids: list[int] = Field(default_factory=list, max_length=8)
    url: str = Field(default="https://api.ipify.org", max_length=2048)

    @model_validator(mode="after")
    def fixed_requires_proxies(self) -> "LaunchBrowser":
        if self.mode == "fixed" and (not self.proxy_ids or any(i < 1 for i in self.proxy_ids)):
            raise ValueError("Choisissez au moins un proxy disponible.")
        return self


@router.get("/navigateurs", response_class=HTMLResponse)
async def browser_page(request: Request, runtime: Services) -> HTMLResponse:
    return render(request, "browsers.html", runtime, {"active_page": "browsers"})


@router.get("/api/browsers")
async def browser_status(runtime: Services) -> dict[str, object]:
    service = manager(runtime)
    try:
        catalog = await service.catalog()
    except (OSError, RuntimeError, Error):
        raise HTTPException(503, "Catalogue indisponible. Vérifiez l’installation de Playwright.") from None
    exits = runtime.pool.exit_observations()
    return {
        **catalog, "sessions": service.session_views(),
        "proxies": [{"id": p.id, "host": p.host, "port": p.port,
                     "exit_ip": str(exits[p.id].ip) if p.id in exits else None,
                     "country": runtime.pool.country_name(p.id),
                     "username": p.username} for p in runtime.pool.active()],
    }


@router.post("/api/browsers/install/{engine}", status_code=202)
async def install_engine(engine: Literal["chromium", "firefox", "webkit"], runtime: Services) -> dict[str, str]:
    try:
        manager(runtime).install(engine)
    except BrowserError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"status": "installing"}


@router.post("/api/browsers/sessions", status_code=201)
async def launch_browser(body: LaunchBrowser, runtime: Services) -> dict[str, object]:
    service = manager(runtime)
    ids: list[int | None] = list(dict.fromkeys(body.proxy_ids)) if body.mode == "fixed" else [None]
    available = {p.id for p in runtime.pool.active()}
    if any(identifier is not None and identifier not in available for identifier in ids):
        raise HTTPException(409, "Un proxy sélectionné n’est plus disponible. Actualisez la liste.")
    created = []
    try:
        for identifier in ids:
            created.append(await service.launch(body.source, body.browser_id, identifier, body.url))
    except BaseException as exc:
        for session_id in created:
            await service.close_session(session_id)
        if isinstance(exc, BrowserError):
            raise HTTPException(409, str(exc)) from None
        raise
    return {"opened": len(created), "sessions": created}


@router.delete("/api/browsers/sessions/{identifier}")
async def close_browser(identifier: str, runtime: Services) -> dict[str, bool]:
    await manager(runtime).close_session(identifier)
    return {"closed": True}
