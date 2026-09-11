"""Shared FastAPI helpers: session dependency + envelope builders."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import ProjectFile


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def ok(data=None, message: str = "") -> dict:
    return {"code": 0, "data": data, "message": message}


def err(code: int, message: str, data=None) -> dict:  # noqa: ANN001
    return {"code": code, "data": data, "message": message}


def file_to_dict(row: ProjectFile) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "code": row.code,
        "path": row.path,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
        "index_error": row.index_error,
    }


def project_to_dict(project) -> dict:  # noqa: ANN001
    return {
        "id": project.id,
        "name": project.name,
        "status": project.status,
        "customer_inputs": project.customer_inputs or {},
        "files": [],
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "finished_at": project.finished_at.isoformat() if project.finished_at else None,
        "incomplete_reason": project.incomplete_reason,
    }


def project_with_files_to_dict(project, files: list[ProjectFile]) -> dict:  # noqa: ANN001
    data = project_to_dict(project)
    data["files"] = [file_to_dict(f) for f in files]
    return data


SessionDep = Depends(get_session)  # type: ignore[valid-type]
