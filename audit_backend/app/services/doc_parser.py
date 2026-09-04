"""Manual (docx) and drawing (pdf) text extraction.

- DOCX: XML-level cell iteration (handles merged cells, SPEC section 11 pit 10)
- PDF: PyMuPDF text layer, per-page, used by pdf-text selectors (e.g. CHAS-01)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import ijson
import pymupdf as fitz
from docx import Document

from app.services.standards_registry import normalize_standard_reference

_RATING_PATTERNS = {
    "max_gross_kg": r"(?:MAXIMUM\s+GROSS\s+WEIGHT|最大总重|总重).{0,80}?([\d,]+)\s*KGS?",
    "payload_kg": r"(?:MAXIMUM\s+PAYLOAD|最大载重|载重).{0,80}?([\d,]+)\s*KGS?",
    "tare_weight_kg": r"(?:TARE\s+WEIGHT|净重|自重).{0,80}?([\d,]+)\s*KGS?",
    "stacking_test_kg": r"(?:STACKING|堆码).{0,80}?(?:TEST(?:ING)?\s+LOAD\s*:?\s*)?([\d,]+)\s*KGS?",
    "floor_strength_kg": r"(?:FLOOR\s+STRENGTH|地板强度).{0,80}?([\d,]+)\s*KGS?",
}
_VERSION_RE = re.compile(r"\b(\d{2}[A-Z]-?\d{2})\b")
_STANDARD_RE = re.compile(
    r"\b(?:ISO|JIS\s+STANDARD|JIS|GB/T|GB|DIN|EN|ASTM|IEC)"
    r"\s*[/ -]?\s*[A-Z]?\s*\d+(?:\s*[/ -]\s*\d+)?(?:\s*:\s*\d{4})?",
    re.IGNORECASE,
)
_DIM_KEYS = {
    "external_L": r"6,?058",
    "external_W": r"2,?438",
    "external_H": r"2,?591",
    "internal_L": r"5,?898",
    "internal_W": r"2,?352",
    "internal_H": r"2,?393",
    "cubic_capacity": r"33\.2",
}


@dataclass
class ManualDoc:
    path: str = ""
    paragraphs: list[str] = field(default_factory=list)
    table_cells: list[str] = field(default_factory=list)
    footer_text: str = ""
    full_text: str = ""
    standard_refs: list[str] = field(default_factory=list)

    def extract_ratings(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, pattern in _RATING_PATTERNS.items():
            match = re.search(pattern, self.full_text, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            try:
                out[key] = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
        stack_high = re.search(r"(?:EIGHT\s*\(8\)|\b8\b)\s+HIGH\s+STACKED", self.full_text, re.IGNORECASE)
        if stack_high:
            out["stack_high"] = 8.0
        return out

    def extract_testing_values(self) -> dict[str, float]:
        stacking = re.search(
            r"\bStacking\b.{0,160}?Testing\s+load\s*:\s*([\d,]+)\s*kg\s*/\s*post",
            self.full_text,
            re.IGNORECASE | re.DOTALL,
        )
        transverse = re.search(
            r"Rigidity\s*\(\s*Transverse\s*\).{0,120}?Test\s+Force\s*:\s*"
            r"([\d,]+)\s*kg\s*\(\s*([\d,.]+)\s*kn\s*\)",
            self.full_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not stacking or not transverse:
            return {}
        stacking_kg = float(stacking.group(1).replace(",", ""))
        transverse_kg = float(transverse.group(1).replace(",", ""))
        transverse_kn = float(transverse.group(2).replace(",", ""))
        return {
            "stacking_test_kg_per_post": stacking_kg,
            "allowable_stacking_load_1_8g_kg": stacking_kg * 4 / 1.8,
            "transverse_racking_test_force_kg": transverse_kg,
            "transverse_racking_test_force_kn": transverse_kn,
            "transverse_racking_test_force_n": transverse_kn * 1000,
        }

    def extract_dimensions(self) -> dict[str, str]:
        # Line-based extraction: a fixed +/-8 char window around the match can
        # bleed the previous dimension's tolerance "(0, -6)" into the value
        # context, which corrupts the first-number parse downstream (TOT-01).
        out: dict[str, str] = {}
        lines = re.split(r"[\r\n]+", self.full_text)
        for key, pattern in _DIM_KEYS.items():
            for line in lines:
                if re.search(pattern, line):
                    out[key] = line.strip()
                    break
        return out

    def extract_versions(self) -> list[str]:
        return list(dict.fromkeys(_VERSION_RE.findall(self.full_text)))

    def extract_standards(self) -> list[str]:
        found = [normalize_standard_reference(value) for value in self.standard_refs]
        found.extend(
            normalize_standard_reference(match.group(0))
            for match in _STANDARD_RE.finditer(self.full_text)
        )
        return list(dict.fromkeys(value for value in found if value))

    def extract_colors(self) -> list[str]:
        lines: list[str] = []
        for line in re.split(r"[\r\n]+", self.full_text):
            if re.search(r"颜色|油漆|涂|\bRAL\s*\d{4}\b", line, re.IGNORECASE):
                lines.append(line.strip())
        return lines[:12]

    def extract_guarantee(self) -> str:
        match = re.search(
            r".{0,40}(质保|保修|GUARANTEE).{0,40}?(\d+)\s*(年|月|years|months)",
            self.full_text,
            re.IGNORECASE | re.DOTALL,
        )
        return match.group(0).strip() if match else ""

    def extract_revision_rows(self) -> list[str]:
        rows: list[str] = []
        for cell in self.table_cells:
            if re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", cell) and re.search(
                r"(改|REV|版本|说明|内容)", cell, re.IGNORECASE
            ):
                rows.append(cell.strip())
        return rows[:20]

    def find_around(self, keyword: str, width: int = 60) -> str:
        idx = self.full_text.find(keyword)
        if idx == -1:
            return ""
        start = max(0, idx - width)
        return self.full_text[start : idx + width].strip()


def extract_manual(path: str) -> ManualDoc:
    doc = Document(path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    table_cells: list[str] = []
    standard_refs: list[str] = []
    for table in doc.tables:
        for row in table.rows:
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            if len(cells) >= 2 and re.fullmatch(r"\d{3,5}(?::\d{4})?(?:-\d+)?", cells[0]):
                description = " ".join(cells[1:]).lower()
                if "freight container" in description or "series 1" in description:
                    standard_refs.append(f"ISO {cells[0]}")
        for tc in table._tbl.iter():  # XML-level: robust against merged cells
            if tc.tag.endswith("}tc"):
                text = "".join(t.text or "" for t in tc.iter() if t.tag.endswith("}t"))
                if text.strip():
                    table_cells.append(text.strip())
    footer_text = ""
    try:
        for section in doc.sections:
            for para in section.footer.paragraphs:
                footer_text += para.text + "\n"
            for table in section.footer.tables:
                for row in table.rows:
                    for cell in row.cells:
                        text = " ".join(cell.text.split())
                        if text:
                            footer_text += text + "\n"
    except Exception:  # noqa: BLE001 - footers are best-effort
        footer_text = ""
    full_text = "\n".join(paragraphs + table_cells + [footer_text])
    return ManualDoc(
        path=path,
        paragraphs=paragraphs,
        table_cells=table_cells,
        footer_text=footer_text.strip(),
        full_text=full_text,
        standard_refs=list(dict.fromkeys(standard_refs)),
    )


def extract_pdf_texts(path: str, max_pages: int = 40) -> list[str]:
    """Per-page text layer; returns list of page texts (1-based index)."""
    texts: list[str] = []
    with fitz.open(path) as pdf:
        page_count = pdf.page_count
        for page in pdf.pages(0, min(page_count, max_pages)):
            texts.append(page.get_text("text") or "")
    return texts


def pdf_keyword_context(texts: list[str], keywords: list[str], width: int = 80) -> list[dict]:
    """Find keyword context across pages -> [{page, text}]."""
    results: list[dict] = []
    for idx, text in enumerate(texts, start=1):
        for kw in keywords:
            pos = text.find(kw)
            if pos != -1:
                start = max(0, pos - width // 2)
                results.append({"page": idx, "text": text[start : pos + width].strip()})
    return results


def extract_drawing_facts(path: str) -> list[dict]:
    """Stream notes, weld symbols, dimensions and BOM rows from a SolidWorks api.json."""
    facts: list[dict] = []
    with open(path, "rb") as handle:
        for page, sheet in enumerate(ijson.items(handle, "sheets.item"), start=1):
            width = float(sheet.get("widthMeters") or 0)
            height = float(sheet.get("heightMeters") or 0)
            for view in sheet.get("views") or []:
                common = {
                    "page": page,
                    "sheet": str(sheet.get("name") or ""),
                    "view": str(view.get("name") or ""),
                }
                for note in view.get("notes") or []:
                    facts.append(_drawing_fact("note", note, common, width, height))
                for weld in view.get("weldSymbols") or []:
                    facts.append(_drawing_fact("weld", weld, common, width, height))
                for dimension in view.get("dimensions") or []:
                    facts.append(_drawing_fact("dimension", dimension, common, width, height))
                for table in view.get("tables") or []:
                    if table.get("typeName") != "swTableAnnotation_BillOfMaterials":
                        continue
                    for row in table.get("cells") or []:
                        cells = {
                            int(cell.get("column", -1)): str(
                                cell.get("displayedText") or cell.get("rawText") or ""
                            ).strip()
                            for cell in row
                        }
                        if not cells.get(0, "").isdigit():
                            continue
                        position = [float(value) for value in (table.get("position") or [])[:2]]
                        facts.append(
                            {
                                **common,
                                "kind": "bom",
                                "name": cells[0],
                                "text": (
                                    f"BOM 序号 {cells[0]}；图号 {cells.get(2, '')}；"
                                    f"名称 {cells.get(1, '')}；厚度/规格 {cells.get(4, '')}"
                                ),
                                "position": position,
                                "rect": _position_rect(position, width, height),
                                "attachments": [],
                                "leaders": 0,
                                "attached": False,
                                "values": [],
                                "item": cells[0],
                                "description": cells.get(1, ""),
                                "drawing_no": cells.get(2, ""),
                                "material": cells.get(3, ""),
                                "specification": cells.get(4, ""),
                                "quantity": cells.get(5, ""),
                            }
                        )
    return facts


def _drawing_fact(kind: str, raw: dict, common: dict, width: float, height: float) -> dict:
    attachments = [
        {
            "component_name": str(item.get("componentName") or ""),
            "drawing_component_name": str(item.get("drawingComponentName") or ""),
            "geometry_type": str(item.get("geometryType") or ""),
            "dangling": bool(item.get("dangling", False)),
            "persistent_status": str(item.get("persistentReferenceStatus") or ""),
        }
        for item in raw.get("attachments") or []
    ]
    if kind == "note":
        text = str(raw.get("text") or raw.get("propertyLinkedText") or "")
        attached = bool(attachments) and not any(a["dangling"] for a in attachments)
        values: list[float] = []
    elif kind == "weld":
        text = " ".join(str(value) for value in raw.get("texts") or [] if value)
        attached = bool(raw.get("attached")) and not any(a["dangling"] for a in attachments)
        values = []
    else:
        fields = ("textAll", "prefix", "suffix", "calloutAbove", "calloutBelow", "lowerText")
        text = " ".join(str(raw.get(name) or "") for name in fields).strip()
        values = []
        for value in raw.get("value") or []:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        attached = bool(attachments) and not any(a["dangling"] for a in attachments)
    position = [float(v) for v in (raw.get("position") or [])[:2]]
    return {
        **common,
        "kind": kind,
        "name": str(raw.get("annotationName") or ""),
        "text": text,
        "position": position,
        "rect": _position_rect(position, width, height),
        "attachments": attachments,
        "leaders": len(raw.get("leaders") or []),
        "leader_point_counts": [len(item.get("points") or []) for item in raw.get("leaders") or []],
        "raw_attached": bool(raw.get("attached")) if kind == "weld" else attached,
        "attached": attached,
        "values": values,
    }


def _position_rect(position: list[float], width: float, height: float) -> dict | None:
    """Approximate annotation point as a small PDF-space rectangle."""
    if len(position) < 2 or width <= 0 or height <= 0:
        return None
    points_per_meter = 72 / 0.0254
    x = position[0] * points_per_meter
    y = position[1] * points_per_meter
    page_w = width * points_per_meter
    page_h = height * points_per_meter
    return {
        "x": round(max(0.0, min(page_w - 28.0, x - 14.0)), 2),
        "y": round(max(0.0, min(page_h - 16.0, y - 8.0)), 2),
        "w": 28.0,
        "h": 16.0,
    }
