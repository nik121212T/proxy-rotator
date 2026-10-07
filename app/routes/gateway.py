"""Gateway API router; endpoints follow in the next backlog tasks."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/gateway", tags=["gateway"])
