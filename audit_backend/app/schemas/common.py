"""Common response schemas aligned with openapi.yaml ApiResponse / HealthData / enums."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

Domain = Literal[
    "说明书", "总图", "门端图", "侧板图", "前端图", "底架图", "顶板图", "商标图", "全局通用"
]
RuleSource = Literal["json_field", "spatial", "vlm", "doc_compare"]
TaskStatus = Literal[
    "created", "incomplete", "running", "aggregating", "completed", "failed", "interrupted"
]
Verdict = Literal["pass", "fail", "warning", "skipped", "error", "pending"]
ItemStatus = Literal["pending", "running", "done"]
EngineKind = Literal["rule", "vlm"]
FileKind = Literal["api_json", "spatial", "pdf", "docx", "trademark"]
Priority = Literal["P0", "P1"]


class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    data: T | None = None
    message: str = ""


class HealthData(BaseModel):
    status: Literal["ok"] = "ok"
    db_writable: bool
    vlm_configured: bool
    vlm_dry_run: bool = False
    version: str = "1.0.0"


class ErrorBody(BaseModel):
    code: int
    data: Any = None
    message: str


class ApiError(Exception):
    """Business error carrying an API error code (4001/4002/4090/...)."""

    def __init__(self, code: int, message: str, http_status: int = 400, data=None) -> None:  # noqa: ANN001
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data
