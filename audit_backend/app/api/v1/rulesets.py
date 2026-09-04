"""GET /api/v1/rulesets — 40-item rule catalog with source routing."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.deps import ok
from app.services.rule_engine import RULES

router = APIRouter(tags=["rulesets"])


@router.get("/rulesets")
def list_rulesets() -> dict:
    items = [
        {
            "rule_key": r.rule_key,
            "domain": r.domain,
            "title": r.title,
            "source": r.source,
            "priority": r.priority,
            "is_starred": r.is_starred,
            "target_files": r.target_files,
            "on_missing": r.on_missing,
        }
        for r in RULES
    ]
    return ok({"items": items, "total": len(items)})
