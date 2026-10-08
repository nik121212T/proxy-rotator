"""Shared template rendering for page, fragments and HTMX responses."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import Proxy
from app.runtime import Runtime

UI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=UI_DIR / "templates")


SORT_OPTIONS = {
    "best": "Meilleurs résultats d’abord",
    "issues": "Problèmes d’abord",
    "latency": "Latence : plus rapides d’abord",
    "score": "Score : du meilleur au moins bon",
    "success": "Taux de réussite décroissant",
    "recent": "Derniers tests d’abord",
    "added": "Ordre d’import",
}


def ordered_proxies(proxies: tuple[Proxy, ...], order: str) -> tuple[Proxy, ...]:
    def key(proxy: Proxy) -> tuple[float, ...]:
        latency = proxy.latency_ms if proxy.latency_ms is not None else float("inf")
        if order == "latency":
            return (latency, proxy.id)
        if order == "score":
            return (-proxy.score, proxy.id)
        if order == "success":
            count = proxy.success_count + proxy.fail_count
            return (not count, -proxy.success_count / max(1, count), -proxy.score, proxy.id)
        if order == "recent":
            checked = proxy.last_checked_at.timestamp() if proxy.last_checked_at else 0
            return (-checked, proxy.id)
        if order == "added":
            return (proxy.id,)
        ranks = (
            {"dead": 0, "quarantine": 1, "unknown": 2, "alive": 3}
            if order == "issues"
            else {"alive": 0, "unknown": 1, "quarantine": 2, "dead": 3}
        )
        return (ranks[proxy.status], -proxy.score, latency, proxy.id)

    return tuple(sorted(proxies, key=key))


def render(
    request: Request, template: str, runtime: Runtime, extra: dict[str, object] | None = None
) -> HTMLResponse:
    order = request.query_params.get("sort", "best")
    if order not in SORT_OPTIONS:
        order = "best"
    context: dict[str, object] = {
        "sort_order": order,
        "sort_options": SORT_OPTIONS,
        "proxies": ordered_proxies(
            tuple(p for p in runtime.pool.all() if p.status != "dead"), order
        ),
        "stats": runtime.pool.stats(runtime.rotator.strategy),
        "gateway": runtime.gateway,
        "checks": runtime.checker.batch_status,
        "pinned_ids": runtime.pool.pinned_ids(),
        "activity": runtime.activity(),
        "exits": runtime.pool.exit_observations(),
        "country_names": {p.id: runtime.pool.country_name(p.id) for p in runtime.pool.all()},
        "countries": sorted(
            {
                (runtime.pool.country_code(p.id), runtime.pool.country_name(p.id))
                for p in runtime.pool.all()
                if runtime.pool.country_code(p.id)
            }
        ),
        "selection": runtime.rotator.selection,
        "interval_minutes": runtime.rotator.interval_minutes,
        "geo_enabled": bool(runtime.geo and runtime.geo.enabled),
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
