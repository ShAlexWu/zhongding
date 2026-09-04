"""Focused checks for the hybrid manual-review path."""

from __future__ import annotations

from app.services.doc_parser import ManualDoc, extract_manual
from app.services.handlers.manual import h_man_03, h_man_04, h_man_05, h_man_06, h_man_08
from app.services.rule_engine import ProjectContext, rule_by_key
from app.services.standards_registry import OfficialStandardsRegistry


def test_real_manual_extracts_complete_standard_references(real_docx_path) -> None:  # noqa: ANN001
    refs = extract_manual(str(real_docx_path)).extract_standards()

    assert {
        "ISO 668",
        "ISO 6346",
        "ISO 1161:1984",
        "ISO 1496-1",
        "ISO 830",
        "JIS G 3193:1990",
    } <= set(refs)


def test_standard_registry_fails_withdrawn_versions_without_network() -> None:
    registry = OfficialStandardsRegistry(live=False)

    result = registry.review(["ISO 1161:1984", "ISO 668", "JIS G 3193:1990"])

    assert result.verdict == "fail"
    assert {item.reference: item.verdict for item in result.items} == {
        "ISO 1161:1984": "fail",
        "ISO 668": "warning",
        "JIS G 3193:1990": "fail",
    }
    assert all(item.source_url.startswith("https://") for item in result.items)


def test_man04_returns_one_official_comparison_per_standard() -> None:
    ctx = ProjectContext(project_id="manual")
    ctx.manual = ManualDoc(
        standard_refs=["ISO 1161:1984", "ISO 668", "JIS G 3193:1990"]
    )
    ctx.standards_registry = OfficialStandardsRegistry(live=False)

    outcome = h_man_04(ctx, rule_by_key("MAN-04"))

    assert outcome.verdict == "fail"
    assert len(outcome.evidence) == 3
    assert any("ISO 1161:2016" in item["text"] for item in outcome.evidence)
    assert any(item.get("source_url", "").startswith("https://") for item in outcome.evidence)


def test_man05_uses_real_footer_contact_not_a_standard_number(real_docx_path) -> None:  # noqa: ANN001
    ctx = ProjectContext(project_id="manual")
    ctx.manual = extract_manual(str(real_docx_path))

    outcome = h_man_05(ctx, rule_by_key("MAN-05"))

    assert "jinping.hu@cimc.com" in outcome.evidence[0]["text"]
    assert "+86 769 21667128" in outcome.evidence[0]["text"]
    assert "JIS" not in outcome.evidence[0]["text"]


def test_man03_uses_ai_change_checks_instead_of_keyword_ratio() -> None:
    ctx = ProjectContext(project_id="manual")
    ctx.manual = ManualDoc(full_text="正文")
    ctx.inputs = {"tech_req": "外面漆改为 RAL 9016；质保 5 年"}
    ctx.manual_facts = {
        "change_checks": [
            {
                "requirement": "外面漆改为 RAL 9016",
                "status": "found",
                "evidence": "Exterior color: RAL 9016",
            },
            {
                "requirement": "质保 5 年",
                "status": "missing",
                "evidence": "",
            },
        ]
    }

    outcome = h_man_03(ctx, rule_by_key("MAN-03"))

    assert outcome.verdict == "fail"
    assert "1/2" in outcome.conclusion
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass", "fail"}


def test_man03_reports_explicit_manual_values_instead_of_missing(real_docx_path) -> None:  # noqa: ANN001
    requirements = [
        "1.Fork Pocket Top plate thickness is 4.0 mm",
        "2.Side Walls Inner panel thickness is 2.0 mm",
        "3.Ventilators Quantity is 2 per each side panel",
        "4.The paint suppliers add Gaojing",
        "5.Stacking Testing load is 86,400kg/post",
        "6.Self-adhesive film decal guarantee period is 9 years",
    ]
    ctx = ProjectContext(project_id="manual")
    ctx.manual = extract_manual(str(real_docx_path))
    ctx.inputs = {"tech_req": "；".join(requirements)}
    ctx.manual_facts = {
        "change_checks": [
            {
                "requirement": item,
                "status": "missing" if index < 3 else "uncertain",
                "evidence": "",
            }
            for index, item in enumerate(requirements)
        ]
    }

    outcome = h_man_03(ctx, rule_by_key("MAN-03"))

    assert outcome.verdict == "fail"
    assert all(item["type"] == "doc" for item in outcome.evidence)
    assert all("未在说明书找到证据" not in item["text"] for item in outcome.evidence)
    assert [item["text"] for item in outcome.evidence] == [
        "Top plate : 3.0 mm Thk.",
        "Inner panel : 1.6 mm Thk., Qty. : 3 Pcs/Each side",
        "Quantity: 1 / each side panel",
        "7.2.4 The paint suppliers are Dowill, KCC, Mega, Chugoku, Kansai, KMK, Haoli or Gaojing.",
        "Stacking Testing load: 97,200kg/post",
        "The self-adhesive film decal shall be guaranteed seven (7) years.",
    ]


def test_man03_without_ai_never_passes_on_keyword_overlap() -> None:
    ctx = ProjectContext(project_id="manual")
    ctx.manual = ManualDoc(full_text="外面漆 RAL 9016，质保 5 年")
    ctx.inputs = {"tech_req": "外面漆 RAL 9016，质保 5 年"}

    outcome = h_man_03(ctx, rule_by_key("MAN-03"))

    assert outcome.verdict == "warning"
    assert "AI" in outcome.conclusion


def test_man06_compares_ai_extracted_paint_facts() -> None:
    ctx = ProjectContext(project_id="manual")
    ctx.manual = ManualDoc(full_text="正文")
    ctx.inputs = {"tech_req": "外面漆 RAL 9016，内面漆 RAL 7035"}
    ctx.manual_facts = {
        "requirements": {
            "paint": {
                "exterior": {"ral": "RAL 9016"},
                "interior": {"ral": "RAL 7035"},
            }
        },
        "paint": {
            "exterior": {"ral": "RAL 9016", "evidence": "Exterior RAL 9016"},
            "interior": {"ral": "RAL 7035", "evidence": "Interior RAL 7035"},
        },
    }

    outcome = h_man_06(ctx, rule_by_key("MAN-06"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"外面漆颜色", "内面漆颜色"}


def test_man08_compares_warranty_subject_duration_and_unit() -> None:
    ctx = ProjectContext(project_id="manual")
    ctx.manual = ManualDoc(full_text="正文")
    ctx.inputs = {"tech_req": "原材料质保 5 年"}
    ctx.manual_facts = {
        "requirements": {
            "guarantees": [{"subject": "原材料", "duration": 5, "unit": "年"}]
        },
        "guarantees": [
            {
                "subject": "原材料",
                "duration": 3,
                "unit": "年",
                "evidence": "Raw material guarantee: 3 years",
            }
        ],
    }

    outcome = h_man_08(ctx, rule_by_key("MAN-08"))

    assert outcome.verdict == "fail"
    assert "5年" in outcome.conclusion and "3年" in outcome.conclusion
