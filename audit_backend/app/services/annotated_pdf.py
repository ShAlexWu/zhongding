"""Build one complete, annotated PDF for a finished review task."""

from __future__ import annotations

import textwrap
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf as fitz

from app.models import ProjectFile, RuleResult
from app.services.manual_pdf import anchor_manual_evidence, manual_pdf_path

PROBLEM_VERDICTS = {"fail", "warning", "error"}
RED = (0.85, 0.12, 0.12)
ORANGE = (0.95, 0.55, 0.08)
BLUE = (0.15, 0.39, 0.92)
GREEN = (0.08, 0.50, 0.24)
DARK = (0.10, 0.12, 0.15)
TEXT = (0.24, 0.28, 0.33)
MUTED = (0.42, 0.45, 0.50)
LINE = (0.88, 0.90, 0.93)
PALE_BLUE = (0.92, 0.96, 1.00)
PALE_RED = (1.00, 0.93, 0.93)
PALE_ORANGE = (1.00, 0.96, 0.90)
PALE_GREEN = (0.92, 0.98, 0.94)
PALE_GRAY = (0.95, 0.96, 0.97)
DOMAIN_ORDER = (
    "全局通用", "说明书", "总图", "门端图", "侧板图",
    "前端图", "底架图", "顶板图", "商标图",
)
VERDICT_LABELS = {
    "pass": "通过",
    "fail": "未通过",
    "warning": "待确认",
    "skipped": "已跳过",
    "error": "执行错误",
}
VERDICT_STYLES = {
    "pass": GREEN,
    "fail": RED,
    "warning": (0.70, 0.33, 0.04),
    "skipped": MUTED,
    "error": RED,
}
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
    _append_summary(output, project_name, results, problems, sources)
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
    results: list[RuleResult],
    problems: list[tuple[RuleResult, list[dict]]],
    sources: list[tuple[ProjectFile, Path]],
) -> None:
    labels = {row.id: _source_label(row) for row, _ in sources}
    counts = Counter(str(getattr(row, "verdict", "")) for row in results)
    domain_rank = {name: index for index, name in enumerate(DOMAIN_ORDER)}
    priority_rank = {"P0": 0, "P1": 1}
    verdict_rank = {"error": 0, "fail": 1, "warning": 2}
    ordered = sorted(
        problems,
        key=lambda item: (
            domain_rank.get(str(getattr(item[0], "domain", "")), 99),
            priority_rank.get(str(getattr(item[0], "priority", "P1")), 9),
            verdict_rank.get(str(getattr(item[0], "verdict", "warning")), 9),
            str(item[0].rule_key),
        ),
    )

    summary_pages: list[int] = []
    page, font, y = _new_summary_page(output, project_name, counts, first=True)
    summary_pages.append(page.number)
    current_domain = ""
    row_number = 0

    if not ordered:
        page.insert_text(
            (36, y + 50), "未发现需处理的问题", fontname=font,
            fontsize=14, color=GREEN,
        )

    for row, evidence in ordered:
        domain = str(getattr(row, "domain", "其他") or "其他")
        if domain != current_domain:
            if y + 64 > 806:
                page, font, y = _new_summary_page(output, project_name, counts, first=False)
                summary_pages.append(page.number)
            y = _draw_domain_header(page, font, y, domain)
            current_domain = domain
        if y + 44 > 806:
            page, font, y = _new_summary_page(output, project_name, counts, first=False)
            summary_pages.append(page.number)
            y = _draw_domain_header(page, font, y, domain)
        row_number += 1
        y = _draw_problem_row(
            page, font, y, row_number, row, _evidence_location(evidence, labels)
        )

    total_pages = len(summary_pages)
    for index, page_number in enumerate(summary_pages, start=1):
        page = output[page_number]
        font = _summary_font(page)
        page.draw_line((36, 814), (559, 814), color=LINE, width=0.6)
        page.insert_text(
            (36, 829), "仅列未通过、待确认和执行错误",
            fontname=font, fontsize=7, color=MUTED,
        )
        page.insert_text(
            (505, 829), f"问题清单 {index} / {total_pages}",
            fontname=font, fontsize=7, color=MUTED,
        )


def _new_summary_page(
    output: fitz.Document,
    project_name: str,
    counts: Counter,
    *,
    first: bool,
) -> tuple[fitz.Page, str, float]:
    page = output.new_page(width=595, height=842)
    font = _summary_font(page)
    page.draw_line((36, 30), (559, 30), color=BLUE, width=2)
    page.insert_text(
        (36, 54 if first else 50),
        "智能审图问题清单" if first else "智能审图问题清单（续）",
        fontname=font, fontsize=18 if first else 13, color=DARK,
    )
    page.insert_text(
        (36, 72 if first else 66), f"项目：{project_name}",
        fontname=font, fontsize=8, color=MUTED,
    )
    if first:
        stats = (
            ("通过", counts["pass"], GREEN, PALE_GREEN),
            ("未通过", counts["fail"], RED, PALE_RED),
            ("待确认", counts["warning"], (0.70, 0.33, 0.04), PALE_ORANGE),
            ("已跳过", counts["skipped"], MUTED, PALE_GRAY),
        )
        for index, (label, value, color, fill) in enumerate(stats):
            x = 36 + index * 132
            page.draw_rect(fitz.Rect(x, 86, x + 123, 124), color=LINE, fill=fill, width=0.5)
            page.draw_rect(fitz.Rect(x, 86, x + 4, 124), color=color, fill=color, width=0)
            page.insert_text((x + 13, 102), label, fontname=font, fontsize=8, color=color)
            page.insert_text((x + 13, 118), str(value), fontname=font, fontsize=15, color=color)
        y = 140.0
    else:
        y = 78.0
    return page, font, _draw_table_header(page, font, y)


def _draw_table_header(page: fitz.Page, font: str, y: float) -> float:
    columns = (
        (36, 60, "序号"), (60, 115, "规则"), (115, 363, "问题描述"),
        (363, 401, "优先级"), (401, 459, "状态"), (459, 559, "证据定位"),
    )
    page.draw_rect(fitz.Rect(36, y, 559, y + 24), color=LINE, fill=PALE_GRAY, width=0.6)
    for x0, x1, label in columns:
        if x0 > 36:
            page.draw_line((x0, y), (x0, y + 24), color=LINE, width=0.6)
        page.insert_text((x0 + 5, y + 16), label, fontname=font, fontsize=7.5, color=TEXT)
    return y + 24


def _draw_domain_header(page: fitz.Page, font: str, y: float, domain: str) -> float:
    page.draw_rect(fitz.Rect(36, y, 559, y + 20), color=LINE, fill=PALE_BLUE, width=0.5)
    page.insert_text((43, y + 14), domain, fontname=font, fontsize=8.5, color=BLUE)
    return y + 20


def _draw_problem_row(
    page: fitz.Page,
    font: str,
    y: float,
    number: int,
    row: RuleResult,
    location: str,
) -> float:
    height = 44.0
    page.draw_rect(fitz.Rect(36, y, 559, y + height), color=LINE, fill=(1, 1, 1), width=0.5)
    for x in (60, 115, 363, 401, 459):
        page.draw_line((x, y), (x, y + height), color=LINE, width=0.5)

    verdict = str(getattr(row, "verdict", "warning"))
    status_color = VERDICT_STYLES.get(verdict, MUTED)
    page.insert_text((406, y + 20), VERDICT_LABELS.get(verdict, verdict), fontname=font, fontsize=7, color=status_color)

    page.insert_text((43, y + 17), str(number), fontname=font, fontsize=7.5, color=TEXT)
    page.insert_text((66, y + 17), str(row.rule_key), fontname=font, fontsize=7.5, color=DARK)
    page.insert_text((370, y + 17), str(getattr(row, "priority", "P1")), fontname=font, fontsize=7.5, color=DARK)

    title = _shorten(str(getattr(row, "title", "")), 28)
    conclusion = str(getattr(row, "conclusion", ""))
    detail_lines = textwrap.wrap(conclusion, width=30, break_long_words=True)[:2]
    if len(textwrap.wrap(conclusion, width=30, break_long_words=True)) > 2 and detail_lines:
        detail_lines[-1] = _shorten(detail_lines[-1], 29)
    page.insert_text((121, y + 14), title, fontname=font, fontsize=7.5, color=DARK)
    for index, line in enumerate(detail_lines):
        page.insert_text((121, y + 27 + index * 10), line, fontname=font, fontsize=6.8, color=MUTED)

    for index, line in enumerate(textwrap.wrap(location, width=12, break_long_words=True)[:2]):
        page.insert_text((465, y + 16 + index * 11), line, fontname=font, fontsize=7, color=TEXT)
    return y + height


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


def _source_label(row: ProjectFile) -> str:
    if row.kind == "docx":
        return "说明书"
    return {
        "000A22G1G": "总图",
        "000A22G1E": "门端图",
        "000A22G1S": "侧板图",
        "000A22G1F": "前端图",
        "000A22G1B": "底架图",
        "000A22G1R": "顶板图",
        "trademark": "商标图",
    }.get(str(getattr(row, "code", "")), Path(row.path).stem)


def _evidence_location(evidence: list[dict], labels: dict[str, str]) -> str:
    locations: list[str] = []
    for item in evidence:
        file_id = str(item.get("file_id") or "")
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        if file_id not in labels or page < 1:
            continue
        location = f"{labels[file_id]} P{page}"
        if location not in locations:
            locations.append(location)
    if not locations:
        return "未定位"
    return " / ".join(locations[:2]) + (" 等" if len(locations) > 2 else "")


def _summary_font(page: fitz.Page) -> str:
    font_file = next((path for path in SUMMARY_FONTS if path.is_file()), None)
    if font_file:
        page.insert_font(fontname="audit", fontfile=font_file)
        return "audit"
    return "china-s"
