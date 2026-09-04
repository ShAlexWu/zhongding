"""GET /api/v1/health — liveness + dependency checks."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.deps import ok
from app.core.config import settings
from app.core.db import check_db_writable
from app.schemas.common import ApiResponse, HealthData

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[HealthData])
def health() -> dict:
    vlm_configured = bool(settings.dashscope_api_key)
    data = {
        "status": "ok",
        "db_writable": check_db_writable(),
        "vlm_configured": vlm_configured,
        "vlm_dry_run": settings.vlm_dry_run or not vlm_configured,
        "version": settings.app_version,
    }
    return ok(data)
