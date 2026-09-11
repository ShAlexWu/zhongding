"""Rule-related schemas aligned with openapi.yaml."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import Domain, EngineKind, ItemStatus, Priority, RuleSource, Verdict


class RuleDefinition(BaseModel):
    rule_key: str
    domain: Domain
    title: str
    source: RuleSource
    priority: Priority
    is_starred: bool
    target_files: list[str] = []
    on_missing: str = "warning"


class EvidenceAnchor(BaseModel):
    type: str  # doc/json/pdf/input
    file_id: str | None = None
    page: int | None = None
    rect: dict | None = None
    text: str
    side: str | None = None  # manual/drawing
    checkpoint: str | None = None
    checkpoint_verdict: str | None = None
    source_url: str | None = None
    checked_at: str | None = None


class RuleResultOut(BaseModel):
    rule_key: str
    domain: Domain
    title: str
    verdict: Verdict
    engine: EngineKind
    status: ItemStatus
    is_starred: bool
    priority: Priority
    evidence: list[EvidenceAnchor] = []
    vlm_raw: dict | None = None
    conclusion: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_cny: float | None = None
    latency_ms: int | None = None
    attempts: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RuleResultListData(BaseModel):
    items: list[RuleResultOut]
    total: int
    filters_applied: dict = {}


class RerunResult(BaseModel):
    rule_key: str
    status: ItemStatus
    attempts: int


class RulesetData(BaseModel):
    items: list[RuleDefinition]
    total: int


class ReportData(BaseModel):
    format: str
    content: str
    generated_at: datetime
