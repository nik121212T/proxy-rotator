"""Routes for third-party SOCKS5 provider integrations (opt-in)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, SecretStr

from app.parser import parse_line
from app.providers.dichvusocks import DichvusocksClient, DichvusocksError
from app.routes.proxies import Services
from app.runtime import Runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers/dichvusocks", tags=["providers"])


@router.get("/status")
async def status(runtime: Services) -> dict[str, Any]:
    client = runtime.dichvusocks
    return {"enabled": client is not None and client.enabled}


@router.get("/user-info")
async def user_info(runtime: Services) -> dict[str, Any]:
    client = _require_client(runtime)
    try:
        return await client.user_info()
    except DichvusocksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/countries")
async def countries(runtime: Services) -> list[str]:
    client = _require_client(runtime)
    try:
        return await client.countries()
    except DichvusocksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/preview")
async def preview(
    runtime: Services,
    country: Annotated[str | None, Query()] = None,
    region: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    page: Annotated[int, Query(ge=1)] = 1,
) -> list[dict[str, Any]]:
    """List sock entries WITHOUT viewing them — no credit spent."""
    client = _require_client(runtime)
    try:
        return await client.list_socks(
            country=country, region=region, city=city, limit=limit, page=page
        )
    except DichvusocksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/import")
async def import_from_provider(
    runtime: Services,
    country: Annotated[str | None, Query()] = None,
    region: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
) -> dict[str, Any]:
    """DEDUCTS 1 credit per sock. Views then imports into the local pool."""
    client = _require_client(runtime)
    try:
        listing = await client.list_socks(
            country=country, region=region, city=city, limit=limit, page=1
        )
    except DichvusocksError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    lines: list[str] = []
    skipped: list[dict[str, Any]] = []
    country_codes: list[str] = []
    for entry in listing[:limit]:
        sock_id = entry.get("id")
        if not isinstance(sock_id, int):
            skipped.append({"id": sock_id, "reason": "no id"})
            continue
        try:
            sock = await client.view(int(sock_id))
        except DichvusocksError as exc:
            skipped.append({"id": sock_id, "reason": str(exc)})
            continue
        if sock is None or not sock.username or not sock.password:
            skipped.append({"id": sock_id, "reason": "no credentials returned"})
            continue
        try:
            parse_line(
                f"socks5://{sock.username}:{sock.password}@{sock.ip}:{sock.port}"
            )
        except ValueError as exc:
            skipped.append({"id": sock_id, "reason": f"parse: {exc}"})
            continue
        lines.append(f"socks5://{sock.username}:{sock.password}@{sock.ip}:{sock.port}")
        if sock.country_code:
            country_codes.append(sock.country_code)
    if not lines:
        return {"imported": 0, "skipped": skipped}
    cc = country_codes[0] if country_codes else "??"
    report = await runtime.pool.import_text("\n".join(lines), label=f"dichvusocks:{cc}")
    runtime.checker.queue_all()
    return {
        "imported": report.imported,
        "deduped": len(lines) - report.imported,
        "skipped": skipped,
    }


def _require_client(runtime: Runtime) -> DichvusocksClient:
    client = runtime.dichvusocks
    if client is None or not client.enabled:
        raise HTTPException(status_code=404, detail="Dichvusocks provider disabled")
    return client


class TokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


@router.post("/token", status_code=204)
async def set_token(payload: TokenPayload, runtime: Services) -> Response:
    """Persist the Dichvusocks API token (encrypted) and hot-swap into the client."""
    if runtime.db_conn is None or not runtime.fernet_key:
        raise HTTPException(status_code=500, detail="Secret store unavailable")
    from app import provider_store  # noqa: PLC0415

    await provider_store.set_token(
        runtime.db_conn, "dichvusocks", payload.token, runtime.fernet_key  # type: ignore[arg-type]
    )
    if runtime.dichvusocks is not None:
        runtime.dichvusocks.set_token(payload.token)
    return Response(status_code=204)


@router.delete("/token", status_code=204)
async def clear_token(runtime: Services) -> Response:
    if runtime.db_conn is None or not runtime.fernet_key:
        raise HTTPException(status_code=500, detail="Secret store unavailable")
    from app import provider_store  # noqa: PLC0415

    await provider_store.set_token(
        runtime.db_conn, "dichvusocks", None, runtime.fernet_key  # type: ignore[arg-type]
    )
    if runtime.dichvusocks is not None:
        runtime.dichvusocks.set_token(None)
    return Response(status_code=204)
