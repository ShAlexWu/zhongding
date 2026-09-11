"""Core business tables: rule_results and job_events (SPEC section 6)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class RuleResult(Base):
    __tablename__ = "rule_results"
    __table_args__ = (
        UniqueConstraint("project_id", "rule_key", name="uq_project_rule"),
        Index("ix_rule_results_project_verdict", "project_id", "verdict"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    rule_key: Mapped[str] = mapped_column(String(16))
    domain: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(12), default="pending")
    # verdict: pending/pass/fail/warning/skipped/error
    engine: Mapped[str] = mapped_column(String(8), default="rule")  # rule/vlm
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/running/done
    is_starred: Mapped[bool] = mapped_column(default=False)
    priority: Mapped[str] = mapped_column(String(4), default="P1")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    vlm_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_cny: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    rule_key: Mapped[str | None] = mapped_column(String(16), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
