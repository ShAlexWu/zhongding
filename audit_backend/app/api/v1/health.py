"""GET /api/v1/health — liveness + dependency checks."""

from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.api.v1.deps import ok
from app.core.config import settings
from app.core.db import check_db_writable
from app.schemas.common import ApiError, ApiResponse, HealthData
from app.workers.worker import Runner

router = APIRouter(tags=["health"])
_runner: Runner | None = None


class ModelKeyRequest(BaseModel):
    api_key: str


def set_runner(runner: Runner) -> None:
    global _runner
    _runner = runner


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


@router.put("/internal/model-key")
def update_model_key(
    payload: ModelKeyRequest,
    internal_token: str = Header("", alias="X-Internal-Config-Token"),
) -> dict:
    expected = os.environ.get("PASSWORD", "")
    expected_token = hashlib.sha256(expected.encode()).hexdigest()
    if not expected or not secrets.compare_digest(internal_token, expected_token):
        raise ApiError(4031, "forbidden", http_status=403)
    api_key = payload.api_key.strip()
    if not 8 <= len(api_key) <= 512:
        raise ApiError(4001, "invalid model key", http_status=400)
    if _runner is None:
        raise ApiError(5031, "runner unavailable", http_status=503)
    _runner.set_api_key(api_key)
    return ok({"configured": True, "runtime_only": True})
