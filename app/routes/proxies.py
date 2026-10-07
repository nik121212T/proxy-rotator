"""Proxy management API; credential input never becomes public output."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, SecretStr

from app.models import CheckResult, Proxy, ProxyStatus
from app.parser import ImportReport
from app.runtime import Runtime, get_runtime

router = APIRouter(prefix="/api/proxies", tags=["proxies"])
Services = Annotated[Runtime, Depends(get_runtime)]


class ImportRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    text: SecretStr
    label: str | None = None


@router.post("/import")
async def import_proxies(body: ImportRequest, runtime: Services) -> ImportReport:
    return await runtime.pool.import_text(body.text.get_secret_value(), label=body.label)


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


@router.post("/test-all")
async def test_all(runtime: Services) -> dict[str, int]:
    return {"queued": runtime.checker.queue_all()}


@router.get("/{proxy_id}")
async def proxy_detail(proxy_id: int, runtime: Services) -> ProxyDetail:
    try:
        proxy, checks = await runtime.pool.detail(proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None
    return ProxyDetail(**proxy.__dict__, checks=checks)


@router.post("/{proxy_id}/test")
async def test_proxy(proxy_id: int, runtime: Services) -> CheckResult:
    try:
        return await runtime.checker.check_one(proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(proxy_id: int, runtime: Services) -> Response:
    await runtime.checker.cancel_one(proxy_id)
    try:
        await runtime.pool.soft_delete(proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proxy not found") from None
    return Response(status_code=204)
