from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz

from app.services.annotated_pdf import build_annotated_pdf


def test_complete_pdf_keeps_pages_and_places_problem_annotations(tmp_path: Path) -> None:
    source_path = tmp_path / "drawing.pdf"
    source = fitz.open()
    source.new_page().insert_text((72, 72), "PAGE ONE")
    page = source.new_page()
    page.insert_text((100, 120), "ERROR TEXT")
    source.save(source_path)
    source.close()

    file = SimpleNamespace(id="drawing", kind="pdf", code="000A22G1G", path=str(source_path))
    result = SimpleNamespace(
        rule_key="TEST-01",
        title="Test problem",
        verdict="fail",
        conclusion="Value is incorrect",
        evidence=[{
            "type": "pdf",
            "file_id": "drawing",
            "page": 2,
            "rect": {"x": 98, "y": 710, "w": 70, "h": 16},
            "text": "ERROR TEXT",
        }],
    )

    exported = fitz.open(stream=build_annotated_pdf("demo", [file], [result]), filetype="pdf")
    assert exported.page_count == 3  # summary + both complete source pages
    assert "PAGE ONE" in exported[1].get_text()
    assert "ERROR TEXT" in exported[2].get_text()
    exported_page = exported[2]
    annotations = list(exported_page.annots() or [])
    assert {item.type[1] for item in annotations} == {"Square", "Text"}
    assert any("TEST-01" in (item.info.get("content") or "") for item in annotations)
    assert exported.get_toc()[1][1] == "drawing"
    exported.close()


def test_summary_groups_problems_and_shows_counts_and_locations(tmp_path: Path) -> None:
    source_path = tmp_path / "drawing.pdf"
    source = fitz.open()
    source.new_page().insert_text((72, 72), "DRAWING")
    source.save(source_path)
    source.close()

    file = SimpleNamespace(
        id="drawing", kind="pdf", code="000A22G1G", path=str(source_path)
    )

    def result(**values):
        defaults = {
            "rule_key": "TEST-01",
            "domain": "总图",
            "title": "Test item",
            "priority": "P1",
            "verdict": "pass",
            "conclusion": "No problem",
            "evidence": [],
        }
        return SimpleNamespace(**(defaults | values))

    results = [
        result(rule_key="PASS-01"),
        result(rule_key="SKIP-01", verdict="skipped"),
        result(
            rule_key="TOT-01",
            domain="总图",
            priority="P0",
            verdict="fail",
            title="尺寸不一致",
            conclusion="总图尺寸与说明书不一致",
            evidence=[{
                "type": "pdf",
                "file_id": "drawing",
                "page": 1,
                "rect": {"x": 70, "y": 760, "w": 60, "h": 16},
                "text": "DRAWING",
            }],
        ),
        result(
            rule_key="MAN-02",
            domain="说明书",
            verdict="warning",
            title="Revision list 待确认",
            conclusion="未找到可核验行",
        ),
    ]

    exported = fitz.open(
        stream=build_annotated_pdf("示例项目", [file], results), filetype="pdf"
    )
    page = exported[0]
    text = page.get_text()
    words = page.get_text("words")
    for label in ("通过", "未通过", "待确认", "已跳过"):
        label_word = next(word for word in words if word[4] == label)
        assert any(
            word[4] == "1"
            and label_word[0] <= (word[0] + word[2]) / 2 <= label_word[0] + 80
            and label_word[1] < word[1] < label_word[1] + 30
            for word in words
        )
    assert "PASS-01" not in text
    assert "SKIP-01" not in text
    assert text.index("说明书") < text.index("总图")
    assert "总图 P1" in text
    assert "未定位" in text
    exported.close()


def test_summary_paginates_without_splitting_problem_rows() -> None:
    results = [
        SimpleNamespace(
            rule_key=f"MAN-{index:02d}",
            domain="说明书",
            title=f"第 {index} 项检查",
            priority="P0" if index % 2 else "P1",
            verdict="warning",
            conclusion="这是一段用于验证长清单自动分页和内容截断的判定结论。" * 3,
            evidence=[],
        )
        for index in range(1, 31)
    ]

    exported = fitz.open(
        stream=build_annotated_pdf("分页测试", [], results), filetype="pdf"
    )
    assert exported.page_count > 1
    texts = [page.get_text() for page in exported]
    assert all("智能审图问题清单" in text for text in texts)
    assert all("序号" in text and "证据定位" in text for text in texts)
    assert all(f"问题清单 {index} / {len(texts)}" in text for index, text in enumerate(texts, 1))
    combined = "\n".join(texts)
    for index in range(1, 31):
        assert combined.count(f"MAN-{index:02d}") == 1
    exported.close()


def test_summary_shows_empty_problem_state() -> None:
    result = SimpleNamespace(
        rule_key="TOT-01",
        domain="总图",
        title="尺寸检查",
        priority="P0",
        verdict="pass",
        conclusion="尺寸一致",
        evidence=[],
    )

    exported = fitz.open(
        stream=build_annotated_pdf("无问题项目", [], [result]), filetype="pdf"
    )
    assert exported.page_count == 1
    text = exported[0].get_text()
    assert "未发现需处理的问题" in text
    assert "TOT-01" not in text
    exported.close()
