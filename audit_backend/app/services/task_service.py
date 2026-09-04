"""Task orchestration: two-level state machine + persistence helpers.

Task-level:   created -> running -> aggregating -> completed
              failed / interrupted / incomplete (SPEC section 4)
Item-level:   pending -> running -> pass|fail|warning|skipped|error
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.models import JobEvent, Project, RuleResult, utcnow
from app.schemas.common import ApiError
from app.schemas.project import CreateProjectRequest
from app.services import aggregator
from app.services.indexer import index_project_files, new_id, register_files
from app.services.rule_engine import RULES

logger = get_logger(__name__)

MAX_ATTEMPTS = 2  # SPEC: error retry <=2
_STARTABLE = {"created", "failed", "interrupted", "completed"}


def ensure_rule_rows(session: Session, project_id: str) -> None:
    """Create 40 pending rule_results rows on project creation."""
    for rule in RULES:
        session.add(
            RuleResult(
                project_id=project_id,
                rule_key=rule.rule_key,
                domain=rule.domain,
                title=rule.title,
                verdict="pending",
                engine="vlm" if rule.source == "vlm" else "rule",
                status="pending",
                is_starred=rule.is_starred,
                priority=rule.priority,
                evidence=[],
            )
        )


def create_project(req: CreateProjectRequest) -> Project:
    """Create + register + index + anchor-verify. On 4001/4002 the project is
    committed as 'incomplete' before the error is re-raised (SPEC AC-07)."""
    session = SessionLocal()
    project: Project | None = None
    try:
        project = Project(
            id=new_id("p"),
            name=req.name.strip(),
            status="created",
            customer_inputs=req.customer_inputs.model_dump(),
        )
        session.add(project)
        session.flush()
        register_files(session, project.id, req.files)
        index_project_files(session, project)
        ensure_rule_rows(session, project.id)
        session.commit()
        return project
    except ApiError as exc:
        if project is not None:
            project.status = "incomplete"
            project.incomplete_reason = exc.message
            session.commit()
            exc.data = {"project_id": project.id}
        raise
    finally:
        session.close()


def start_project(session: Session, project_id: str) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError(404, "project not found", http_status=404)
    if project.status in ("running", "aggregating"):
        raise ApiError(4090, "任务处于 running/aggregating，不能重复触发", http_status=409)
    if project.status == "incomplete":
        raise ApiError(4090, "任务 incomplete，无法启动（请先补全文件）", http_status=409)
    project.status = "running"
    project.finished_at = None
    now = utcnow()
    # reset all items for a clean (re)run
    session.query(RuleResult).filter_by(project_id=project_id).update(
        {"verdict": "pending", "status": "pending", "error": None, "attempts": 0}
    )
    push_event(session, project_id, None, "task_status", {"status": "running", "at": now.isoformat()})
    return {"task_status": project.status, "started_at": now}


def rerun_rule(session: Session, project_id: str, rule_key: str) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError(404, "project not found", http_status=404)
    row = (
        session.query(RuleResult)
        .filter_by(project_id=project_id, rule_key=rule_key)
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, f"rule {rule_key} not found", http_status=404)
    if row.attempts >= MAX_ATTEMPTS:
        raise ApiError(4090, "单项重试次数已达上限（2 次）", http_status=409)
    if row.status == "running":
        raise ApiError(4090, "该项正在执行中", http_status=409)
    if row.verdict not in ("fail", "error", "warning", "pending", "skipped"):
        raise ApiError(4090, f"仅 fail/error/warning 项可重跑，当前 {row.verdict}", http_status=409)
    row.attempts += 1
    row.verdict = "pending"
    row.status = "pending"
    row.conclusion = ""
    row.evidence = []
    row.vlm_raw = None
    row.tokens_in = None
    row.tokens_out = None
    row.cost_cny = None
    row.latency_ms = None
    row.error = None
    row.started_at = None
    row.finished_at = None
    if rule_key == "TM-01":
        dependent = (
            session.query(RuleResult)
            .filter_by(project_id=project_id, rule_key="MAN-01")
            .one_or_none()
        )
        if dependent is not None:
            dependent.verdict = "pending"
            dependent.status = "running"
            dependent.conclusion = ""
            dependent.evidence = []
            dependent.error = None
            dependent.started_at = None
            dependent.finished_at = None
    push_event(
        session, project_id, rule_key, "task_status",
        {"status": "rerun_requested", "attempts": row.attempts},
    )
    return {"rule_key": rule_key, "status": "pending", "attempts": row.attempts}


def recover_interrupted(session: Session) -> int:
    """Startup recovery: running/aggregating -> interrupted (SPEC AC-10)."""
    count = (
        session.query(Project)
        .filter(Project.status.in_(["running", "aggregating"]))
        .update({"status": "interrupted"}, synchronize_session=False)
    )
    session.query(RuleResult).filter(RuleResult.status == "running").update(
        {"status": "pending"}, synchronize_session=False
    )
    return int(count or 0)


def sync_rule_engines(session: Session) -> int:
    """Keep stored result metadata aligned with the current rule definitions."""
    changed = 0
    for rule in RULES:
        expected = "vlm" if rule.source == "vlm" else "rule"
        values = {
            "engine": expected,
            "domain": rule.domain,
            "title": rule.title,
            "is_starred": rule.is_starred,
            "priority": rule.priority,
        }
        changed += (
            session.query(RuleResult)
            .filter(
                RuleResult.rule_key == rule.rule_key,
                or_(
                    RuleResult.engine != expected,
                    RuleResult.domain != rule.domain,
                    RuleResult.title != rule.title,
                    RuleResult.is_starred != rule.is_starred,
                    RuleResult.priority != rule.priority,
                ),
            )
            .update(values, synchronize_session=False)
        )
    return changed


def push_event(
    session: Session,
    project_id: str,
    rule_key: str | None,
    event_type: str,
    payload: dict,
) -> None:
    session.add(
        JobEvent(
            project_id=project_id,
            rule_key=rule_key,
            event_type=event_type,
            payload=payload,
        )
    )


def finish_project(session: Session, project_id: str, status: str, reason: str = "") -> None:
    project = session.get(Project, project_id)
    if project is None:
        return
    project.status = status
    project.finished_at = utcnow() if status in ("completed", "failed") else None
    if reason:
        project.incomplete_reason = reason
    push_event(session, project_id, None, "task_status", {"status": status})


def get_project_detail(session: Session, project: Project) -> dict:
    summary, per_domain = aggregator.summarize(session, project.id)
    return {"summary": summary, "per_domain_summary": per_domain}


def report_data(session: Session, project: Project, fmt: str) -> dict:
    if fmt == "json":
        results = (
            session.query(RuleResult).filter_by(project_id=project.id).order_by(RuleResult.rule_key).all()
        )
        summary, per_domain = aggregator.summarize(session, project.id)
        import json

        content = json.dumps(
            {
                "project_id": project.id,
                "name": project.name,
                "status": project.status,
                "summary": summary,
                "per_domain_summary": per_domain,
                "items": [
                    {
                        "rule_key": r.rule_key,
                        "domain": r.domain,
                        "verdict": r.verdict,
                        "engine": r.engine,
                        "conclusion": r.conclusion,
                        "evidence": r.evidence,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        content = aggregator.build_markdown(session, project.id, project.name)
    return {"format": fmt, "content": content, "generated_at": datetime.now(timezone.utc)}
