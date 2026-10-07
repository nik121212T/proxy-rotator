"""Proxy management API; credential input never becomes public output."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from app.models import CheckResult, Proxy, ProxyStatus
from app.parser import ImportReport
from app.runtime import Runtime, get_runtime
from app.ui.rendering import feedback

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
    report = await runtime.pool.import_text(body.text.get_secret_value(), label=body.label)
    if request.headers.get("HX-Request") == "true":
        return feedback(
            request,
            f"{report.imported} proxy(s) importé(s) · {report.skipped} ligne(s) ignorée(s).",
            errors=[e.model_dump() for e in report.errors],
            clear_import=not report.errors,
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


@router.post("/test-all", response_model=dict[str, int])
async def test_all(runtime: Services, request: Request) -> dict[str, int] | HTMLResponse:
    queued = runtime.checker.queue_all()
    if request.headers.get("HX-Request") == "true":
        return feedback(request, f"{queued} vérification(s) en cours.")
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
            return feedback(
                request,
                "Proxy disponible."
                if result.ok
                else "Vérification échouée. Consultez l’état du proxy.",
            )
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
