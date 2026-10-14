"""Proxy management API; credential input never becomes public output."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.models import CheckResult, Proxy, ProxyStatus
from app.parser import ImportReport
from app.runtime import Runtime, get_runtime
from app.views import feedback, render

router = APIRouter(prefix="/api/proxies", tags=["proxies"])
Services = Annotated[Runtime, Depends(get_runtime)]


class ImportRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    text: SecretStr
    label: str | None = None


@router.post("/import", response_model=ImportReport)
async def import_proxies(
    body: ImportRequest, runtime: Services, request: Request
) -> ImportReport | HTMLResponse:
    report = await runtime.pool.import_text(body.text.get_secret_value(), label=body.label, verify=runtime.checker.probe_candidate)
    if request.headers.get("HX-Request") == "true":
        return feedback(
            request,
            f"{report.imported} proxy(s) importé(s) : "
            f"{report.with_auth} avec identifiants · {report.without_auth} sans identifiants. "
            f"{report.failed} échec(s) au test · {report.skipped} ligne(s) ignorée(s) au total.",
            errors=[e.model_dump() for e in report.errors],
            clear_import=not report.errors and not report.failed,
        )
    return report


class ProxyPage(BaseModel):
    items: tuple[Proxy, ...]
    total: int


@router.get("")
async def list_proxies(
    runtime: Services,
    status: ProxyStatus | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=100, allow_inf_nan=False)] = None,
    limit: Annotated[int, Query(ge=1)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProxyPage:
    matches = tuple(
        p
        for p in runtime.pool.all()
        if (p.status == status if status else p.status != "dead")
        and (min_score is None or p.score >= min_score)
    )
    return ProxyPage(items=matches[offset : offset + limit], total=len(matches))


class ProxyDetail(Proxy):
    checks: tuple[CheckResult, ...]


@router.post("/{proxy_id}/pin", response_model=dict[str, bool])
async def pin_proxy(
    proxy_id: int, runtime: Services, request: Request
) -> dict[str, bool] | HTMLResponse:
    try:
        added = runtime.pool.pin(proxy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Proxy not found") from exc
    if request.headers.get("HX-Request") == "true":
        return feedback(request, f"Proxy #{proxy_id} utilisé en priorité pour les prochaines connexions, s’il est disponible et correspond aux filtres.")
    return {"pinned": True, "changed": added}


@router.post("/{proxy_id}/unpin", response_model=dict[str, bool])
async def unpin_proxy(
    proxy_id: int, runtime: Services, request: Request
) -> dict[str, bool] | HTMLResponse:
    removed = runtime.pool.unpin(proxy_id)
    if request.headers.get("HX-Request") == "true":
        return feedback(request, f"Priorité retirée du proxy #{proxy_id}.")
    return {"pinned": False, "changed": removed}


@router.get("/export", response_class=Response)
async def export_proxies(
    runtime: Services,
    status: Annotated[ProxyStatus | None, Query()] = "alive",
    country: Annotated[list[Annotated[str, Field(pattern=r"^(?:[A-Z]{2}|unknown)$")]] | None, Query()] = None,
) -> Response:
    """Plain-text download of proxies (default: alive only)."""
    from datetime import UTC, datetime  # noqa: PLC0415 - keep import local

    lines = runtime.pool.export_lines(status=status, countries=set(country or []))
    body = "\n".join(lines) + ("\n" if lines else "")
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    countries_label = "-".join(
        "pays-inconnu" if code == "unknown" else code for code in sorted(set(country or []))
    ) or "tous-pays"
    filename = f"proxies-{status or 'all'}-{countries_label}-{stamp}.txt"
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Line-Count": str(len(lines)),
        },
    )


@router.post("/test-all", response_model=dict[str, int])
async def test_all(runtime: Services, request: Request) -> dict[str, int] | HTMLResponse:
    queued = runtime.checker.queue_all()
    if request.headers.get("HX-Request") == "true":
        response = render(request, "_check_controls.html", runtime)
        response.headers["Cache-Control"] = "no-store"
        response.headers["HX-Trigger"] = "pool-refresh"
        return response
    return {"queued": queued}


@router.get("/{proxy_id}")
async def proxy_detail(proxy_id: int, runtime: Services) -> ProxyDetail:
    try:
        proxy, checks = await runtime.pool.detail(proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None
    return ProxyDetail(**proxy.__dict__, checks=checks)


@router.post("/{proxy_id}/test", response_model=CheckResult)
async def test_proxy(
    proxy_id: int, runtime: Services, request: Request
) -> CheckResult | HTMLResponse:
    try:
        result = await runtime.checker.check_one(proxy_id)
        if request.headers.get("HX-Request") == "true":
            from app.views import templates

            text = (
                f"OK · {result.latency_ms} ms"
                if result.ok
                else f"KO · {(result.error or 'échec')[:28]}"
            )
            response = templates.TemplateResponse(
                request=request,
                name="_test_result.html",
                context={"proxy_id": proxy_id, "ok": result.ok, "text": text},
            )
            response.headers["HX-Trigger"] = "pool-refresh"
            response.headers["Cache-Control"] = "no-store"
            return response
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(proxy_id: int, runtime: Services) -> Response:
    await runtime.checker.cancel_one(proxy_id)
    try:
        await runtime.pool.soft_delete(proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None
    return Response(status_code=204, headers={"HX-Trigger": "pool-refresh"})
