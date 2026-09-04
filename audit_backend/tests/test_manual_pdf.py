from __future__ import annotations

import hashlib

from app.models import ProjectFile
from app.services.manual_pdf import anchor_manual_evidence, manual_pdf_path


def test_manual_renders_and_evidence_has_exact_page_rect(real_docx_path) -> None:  # noqa: ANN001
    row = ProjectFile(
        id="manual-test",
        project_id="project-test",
        kind="docx",
        path=str(real_docx_path),
        sha256=hashlib.sha256(real_docx_path.read_bytes()).hexdigest(),
        size_bytes=real_docx_path.stat().st_size,
    )
    groups = anchor_manual_evidence(
        manual_pdf_path(row),
        row.id,
        [[
            {"type": "doc", "text": "Top plate : 3.0 mm Thk."},
            {"type": "doc", "text": "Inner panel : 1.6 mm Thk., Qty. : 3 Pcs/Each side"},
            {"type": "doc", "text": "说明书 9.3：97,200 kg/post × 4 ÷ 1.8 = 216,000 kg"},
            {"type": "doc", "text": "说明书 9.3：横向刚性试验力 15,240 kg（150 kN）= 150,000 N"},
        ]],
    )

    assert [(item["file_id"], item["page"]) for item in groups[0]] == [
        ("manual-test", 6),
        ("manual-test", 10),
        ("manual-test", 14),
        ("manual-test", 15),
    ]
    assert all(item["rect"]["w"] > 0 and item["rect"]["h"] > 0 for item in groups[0])
