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
