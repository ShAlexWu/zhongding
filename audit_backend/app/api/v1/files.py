"""Files endpoints: raw file streaming (Range) + PDF page PNG rendering."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session
from app.core.config import settings
from app.core.logging import get_logger
from app.models import ProjectFile
from app.schemas.common import ApiError
from app.services.manual_pdf import manual_pdf_path

logger = get_logger(__name__)

router = APIRouter(tags=["files"])

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _load_file(session: Session, file_id: str) -> ProjectFile:
    row = session.get(ProjectFile, file_id)
    if row is None:
        raise ApiError(404, "file not found", http_status=404)
    if not Path(row.path).is_file():
        raise ApiError(404, "file missing on disk", http_status=404)
    return row


@router.get("/files/{file_id}")
def get_file(file_id: str, range: str | None = None, session: Session = Depends(get_session)) -> Response:
    row = _load_file(session, file_id)
    path = manual_pdf_path(row) if row.kind == "docx" else Path(row.path)
    media_type = (
        "application/pdf" if row.kind in ("pdf", "trademark", "docx")
        else "application/octet-stream"
    )
    size = path.stat().st_size

    if not range:
        return FileResponse(path, media_type=media_type, filename=path.name)

    match = _RANGE_RE.match(range)
    if not match:
        return Response(status_code=416)
    start_s, end_s = match.groups()
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416)
    length = end - start + 1

    def iterator():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1 << 16, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(iterator(), status_code=206, media_type=media_type, headers=headers)


@router.get("/files/{file_id}/pages/{page}")
def get_file_page(
    file_id: str,
    page: int,
    dpi: int = Query(150, ge=96, le=200),
    session: Session = Depends(get_session),
) -> Response:
    row = _load_file(session, file_id)
    if dpi not in (96, 150, 200):
        dpi = 150
    png_path = _render_page(row, page, dpi)
    return FileResponse(png_path, media_type="image/png")


def _render_page(row: ProjectFile, page: int, dpi: int) -> Path:
    settings.png_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = settings.png_cache_dir / f"{row.sha256[:16]}-p{page}-{dpi}.png"
    if cache_path.exists():
        return cache_path
    source_path = manual_pdf_path(row) if row.kind == "docx" else Path(row.path)
    with fitz.open(source_path) as pdf:
        if page < 1 or page > pdf.page_count:
            raise ApiError(404, f"page {page} out of range (1..{pdf.page_count})", http_status=404)
        pdf_page = pdf[page - 1]
        rect = pdf_page.rect
        scale = min(1.0, settings.png_max_long_edge / max(rect.width, rect.height))
        zoom = (dpi / 72.0) * scale
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pix.save(str(cache_path))
    return cache_path
