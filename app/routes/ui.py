"""Dashboard page and server-rendered live fragments."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.routes.proxies import Services
from app.views import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, runtime: Services) -> HTMLResponse:
    return render(request, "dashboard.html", runtime, {"active_page": "dashboard"})


@router.get("/parametres", response_class=HTMLResponse)
async def settings(request: Request, runtime: Services) -> HTMLResponse:
    return render(request, "settings.html", runtime, {"active_page": "settings"})


@router.get("/ui/snapshot", response_class=HTMLResponse)
async def snapshot(request: Request, runtime: Services) -> HTMLResponse:
    response = render(request, "_snapshot.html", runtime)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/ui/proxies/{proxy_id}", response_class=HTMLResponse)
async def row(
    proxy_id: int, request: Request, runtime: Services, append: bool = False, empty: bool = False
) -> HTMLResponse:
    proxy = next((p for p in runtime.pool.all() if p.id == proxy_id), None)
    response = render(
        request,
        "_row_oob.html",
        runtime,
        {"proxy": proxy, "proxy_id": proxy_id, "append": append, "empty": empty},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/ui/check-status", response_class=HTMLResponse)
async def check_status(request: Request, runtime: Services) -> HTMLResponse:
    response = render(request, "_check_controls.html", runtime)
    response.headers["Cache-Control"] = "no-store"
    return response
