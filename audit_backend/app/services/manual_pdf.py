"""Render the project manual to PDF and locate exact manual evidence."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pymupdf as fitz

from app.core.config import settings
from app.models import ProjectFile

_CONVERT_LOCK = threading.Lock()


def manual_pdf_path(row: ProjectFile) -> Path:
    """Return a cached PDF rendering for a DOCX project file."""
    source = Path(row.path)
    cache_dir = settings.png_cache_dir / "manual-pdf"
    target = cache_dir / f"{row.sha256[:16]}.pdf"
    if target.exists():
        return target

    with _CONVERT_LOCK:  # ponytail: process-local lock; use a file lock only with multi-worker uvicorn
        if target.exists():
            return target
        cache_dir.mkdir(parents=True, exist_ok=True)
        executable = shutil.which("soffice")
        bundled = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if not executable and bundled.is_file():
            executable = str(bundled)
        if not executable:
            raise RuntimeError("未找到 LibreOffice，无法渲染说明书")

        with tempfile.TemporaryDirectory(dir=cache_dir) as temp_dir:
            result = subprocess.run(
                [executable, "--headless", "--convert-to", "pdf", "--outdir", temp_dir, str(source)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            rendered = Path(temp_dir) / f"{source.stem}.pdf"
            if result.returncode != 0 or not rendered.is_file():
                detail = (result.stderr or result.stdout).strip()[-300:]
                raise RuntimeError(f"说明书 PDF 渲染失败：{detail or '未生成文件'}")
            os.replace(rendered, target)
    return target


def anchor_manual_evidence(
    pdf_path: Path,
    file_id: str,
    evidence_groups: list[list[dict]],
) -> list[list[dict]]:
    """Best-effort exact positioning; ambiguous text stays unlocated."""
    with fitz.open(pdf_path) as pdf:
        page_texts = [re.sub(r"\s+", " ", page.get_text()).casefold() for page in pdf]
        return [[_anchor_one(pdf, page_texts, file_id, item) for item in group] for group in evidence_groups]


def _anchor_one(pdf: fitz.Document, page_texts: list[str], file_id: str, item: dict) -> dict:
    if item.get("type") != "doc" or item.get("rect"):
        return item
    for candidate in _candidates(str(item.get("text") or "")):
        probe = re.sub(r"\s+", " ", candidate).casefold()
        pages = [index for index, page_text in enumerate(page_texts) if page_text.count(probe) == 1]
        if len(pages) != 1:
            continue
        page_index = pages[0]
        hits = pdf[page_index].search_for(candidate)
        if not hits:
            continue
        rect = hits[0]
        for hit in hits[1:]:
            rect |= hit
        page_height = pdf[page_index].rect.height
        return {
            **item,
            "file_id": file_id,
            "page": page_index + 1,
            "rect": {
                "x": rect.x0,
                "y": page_height - rect.y1,
                "w": rect.width,
                "h": rect.height,
            },
        }
    return item


def _candidates(text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(
        match.group(0).strip()
        for match in re.finditer(r"\d+(?:\.\d+)\s*mm\s*Thk\.?", text, re.IGNORECASE)
    )
    candidates.extend(
        match.group(0).strip()
        for match in re.finditer(
            r"(?<![\w.])\d[\d,.]*(?:\s*(?:kg\s*/\s*post|kg|kN|N|mm|lbs?|cu\.?\s*m|m[³3]))?",
            text,
            re.IGNORECASE,
        )
        if len(re.sub(r"\D", "", match.group(0))) >= 3
    )
    candidates.extend(
        match.group(0)
        for match in re.finditer(r"(?<![\w.])\d[\d,]*(?![\w.])", text)
        if len(re.sub(r"\D", "", match.group(0))) >= 3
    )
    candidates.extend(
        match.group(0).strip()
        for match in re.finditer(r"\b(?:ISO|JIS|GB/T|DIN|EN|ASTM)\s*[A-Z0-9 ./:-]{3,24}", text, re.IGNORECASE)
    )
    candidates.extend(
        segment.strip(" ：:;；。")
        for segment in re.split(r"[\n；;]", text)
        if len(segment.strip()) >= 12 and re.search(r"[A-Za-z]", segment)
    )
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))
