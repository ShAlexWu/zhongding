"""Rules endpoints: list results / single detail / rerun."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session, ok
from app.core.logging import get_logger
from app.models import ProjectFile, RuleResult
from app.schemas.common import ApiError
from app.services import task_service
from app.services.manual_pdf import anchor_manual_evidence, manual_pdf_path
from app.workers.worker import Job, Runner

router = APIRouter(tags=["rules"])
logger = get_logger(__name__)

_runner: Runner | None = None


def set_runner(runner: Runner) -> None:
    global _runner
    _runner = runner


def _result_to_dict(row: RuleResult, evidence: list[dict] | None = None) -> dict:
    meta = (row.vlm_raw or {}).get("_meta") or {}
    return {
        "rule_key": row.rule_key,
        "domain": row.domain,
        "title": row.title,
        "verdict": row.verdict,
        "engine": row.engine,
        "status": row.status,
        "is_starred": row.is_starred,
        "priority": row.priority,
        "evidence": evidence if evidence is not None else row.evidence or [],
        "vlm_raw": row.vlm_raw,
        "conclusion": row.conclusion,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_cny": float(row.cost_cny) if row.cost_cny is not None else None,
        "latency_ms": row.latency_ms,
        "attempts": row.attempts,
        "vlm_attempts": int(meta.get("vlm_attempts") or 0),
        "error": row.error,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.get("/projects/{project_id}/rules")
def list_rule_results(
    project_id: str,
    domain: str | None = Query(None),
    verdict: str | None = Query(None),
    session: Session = Depends(get_session),
) -> dict:
    _ensure_project(session, project_id)
    query = session.query(RuleResult).filter_by(project_id=project_id)
    filters: dict[str, str] = {}
    if domain:
        query = query.filter(RuleResult.domain == domain)
        filters["domain"] = domain
    if verdict:
        query = query.filter(RuleResult.verdict == verdict)
        filters["verdict"] = verdict
    rows = query.order_by(RuleResult.rule_key).all()
    evidence_groups = _anchor_manual_groups(session, project_id, rows)
    return ok(
        {
            "items": [_result_to_dict(row, evidence) for row, evidence in zip(rows, evidence_groups, strict=True)],
            "total": len(rows),
            "filters_applied": filters,
        }
    )


@router.get("/projects/{project_id}/rules/{rule_key}")
def get_rule_result(
    project_id: str,
    rule_key: str,
    session: Session = Depends(get_session),
) -> dict:
    row = _load_rule(session, project_id, rule_key)
    evidence = _anchor_manual_groups(session, project_id, [row])[0]
    return ok(_result_to_dict(row, evidence))


@router.post("/projects/{project_id}/rules/{rule_key}/rerun", status_code=202)
def rerun_rule(
    project_id: str,
    rule_key: str,
    session: Session = Depends(get_session),
) -> dict:
    result = task_service.rerun_rule(session, project_id, rule_key)
    session.commit()
    if _runner is not None:
        _runner.queue.put_nowait(Job(project_id=project_id, rule_key=rule_key))
    return ok(result)


# ------------------------------------------------------------------ helpers


def _ensure_project(session: Session, project_id: str) -> None:
    from app.models import Project

    if session.get(Project, project_id) is None:
        raise ApiError(404, "project not found", http_status=404)


def _load_rule(session: Session, project_id: str, rule_key: str) -> RuleResult:
    _ensure_project(session, project_id)
    row = (
        session.query(RuleResult)
        .filter_by(project_id=project_id, rule_key=rule_key)
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, f"rule {rule_key} not found", http_status=404)
    return row


def _anchor_manual_groups(session: Session, project_id: str, rows: list[RuleResult]) -> list[list[dict]]:
    groups = [list(row.evidence or []) for row in rows]
    manual = session.query(ProjectFile).filter_by(project_id=project_id, kind="docx").first()
    if manual is None or not any(any(item.get("type") == "doc" for item in group) for group in groups):
        return groups
    try:
        return anchor_manual_evidence(manual_pdf_path(manual), manual.id, groups)
    except Exception as exc:  # noqa: BLE001 - evidence remains readable without a locator
        logger.warning("manual evidence anchoring failed project=%s: %s", project_id, exc)
        return groups
