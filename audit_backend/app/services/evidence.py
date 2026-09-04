"""Evidence anchor assembly (original text + page + rect + file_id)."""

from __future__ import annotations

from typing import Any


def doc_evidence(text: str, side: str = "manual") -> dict:
    """Manual (docx) evidence anchor."""
    return {"type": "doc", "text": text[:400], "side": side}


def json_evidence(file_id: str, text: str, prop_name: str = "") -> dict:
    """api.json / spatial.json field value evidence anchor."""
    item: dict[str, Any] = {"type": "json", "file_id": file_id, "text": str(text)[:400]}
    if prop_name:
        item["prop_name"] = prop_name
    return item


def pdf_evidence(
    file_id: str,
    page: int,
    text: str,
    rect: dict | None = None,
    side: str | None = None,
) -> dict:
    """Drawing page evidence anchor; rect uses PDF-native coordinates."""
    item: dict[str, Any] = {
        "type": "pdf",
        "file_id": file_id,
        "page": page,
        "rect": rect,
        "text": str(text)[:400],
    }
    if side:
        item["side"] = side
    return item


def input_evidence(text: str) -> dict:
    """Customer input evidence anchor."""
    return {"type": "input", "text": str(text)[:400]}


def both_sides(manual_text: str, drawing_text: str, drawing_file_id: str) -> list[dict]:
    """Consistency rules: manual value X vs drawing value Y (SPEC AC-03)."""
    return [
        doc_evidence(manual_text, side="manual"),
        json_evidence(drawing_file_id, drawing_text, side="drawing"),
    ]
