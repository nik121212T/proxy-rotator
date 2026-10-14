"""Shared template rendering for page, fragments and HTMX responses.

Moved out of ``app/ui/`` so routes no longer have to import the UI package
(audit G1). Templates still live in ``app/ui/templates/``; this module only
owns the Python glue that routes call into.
"""

import unicodedata
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.models import CheckResult, Proxy
from app.runtime import Runtime

UI_DIR = Path(__file__).resolve().parent / "ui"
templates = Jinja2Templates(directory=UI_DIR / "templates")


SORT_OPTIONS = {
    "best": "Meilleurs résultats d’abord",
    "issues": "Problèmes d’abord",
    "latency": "Latence : plus rapides d’abord",
    "city": "Ville : A à Z",
    "score": "Score : du meilleur au moins bon",
    "success": "Taux de réussite décroissant",
    "recent": "Derniers tests d’abord",
    "added": "Ordre d’import",
}


STATUS_FILTERS = {
    "": "Tous sauf hors pool",
    "all": "Tous les états",
    "alive": "Disponibles",
    "unknown": "À vérifier",
    "quarantine": "En quarantaine",
    "dead": "Hors pool",
}


def normalized_text(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.strip().casefold())
        if not unicodedata.combining(char)
    )


def ordered_proxies(
    proxies: tuple[Proxy, ...],
    order: str,
    city_names: dict[int, str | None] | None = None,
) -> tuple[Proxy, ...]:
    if order == "city":

        def city_key(proxy: Proxy) -> tuple[bool, str, int]:
            city = ((city_names or {}).get(proxy.id) or "").strip()
            normalized = normalized_text(city)
            return (not bool(city), normalized, proxy.id)

        return tuple(sorted(proxies, key=city_key))

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


def check_failure(check: CheckResult | None) -> str | None:
    """Translate known failures without reflecting exception text or credentials."""
    if check is None or check.ok:
        return None
    if check.status_code == 407:
        return "Identifiants refusés (HTTP 407) — vérifiez les accès chez le fournisseur."
    messages = {
        "Timeout": "Délai dépassé — réessayez ou choisissez un autre proxy.",
        "Proxy connection failed": "Connexion au proxy impossible — vérifiez l’adresse et le port.",
        "TLS certificate verification failed": "Certificat TLS non valide — vérifiez le fournisseur.",
        "Invalid probe response": "Réponse de test invalide — aucune IP de sortie vérifiée.",
        "Latency spike": "Hausse importante de latence — relancez le test.",
    }
    if check.error in messages:
        return messages[check.error]
    if check.status_code is not None and check.status_code != 200:
        return f"Test refusé (HTTP {check.status_code}) — réessayez ou vérifiez le fournisseur."
    return "Vérification échouée — relancez le test."


def render(
    request: Request, template: str, runtime: Runtime, extra: dict[str, object] | None = None
) -> HTMLResponse:
    order = request.query_params.get("sort", "best")
    if order not in SORT_OPTIONS:
        order = "best"
    all_proxies = runtime.pool.all()
    city_names = {p.id: runtime.pool.city_name(p.id) for p in all_proxies}
    filter_city = request.query_params.get("city", "").strip()[:128]
    filter_country = request.query_params.get("country", "").strip().upper()[:2]
    filter_status = request.query_params.get("status", "")
    if filter_status not in STATUS_FILTERS:
        filter_status = ""
    visible = tuple(
        p
        for p in all_proxies
        if (
            filter_status == "all"
            or (p.status == filter_status if filter_status else p.status != "dead")
        )
        and (not filter_country or runtime.pool.country_code(p.id) == filter_country)
        and (
            not filter_city
            or normalized_text(filter_city) in normalized_text(city_names[p.id] or "")
        )
    )
    context: dict[str, object] = {
        "sort_order": order,
        "sort_options": SORT_OPTIONS,
        "proxies": ordered_proxies(visible, order, city_names),
        "filter_city": filter_city,
        "filter_country": filter_country,
        "filter_status": filter_status,
        "status_filters": STATUS_FILTERS,
        "filters_active": bool(filter_city or filter_country or filter_status),
        "visible_count": len(visible),
        "pool_count": len(all_proxies),
        "stats": runtime.pool.stats(runtime.rotator.strategy),
        "gateway": runtime.gateway,
        "checks": runtime.checker.batch_status,
        "pinned_ids": runtime.pool.pinned_ids(),
        "activity": runtime.activity(),
        "exits": runtime.pool.exit_observations(),
        "city_names": city_names,
        "check_failures": {
            p.id: check_failure(runtime.pool.latest_check(p.id)) for p in runtime.pool.all()
        },
        "country_names": {p.id: runtime.pool.country_name(p.id) for p in runtime.pool.all()},
        "countries": sorted(
            {
                (runtime.pool.country_code(p.id), runtime.pool.country_name(p.id))
                for p in runtime.pool.all()
                if runtime.pool.country_code(p.id)
            }
        ),
        "cities": sorted(
            {
                (
                    runtime.pool.country_code(p.id) or "",
                    runtime.pool.region_name(p.id) or "",
                    runtime.pool.city_name(p.id) or "",
                )
                for p in runtime.pool.all()
                if runtime.pool.city_name(p.id)
            }
        ),
        "selection": runtime.rotator.selection,
        "interval_minutes": runtime.rotator.interval_minutes,
        "geo_enabled": bool(runtime.geo and runtime.geo.enabled),
        "provider_dichvusocks_enabled": bool(runtime.dichvusocks and runtime.dichvusocks.enabled),
    }
    context.update(extra or {})
    if template == "_row_oob.html":
        context["proxy"] = next((p for p in visible if p.id == context.get("proxy_id")), None)
    return templates.TemplateResponse(request=request, name=template, context=context)


def feedback(
    request: Request,
    message: str,
    *,
    errors: list[dict[str, object]] | None = None,
    clear_import: bool = False,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="_feedback.html",
        context={"message": message, "errors": errors or []},
    )
    response.headers["HX-Trigger"] = "pool-refresh"
    if clear_import:
        response.headers["X-Clear-Import"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return response
