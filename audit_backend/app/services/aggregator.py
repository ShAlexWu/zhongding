"""Rule result aggregation into summaries and markdown/json reports."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import RuleResult

DOMAIN_ORDER = [
    "说明书",
    "总图",
    "门端图",
    "侧板图",
    "前端图",
    "底架图",
    "顶板图",
    "商标图",
    "全局通用",
]
VERDICTS = ["pass", "fail", "warning", "skipped", "error", "pending"]


def _verdict_counts(session: Session, project_id: str) -> dict[str, int]:
    rows = (
        session.query(RuleResult.verdict, func.count(RuleResult.id))
        .filter_by(project_id=project_id)
        .group_by(RuleResult.verdict)
        .all()
    )
    return {verdict: 0 for verdict in VERDICTS} | {v: int(c) for v, c in rows}


def summarize(session: Session, project_id: str) -> tuple[dict, list[dict]]:
    """Returns (summary_counts, per_domain_summary)."""
    summary = _verdict_counts(session, project_id)
    per_domain: list[dict] = []
    for domain in DOMAIN_ORDER:
        rows = (
            session.query(RuleResult.verdict, func.count(RuleResult.id))
            .filter_by(project_id=project_id, domain=domain)
            .group_by(RuleResult.verdict)
            .all()
        )
        counts = {v: 0 for v in VERDICTS}
        for v, c in rows:
            counts[v] = int(c)
        per_domain.append({"domain": domain, "total": sum(counts.values()), **counts})
    return summary, per_domain


def build_markdown(session: Session, project_id: str, project_name: str) -> str:
    summary, per_domain = summarize(session, project_id)
    results = (
        session.query(RuleResult)
        .filter_by(project_id=project_id)
        .order_by(RuleResult.rule_key)
        .all()
    )
    lines = [
        f"# 审图报告 - {project_name}",
        "",
        f"- 生成时间: {datetime.now(timezone.utc).isoformat()}",
        f"- 通过 {summary['pass']} / 不通过 {summary['fail']} / 需人工复核 {summary['warning']} / "
        f"跳过 {summary['skipped']} / 错误 {summary['error']} / 待审 {summary['pending']}",
        "",
    ]
    for domain in DOMAIN_ORDER:
        domain_items = [r for r in results if r.domain == domain]
        if not domain_items:
            continue
        lines.append(f"## {domain} ({len(domain_items)} 项)")
        lines.append("")
        for r in domain_items:
            star = "*" if r.is_starred else " "
            lines.append(f"- [{star}] {r.rule_key} **{r.verdict}** {r.title}")
            if r.conclusion:
                lines.append(f"  - 结论: {r.conclusion}")
            for ev in (r.evidence or [])[:2]:
                lines.append(f"  - 证据: {ev.get('text', '')[:120]}")
        lines.append("")
    return "\n".join(lines)
