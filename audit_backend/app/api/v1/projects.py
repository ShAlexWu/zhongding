"""Projects endpoints: create/list/detail/start/events/report."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session, ok, project_to_dict, project_with_files_to_dict
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import JobEvent, Project, ProjectFile, RuleResult
from app.schemas.common import ApiError
from app.schemas.project import (
    CreateProjectRequest,
    CustomerInputs,
    ProjectFileInput,
    RevisionReason,
)
from app.services import task_service
from app.services.annotated_pdf import build_annotated_pdf
from app.workers.worker import Job, Runner

router = APIRouter(tags=["projects"])

_runner: Runner | None = None
_MIB = 1024 * 1024
_MAX_FILE = 50 * _MIB
_MAX_BATCH = 200 * _MIB
_EXTENSIONS = {
    "pdf": ".pdf",
    "trademark": ".pdf",
    "api_json": ".json",
    "spatial": ".json",
    "docx": ".docx",
}
_MIME_TYPES = {
    "pdf": {"application/pdf", "application/octet-stream"},
    "trademark": {"application/pdf", "application/octet-stream"},
    "api_json": {"application/json", "text/json", "application/octet-stream"},
    "spatial": {"application/json", "text/json", "application/octet-stream"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
}


def set_runner(runner: Runner) -> None:
    global _runner
    _runner = runner


@router.post("/projects", status_code=201)
def create_project(req: CreateProjectRequest) -> dict:
    project = task_service.create_project(req)
    return ok(project_to_dict(project))


@router.post("/projects/upload", status_code=201)
async def upload_project(
    name: str = Form(""),
    tech_req: str = Form(...),
    new_material: str = Form(""),
    revision_reason_type: str = Form(...),
    revision_reason_other_text: str = Form(""),
    files: list[UploadFile] = File(...),
    kinds: list[str] = Form(...),
) -> dict:
    """Persist one upload batch, then reuse the existing create/index path."""
    if len(files) != len(kinds):
        raise ApiError(4001, "files 与 kinds 数量不一致")
    if not files:
        raise ApiError(4001, "至少上传一个文件")
    batch_dir = settings.upload_dir / uuid.uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=False)
    saved: list[ProjectFileInput] = []
    seen: set[str] = set()
    total = 0
    try:
        for upload, kind in zip(files, kinds, strict=True):
            filename = upload.filename or ""
            if kind not in _EXTENSIONS:
                raise ApiError(4001, f"不支持的文件类型: {kind}")
            if not filename or Path(filename).name != filename or filename in seen:
                raise ApiError(4001, f"非法或重复的文件名: {filename}")
            if Path(filename).suffix.lower() != _EXTENSIONS[kind]:
                raise ApiError(4001, f"文件扩展名与类型不匹配: {filename}")
            if (upload.content_type or "application/octet-stream").lower() not in _MIME_TYPES[kind]:
                raise ApiError(4001, f"文件内容类型不匹配: {filename}")
            seen.add(filename)
            destination = batch_dir / filename
            size = 0
            with destination.open("xb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > _MAX_FILE:
                        raise ApiError(4130, f"单文件超过 50 MiB: {filename}", http_status=413)
                    if total > _MAX_BATCH:
                        raise ApiError(4130, "整批文件超过 200 MiB", http_status=413)
                    handle.write(chunk)
            _validate_file(destination, kind)
            saved.append(ProjectFileInput(kind=kind, path=str(destination)))

        request = CreateProjectRequest(
            name=name,
            customer_inputs=CustomerInputs(
                tech_req=tech_req,
                new_material=new_material,
                revision_reason=RevisionReason(
                    type=revision_reason_type,
                    other_text=revision_reason_other_text,
                ),
            ),
            files=saved,
        )
        try:
            project = task_service.create_project(request)
        except ApiError as exc:
            project_id = (exc.data or {}).get("project_id") if isinstance(exc.data, dict) else None
            if project_id:
                with SessionLocal() as cleanup_session:
                    failed = cleanup_session.get(Project, project_id)
                    if failed is not None:
                        cleanup_session.delete(failed)
                        cleanup_session.commit()
            raise
        return ok(project_to_dict(project))
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise
    finally:
        for upload in files:
            await upload.close()


@router.get("/projects")
def list_projects(
    session: Session = Depends(get_session),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    total = session.query(Project).count()
    rows = (
        session.query(Project)
        .order_by(Project.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    items = []
    for project in rows:
        files = session.query(ProjectFile).filter_by(project_id=project.id).all()
        items.append(project_with_files_to_dict(project, files))
    return ok(
        {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": page * limit < total,
        }
    )


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    project, files = _load_project(session, project_id)
    detail = task_service.get_project_detail(session, project)
    return ok(
        {
            "project": project_with_files_to_dict(project, files),
            "summary": detail["summary"],
            "per_domain_summary": detail["per_domain_summary"],
        }
    )


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    project, files = _load_project(session, project_id)
    if project.status in ("running", "aggregating"):
        raise ApiError(4090, "任务正在执行，不能删除", http_status=409)
    session.delete(project)
    session.commit()
    _remove_managed_uploads(files)
    return ok({"project_id": project_id, "deleted": True})


@router.post("/projects/{project_id}/start")
def start_project(project_id: str, session: Session = Depends(get_session)) -> dict:
    result = task_service.start_project(session, project_id)
    session.commit()
    if _runner is not None:
        _runner.queue.put_nowait(Job(project_id=project_id))
    return ok(result)


@router.get("/projects/{project_id}/events")
async def stream_events(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    resume_id = request.headers.get("last-event-id", "")
    last_id = int(resume_id) if resume_id.isdigit() else _last_event_id(session, project_id)

    async def event_stream():
        nonlocal last_id
        heartbeat_elapsed = 0.0
        while True:
            if await request.is_disconnected():
                break
            rows = (
                session.query(JobEvent)
                .filter(JobEvent.project_id == project_id, JobEvent.id > last_id)
                .order_by(JobEvent.id)
                .all()
            )
            for row in rows:
                payload = {"rule_key": row.rule_key, **row.payload}
                yield (
                    f"id: {row.id}\nevent: {row.event_type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
                last_id = row.id
                heartbeat_elapsed = 0.0
            if not rows:
                heartbeat_elapsed += 0.5
                if heartbeat_elapsed >= 30:
                    yield "event: heartbeat\ndata: {}\n\n"
                    heartbeat_elapsed = 0.0
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/projects/{project_id}/report", response_model=None)
def get_report(
    project_id: str,
    format: str = Query("md", pattern="^(md|json|pdf)$"),
    session: Session = Depends(get_session),
) -> dict | Response:
    project, files = _load_project(session, project_id)
    if format == "pdf":
        results = (
            session.query(RuleResult)
            .filter_by(project_id=project.id)
            .order_by(RuleResult.rule_key)
            .all()
        )
        content = build_annotated_pdf(project.name, files, results)
        filename = quote(f"审图批注-{project.name}.pdf")
        return Response(
            content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
    return ok(task_service.report_data(session, project, format))


# ------------------------------------------------------------------ helpers


def _load_project(session: Session, project_id: str):
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError(404, "project not found", http_status=404)
    files = session.query(ProjectFile).filter_by(project_id=project_id).all()
    return project, files


def _last_event_id(session: Session, project_id: str) -> int:
    row = (
        session.query(JobEvent.id)
        .filter_by(project_id=project_id)
        .order_by(JobEvent.id.desc())
        .first()
    )
    return row[0] if row else 0


def _validate_file(path: Path, kind: str) -> None:
    with path.open("rb") as handle:
        head = handle.read(8)
    if kind in ("pdf", "trademark") and not head.startswith(b"%PDF-"):
        raise ApiError(4001, f"PDF 文件头无效: {path.name}")
    if kind in ("api_json", "spatial"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = next((char for char in iter(lambda: handle.read(1), "") if not char.isspace()), "")
            if first not in ("{", "["):
                raise ValueError("not JSON")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(4001, f"JSON 内容无效: {path.name}") from exc
    if kind == "docx":
        try:
            with zipfile.ZipFile(path) as archive:
                if "[Content_Types].xml" not in archive.namelist():
                    raise ValueError("missing content types")
        except (zipfile.BadZipFile, ValueError) as exc:
            raise ApiError(4001, f"DOCX 内容无效: {path.name}") from exc


def _remove_managed_uploads(files: list[ProjectFile]) -> None:
    root = settings.upload_dir.resolve()
    parents = {Path(row.path).resolve().parent for row in files}
    for parent in parents:
        if parent.parent == root:
            shutil.rmtree(parent, ignore_errors=True)
