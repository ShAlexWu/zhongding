"""Project-related schemas aligned with openapi.yaml."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import Domain, FileKind, TaskStatus


class RevisionReason(BaseModel):
    type: str = Field(pattern="^(部门要求|标准更新|其他)$")
    other_text: str = ""


class CustomerInputs(BaseModel):
    tech_req: str = Field(min_length=1)
    new_material: str = ""
    revision_reason: RevisionReason


class ProjectFileInput(BaseModel):
    kind: FileKind
    path: str = Field(min_length=1)


class CreateProjectRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    customer_inputs: CustomerInputs
    files: list[ProjectFileInput] = Field(min_length=1)


class ProjectFile(BaseModel):
    id: str
    kind: str
    code: str = ""
    path: str
    sha256: str
    size_bytes: int
    indexed_at: datetime | None = None
    index_error: str | None = None


class Project(BaseModel):
    id: str
    name: str
    status: TaskStatus
    customer_inputs: dict
    files: list[ProjectFile] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    incomplete_reason: str | None = None


class ProjectListData(BaseModel):
    items: list[Project]
    total: int
    page: int
    limit: int
    has_more: bool


class DomainSummary(BaseModel):
    domain: Domain | str
    total: int = 0
    pass_: int = Field(default=0, alias="pass")
    fail: int = 0
    warning: int = 0
    skipped: int = 0
    error: int = 0
    pending: int = 0

    model_config = {"populate_by_name": True}


class SummaryCounts(BaseModel):
    pass_: int = Field(default=0, alias="pass")
    fail: int = 0
    warning: int = 0
    skipped: int = 0
    error: int = 0

    model_config = {"populate_by_name": True}


class ProjectDetail(BaseModel):
    project: Project
    summary: SummaryCounts
    per_domain_summary: list[DomainSummary]


class StartResult(BaseModel):
    task_status: TaskStatus
    started_at: datetime
