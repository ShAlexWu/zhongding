"""Build one complete, annotated PDF for a finished review task."""

from __future__ import annotations

import textwrap
from collections import defaultdict
from pathlib import Path

import pymupdf as fitz

from app.models import ProjectFile, RuleResult
from app.services.manual_pdf import anchor_manual_evidence, manual_pdf_path

PROBLEM_VERDICTS = {"fail", "warning", "error"}
RED = (0.85, 0.12, 0.12)
ORANGE = (0.95, 0.55, 0.08)
SUMMARY_FONTS = (
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)


def build_annotated_pdf(
    project_name: str,
    files: list[ProjectFile],
    results: list[RuleResult],
) -> bytes:
    """Merge every source PDF and attach each problem to its located evidence."""
    sources = _source_pdfs(files)
    evidence_by_rule = [list(row.evidence or []) for row in results]
    manual = next((item for item in sources if item[0].kind == "docx"), None)
    if manual:
        evidence_by_rule = anchor_manual_evidence(manual[1], manual[0].id, evidence_by_rule)

    problems = [
        (row, _problem_evidence(row, evidence))
        for row, evidence in zip(results, evidence_by_rule, strict=True)
        if row.verdict in PROBLEM_VERDICTS
    ]
    locations: dict[tuple, list[tuple[RuleResult, dict]]] = defaultdict(list)
    for row, evidence in problems:
        for item in evidence:
            key = _location_key(item)
            if key:
                locations[key].append((row, item))

    output = fitz.open()
    _append_summary(output, project_name, problems, locations)
    toc = [[1, "审图问题汇总", 1]]
    for row, path in sources:
        with fitz.open(path) as source:
            first_page = output.page_count
            output.insert_pdf(source)
        toc.append([1, "说明书" if row.kind == "docx" else Path(row.path).stem, first_page + 1])
        for key, entries in locations.items():
            file_id, page_number, x, y, width, height = key
            if file_id != row.id or not 1 <= page_number <= output.page_count - first_page:
                continue
            page = output[first_page + page_number - 1]
            rect = _fitz_rect(page, x, y, width, height)
            if not rect:
                continue
            color = RED if any(item[0].verdict in {"fail", "error"} for item in entries) else ORANGE
            box = page.add_rect_annot(rect)
            box.set_colors(stroke=color)
            box.set_border(width=1.5)
            box.update()
            note = page.add_text_annot(_note_point(page, rect), _annotation_text(entries), icon="Comment")
            note.set_info(title="智能审图", subject="错误批注")
            note.set_colors(stroke=color)
            note.update()
    output.set_toc(toc)
    output.subset_fonts()
    data = output.tobytes(garbage=4, deflate=True)
    output.close()
    return data


def _source_pdfs(files: list[ProjectFile]) -> list[tuple[ProjectFile, Path]]:
    order = {"manual": 0, "000A22G1G": 1, "000A22G1E": 2, "000A22G1S": 3,
             "000A22G1F": 4, "000A22G1B": 5, "000A22G1R": 6, "trademark": 7}
    rows = [row for row in files if row.kind in {"docx", "pdf", "trademark"}]
    rows.sort(key=lambda row: (order.get(row.code, 99), row.code, row.path))
    return [
        (row, manual_pdf_path(row) if row.kind == "docx" else Path(row.path))
        for row in rows
        if Path(row.path).is_file()
    ]


def _problem_evidence(row: RuleResult, evidence: list[dict]) -> list[dict]:
    if any(item.get("checkpoint_verdict") in PROBLEM_VERDICTS for item in evidence):
        return [item for item in evidence if item.get("checkpoint_verdict") in PROBLEM_VERDICTS]
    if row.rule_key == "GEN-01":
        return [item for item in evidence if "图中序号" in str(item.get("text") or "")]
    if row.rule_key in {"GEN-02", "GEN-03"}:
        return [
            item for item in evidence
            if any(flag in str(item.get("text") or "") for flag in ("状态=缺失", "状态=绑定无效"))
        ]
    return evidence


def _location_key(item: dict) -> tuple | None:
    rect = item.get("rect")
    try:
        values = tuple(round(float(rect[name]), 2) for name in ("x", "y", "w", "h"))
        page = int(item.get("page"))
    except (KeyError, TypeError, ValueError):
        return None
    if not item.get("file_id") or page < 1 or values[2] <= 0 or values[3] <= 0:
        return None
    return (item["file_id"], page, *values)


def _fitz_rect(page: fitz.Page, x: float, y: float, width: float, height: float) -> fitz.Rect | None:
    media_height = page.mediabox.height
    rect = fitz.Rect(x, media_height - y - height, x + width, media_height - y)
    rect &= page.rect
    return rect if rect.width > 0 and rect.height > 0 else None


def _note_point(page: fitz.Page, rect: fitz.Rect) -> fitz.Point:
    x = rect.x1 + 3 if rect.x1 + 23 < page.rect.x1 else max(page.rect.x0, rect.x0 - 20)
    return fitz.Point(x, max(page.rect.y0, min(rect.y0, page.rect.y1 - 20)))


def _annotation_text(entries: list[tuple[RuleResult, dict]]) -> str:
    messages: list[str] = []
    seen: set[str] = set()
    for row, item in entries:
        message = f"[{row.rule_key}] {row.title}\n结论：{row.conclusion}\n证据：{item.get('text') or '未提供'}"
        if message not in seen:
            messages.append(message)
            seen.add(message)
    return "\n\n".join(messages)


def _append_summary(
    output: fitz.Document,
    project_name: str,
    problems: list[tuple[RuleResult, list[dict]]],
    locations: dict[tuple, list[tuple[RuleResult, dict]]],
) -> None:
    located_rules = {row.rule_key for entries in locations.values() for row, _ in entries}
    lines = [f"审图批注汇总 - {project_name}", "红色=未通过/执行错误，橙色=待复核；批注图标位于对应证据旁。", ""]
    for row, _ in problems:
        marker = "已定位" if row.rule_key in located_rules else "未定位"
        text = f"[{row.verdict.upper()}] {row.rule_key} {row.title} ({marker})：{row.conclusion}"
        lines.extend(textwrap.wrap(text, width=58, break_long_words=True) or [text])
        lines.append("")

    page = None
    y = 45.0
    for index, line in enumerate(lines):
        if page is None or y > 825:
            page = output.new_page(width=595, height=842)
            font = _summary_font(page)
            y = 45.0
        size = 16 if index == 0 else 9
        color = (0.1, 0.1, 0.1) if index < 2 else (0.25, 0.25, 0.25)
        page.insert_text((40, y), line, fontname=font, fontsize=size, color=color)
        y += 24 if index == 0 else 14


def _summary_font(page: fitz.Page) -> str:
    font_file = next((path for path in SUMMARY_FONTS if path.is_file()), None)
    if font_file:
        page.insert_font(fontname="audit", fontfile=font_file)
        return "audit"
    return "china-s"
