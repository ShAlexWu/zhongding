from __future__ import annotations

import asyncio

from app.services.doc_parser import ManualDoc
from app.services.fact_layer import FactLayer
from app.services.handlers.drawing import h_tot_04
from app.services.rule_engine import ProjectContext, rule_by_key


class _FakeManualExtractor:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload

    async def extract(self, manual, inputs) -> dict:  # noqa: ANN001
        return self.payload or {
            "change_checks": [
                {
                    "source_text": "我想要三角版的厚度为3mm",
                    "requirement": "三角板厚度 3mm",
                    "status": "found",
                    "evidence": "Triangle plate thickness: 3mm",
                }
            ],
            "requirement_facts": [
                {
                    "source_text": "我想要三角版的厚度为3mm",
                    "object": "三角版",
                    "attribute": "thickness",
                    "expected_value": 3,
                    "unit": "mm",
                }
            ],
        }


def test_fact_layer_keeps_every_input_and_only_resolves_unique_project_entity() -> None:
    ctx = ProjectContext(project_id="facts")
    ctx.manual = ManualDoc(full_text="Triangle plate thickness: 3mm")
    ctx.inputs = {
        "tech_req": "我想要三角版的厚度为3mm；最大总重6,058kg，地板28mm",
        "new_material": "新材料采用耐候钢",
    }
    ctx.spatial = {
        "000A22G1G": [
            {
                "id": "plate-1",
                "name": "F999001_三角板-1",
                "leaf": True,
                "thicknesses": [{"thicknessMm": 3.0}],
            }
        ]
    }

    bundle = asyncio.run(FactLayer(_FakeManualExtractor()).extract(ctx))
    ctx.fact_bundle = bundle
    ctx.manual_facts = bundle.manual_facts

    assert [item.source_text for item in bundle.requirements] == [
        "我想要三角版的厚度为3mm",
        "最大总重6,058kg",
        "地板28mm",
        "新材料采用耐候钢",
    ]
    assert len(bundle.manual_facts["change_checks"]) == 4
    assert bundle.resolutions[0].status == "resolved"
    assert [item.value for item in bundle.observations] == [3.0]
    outcome = h_tot_04(ctx, rule_by_key("TOT-04"))
    assert outcome.verdict == "pass"
    assert len({item["checkpoint"] for item in outcome.evidence}) == 1

    ctx.spatial["000A22G1G"].append(
        {
            "id": "brace-1",
            "name": "F999002_三角筋-1",
            "leaf": True,
            "thicknesses": [{"thicknessMm": 3.0}],
        }
    )
    ambiguous = asyncio.run(FactLayer(_FakeManualExtractor()).extract(ctx))
    assert ambiguous.resolutions[0].status == "ambiguous"


def test_exact_part_name_wins_over_cross_drawing_duplicates_and_longer_names() -> None:
    source = "前端下梁厚度为4mm"
    extractor = _FakeManualExtractor({
        "change_checks": [],
        "requirement_facts": [{
            "source_text": source,
            "object": "前端下梁",
            "attribute": "thickness",
            "expected_value": 4,
            "unit": "mm",
        }],
    })
    ctx = ProjectContext(project_id="exact-part")
    ctx.manual = ManualDoc(full_text="")
    ctx.inputs = {"tech_req": source}
    duplicate = {
        "id": "beam",
        "name": "F140401_20前端下梁-1",
        "leaf": True,
        "thicknesses": [{"thicknessMm": 4.0}],
    }
    ctx.spatial = {
        "000A22G1G": [duplicate, {
            "id": "reinforcement",
            "name": "F150101_前端下梁加强板-1",
            "leaf": True,
            "thicknesses": [{"thicknessMm": 4.0}],
        }],
        "000A22G1F": [{**duplicate, "id": "beam-in-front"}],
    }

    bundle = asyncio.run(FactLayer(extractor).extract(ctx))

    assert bundle.resolutions[0].status == "resolved"
    assert bundle.resolutions[0].label == "F140401_20前端下梁"
    assert [item.value for item in bundle.observations] == [4.0]
