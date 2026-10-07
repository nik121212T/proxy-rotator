"""Shared template rendering for page, fragments and HTMX responses."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.runtime import Runtime

UI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=UI_DIR / "templates")


def render(
    request: Request, template: str, runtime: Runtime, extra: dict[str, object] | None = None
) -> HTMLResponse:
    context: dict[str, object] = {
        "proxies": tuple(p for p in runtime.pool.all() if p.status != "dead"),
        "stats": runtime.pool.stats(runtime.rotator.strategy),
        "gateway": runtime.gateway,
    }
    context.update(extra or {})
    return templates.TemplateResponse(request=request, name=template, context=context)


def feedback(
    request: Request,
    message: str,
    *,
    errors: list[dict[str, object]] | None = None,
    clear_import: bool = False,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request, name="_feedback.html", context={"message": message, "errors": errors or []}
    )
    response.headers["HX-Trigger"] = "pool-refresh"
    if clear_import:
        response.headers["X-Clear-Import"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return response
