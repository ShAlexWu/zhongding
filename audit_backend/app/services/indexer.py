"""File registration and streaming indexing (ijson) for the audit pipeline.

The 23MB api.json (referencedComponents with 16,712 items) is NEVER fully
loaded: only customProperties / pdfExport / sourceSha256 are streamed out
into the SQLite index tables. Rule execution queries the index tables only.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from decimal import Decimal
from pathlib import Path

import ijson
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models import ApiProp, Project, ProjectFile, SpatialComponent, utcnow
from app.schemas.common import ApiError

logger = get_logger(__name__)

# Drawing code = 3 digits + 6 alphanumeric (9 chars), e.g. 000A22G1B.
CODE_RE = re.compile(r"^(\d{3}[0-9A-Z]{6})_")
_BATCH = 500
def _json_safe(value):  # noqa: ANN001
    """Recursively convert Decimal (ijson default) to float for JSON columns."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def infer_code(path: str, kind: str) -> str:
    if kind == "docx":
        return "manual"
    if kind == "trademark":
        return "trademark"
    match = CODE_RE.match(Path(path).name)
    return match.group(1) if match else ""


def sha256_of(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def register_files(
    session: Session, project_id: str, file_inputs: list
) -> list[ProjectFile]:
    """Register file references: existence check + sha256. Missing file -> 4001."""
    rows: list[ProjectFile] = []
    seen: set[str] = set()
    for item in file_inputs:
        # Resolve: absolute path used as-is; bare filename / relative path
        # is looked up under the configured data dir first (OPC local setup,
        # browser cannot provide absolute paths).
        candidate = Path(item.path)
        if not candidate.is_absolute():
            alt = settings.data_dir / item.path
            if alt.is_file():
                candidate = alt
        path = str(candidate.resolve())
        if not Path(path).is_file():
            raise ApiError(4001, f"文件不存在: {item.path}")
        key = f"{item.kind}:{path}"
        if key in seen:
            continue
        seen.add(key)
        digest, size = sha256_of(path)
        code = infer_code(path, item.kind)
        # Anchor guard (SPEC AC-07): a drawing file whose drawing code cannot
        # be extracted would silently fall into the '' bucket, bypassing the
        # sha256 anchor check and overwriting siblings. Reject it explicitly.
        if item.kind in ("api_json", "pdf", "spatial") and not code:
            raise ApiError(
                4002,
                f"图号或视图角色无法识别: {Path(path).name}",
                http_status=400,
            )
        row = ProjectFile(
            id=new_id("f"),
            project_id=project_id,
            kind=item.kind,
            code=code,
            path=path,
            sha256=digest,
            size_bytes=size,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _index_api_json(session: Session, file_row: ProjectFile) -> None:
    """Stream api.json: customProperties + pdfExport + sourceSha256 into ApiProp."""
    path = file_row.path
    with open(path, "rb") as handle:
        for prop in ijson.items(handle, "customProperties.item"):
            session.add(
                ApiProp(
                    file_id=file_row.id,
                    project_id=file_row.project_id,
                    code=file_row.code,
                    prop_name=str(prop.get("name", "")),
                    raw_value=str(prop.get("rawValue", "") or ""),
                    resolved_value=str(prop.get("resolvedValue", "") or ""),
                )
            )
        handle.seek(0)
        export = next(ijson.items(handle, "pdfExport"), {}) or {}
        handle.seek(0)
        source_sha = next(ijson.items(handle, "sourceSha256"), "") or ""
        handle.seek(0)
        title = next(ijson.items(handle, "title"), "") or ""
    meta = {
        "@pdfExport.sha256": str(export.get("sha256", "") or ""),
        "@sourceSha256": str(source_sha),
        "@title": str(title),
    }
    for name, value in meta.items():
        if value:
            session.add(
                ApiProp(
                    file_id=file_row.id,
                    project_id=file_row.project_id,
                    code=file_row.code,
                    prop_name=name,
                    raw_value=value,
                    resolved_value=value,
                )
            )


def _index_spatial(session: Session, file_row: ProjectFile) -> None:
    """Stream spatial.json components into SpatialComponent (batched)."""
    batch: list[SpatialComponent] = []
    with open(file_row.path, "rb") as handle:
        for comp in ijson.items(handle, "components.item"):
            batch.append(
                SpatialComponent(
                    file_id=file_row.id,
                    project_id=file_row.project_id,
                    code=file_row.code,
                    comp_id=str(comp.get("componentId", "") or comp.get("id", "") or ""),
                    name=str(comp.get("name", "") or ""),
                    parent_id=str(comp.get("parentId", "") or ""),
                    path=str(comp.get("path", "") or ""),
                    depth=int(comp.get("depth", 0) or 0),
                    leaf=bool(comp.get("leaf", False)),
                    material_name=str(comp.get("materialName", "") or ""),
                    thicknesses=_json_safe(list(comp.get("sheetMetalThicknesses") or [])),
                    transform_to_root=_json_safe(comp.get("transformToRoot")),
                    bounds_mm=_json_safe(comp.get("assemblyBoundsMm")),
                )
            )
            if len(batch) >= _BATCH:
                session.add_all(batch)
                session.flush()
                batch = []
    if batch:
        session.add_all(batch)


def get_prop(session: Session, file_id: str, prop_name: str) -> str:
    row = (
        session.query(ApiProp)
        .filter_by(file_id=file_id, prop_name=prop_name)
        .one_or_none()
    )
    return row.resolved_value if row else ""


def index_project_files(session: Session, project: Project) -> None:
    """Index all registered files and verify sha256 anchors (SPEC AC-07)."""
    files = session.query(ProjectFile).filter_by(project_id=project.id).all()
    api_files = {f.code: f for f in files if f.kind == "api_json"}
    pdf_files = {f.code: f for f in files if f.kind in ("pdf", "trademark")}
    spatial_files = {f.code: f for f in files if f.kind == "spatial"}

    for row in files:
        try:
            if row.kind == "api_json":
                _index_api_json(session, row)
            elif row.kind == "spatial":
                _index_spatial(session, row)
            row.indexed_at = utcnow()
            session.flush()
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("index failed for %s", row.path)
            raise ApiError(4001, f"文件索引失败: {Path(row.path).name}: {exc}") from exc

    # sha256 anchor: api.json pdfExport.sha256 must match the registered PDF
    for code, api_file in api_files.items():
        declared = get_prop(session, api_file.id, "@pdfExport.sha256")
        pdf_file = pdf_files.get(code)
        if pdf_file and declared and declared != pdf_file.sha256:
            raise ApiError(
                4002,
                f"sha256 不匹配: {code} api.json 与 pdfExport.sha256 不一致",
                http_status=400,
            )
    # cross-anchor: spatial sourceSha256 vs api.json sourceSha256 for same drawing
    for code, spatial_file in spatial_files.items():
        api_file = api_files.get(code)
        if not api_file:
            continue
        spatial_sha = _spatial_source_sha(spatial_file.path)
        api_sha = get_prop(session, api_file.id, "@sourceSha256")
        if spatial_sha and api_sha and spatial_sha != api_sha:
            raise ApiError(
                4002,
                f"sha256 不匹配: {code} spatial.json 与 api.json sourceSha256 不一致",
                http_status=400,
            )


def _spatial_source_sha(path: str) -> str:
    with open(path, "rb") as handle:
        return str(next(ijson.items(handle, "sourceSha256"), "") or "")
