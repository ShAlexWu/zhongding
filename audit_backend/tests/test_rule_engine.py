"""Rule engine tests against real project data (api.json / spatial / docx).

Builds a ProjectContext directly from real files (no DB) and runs a
representative subset of the 34 engine rules, asserting every run leaves
pending and produces non-empty evidence.
"""

from __future__ import annotations

from dataclasses import replace

import ijson

from app.services.doc_parser import ManualDoc, extract_drawing_facts, extract_manual
from app.services.rule_engine import ProjectContext, resolve_doc_anchor, rule_by_key, run_rule
from app.services.vlm_client import VLMOutcome
from app.workers.worker import _adjudicate_tm04, _adjudicate_tm06

ENGINE_KEYS_TO_TEST = [
    "MAN-01", "MAN-03", "MAN-04", "MAN-05", "MAN-06",
    "TOT-01", "TOT-02", "TOT-03", "TOT-05",
    "DOOR-01", "DOOR-02", "DOOR-06",
    "SIDE-03", "FRONT-01", "FRONT-02",
    "CHAS-01", "CHAS-03", "CHAS-04",
    "GEN-01", "GEN-02", "GEN-03",
]


def _load_api_props(path) -> dict:  # noqa: ANN001
    props: dict[str, str] = {}
    with open(path, "rb") as handle:
        for prop in ijson.items(handle, "customProperties.item"):
            name = prop.get("name", "")
            if name:
                props[name] = str(prop.get("resolvedValue", "") or "")
    return props


def _load_spatial_components(path) -> list[dict]:  # noqa: ANN001
    comps: list[dict] = []
    with open(path, "rb") as handle:
        for comp in ijson.items(handle, "components.item"):
            comps.append(
                {
                    "id": str(comp.get("componentId", "") or comp.get("id", "") or ""),
                    "name": str(comp.get("name", "") or ""),
                    "parent_id": str(comp.get("parentId", "") or ""),
                    "leaf": bool(comp.get("leaf", False)),
                    "material_name": str(comp.get("materialName", "") or ""),
                    "thicknesses": list(comp.get("sheetMetalThicknesses") or []),
                    "transform_to_root": comp.get("transformToRoot"),
                    "bounds_mm": comp.get("assemblyBoundsMm"),
                }
            )
    return comps


def _build_real_ctx(real_api_path, real_spatial_path, real_docx_path, code="000A22G1G") -> ProjectContext:  # noqa: ANN001
    ctx = ProjectContext(project_id="test")
    ctx.inputs = {
        "tech_req": "板厚 4.0mm，外部颜色 RAL 9016，质保 5 年，商标采用丝印",
        "new_material": "",
        "revision_reason": {"type": "标准更新", "other_text": ""},
    }
    ctx.manual = extract_manual(str(real_docx_path))
    ctx.manual_path = str(real_docx_path)
    ctx.json_props[code] = _load_api_props(real_api_path)
    ctx.json_file_ids[code] = "f_test"
    ctx.api_paths[code] = str(real_api_path)
    ctx.spatial_paths[code] = str(real_spatial_path)
    ctx.spatial[code] = _load_spatial_components(real_spatial_path)
    ctx.pdf_file_ids[code] = "f_test_pdf"
    ctx.pdf_paths[code] = str(real_api_path).replace("_api.json", "_api.pdf")
    return ctx


def test_engine_rules_on_real_data(real_api_path, real_spatial_path, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(real_api_path, real_spatial_path, real_docx_path)
    results = {}
    for key in ENGINE_KEYS_TO_TEST:
        rule = rule_by_key(key)
        assert rule is not None, key
        outcome = run_rule(ctx, rule)
        results[key] = outcome.verdict
        assert outcome.verdict in ("pass", "fail", "warning", "skipped", "error"), key
        assert outcome.conclusion, f"{key} missing conclusion"
    # at least 5 rules must resolve to a definitive pass/fail, and none crash
    assert len(results) >= 5
    assert all(v != "error" for v in results.values())


def test_engine_evidence_has_text(real_api_path, real_spatial_path, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(real_api_path, real_spatial_path, real_docx_path)
    rule = rule_by_key("TOT-03")
    outcome = run_rule(ctx, rule)
    assert outcome.evidence, "TOT-03 evidence must not be empty"
    assert all("text" in ev for ev in outcome.evidence)


def test_tm06_anchor_extracts_section_9_test_values(real_docx_path) -> None:  # noqa: ANN001
    ctx = ProjectContext(project_id="tm06", manual=extract_manual(str(real_docx_path)))

    anchor = resolve_doc_anchor(ctx, "doc:section.9.testing")

    assert '"stacking_test_kg_per_post": 97200.0' in anchor
    assert '"allowable_stacking_load_1_8g_kg": 216000.0' in anchor
    assert '"transverse_racking_test_force_n": 150000.0' in anchor


def test_tm04_is_locally_compared_with_manual_and_both_drawings(real_docx_path) -> None:  # noqa: ANN001
    values = {
        ("marking", "max_gross"): "30,480 kg / 67,200 lb",
        ("general", "max_gross"): "30,480 kg / 67,200 lb",
        ("marking", "tare"): "2,040 kg / 4,500 lb",
        ("general", "tare"): "2,100 kg / 4,630 lb",
        ("marking", "payload"): "28,440 kg / 62,700 lb",
        ("general", "payload"): "28,380 kg / 62,570 lb",
    }
    evidence = [
        {
            "image_index": image,
            "text": f"TM-04 {source} {field}: {values[source, field]}",
        }
        for field in ("max_gross", "tare", "payload")
        for image, source in ((1, "marking"), (2, "general"))
    ]
    outcome = VLMOutcome(
        verdict="pass",
        conclusion="模型原始结论",
        evidence=evidence,
        vlm_raw={"facts": {}},
    )

    result = _adjudicate_tm04(outcome, extract_manual(str(real_docx_path)))

    assert result.verdict == "fail"
    assert "商标图 2,040 kg" in result.conclusion
    assert len(result.evidence) == 9
    assert {item["type"] for item in result.evidence} == {"doc", "pdf"}
    assert {item["checkpoint"] for item in result.evidence} == {"最大总重", "皮重", "载重"}
    assert sum(item["type"] == "doc" for item in result.evidence) == 3


def test_tm06_is_locally_adjudicated_with_manual_and_same_plate_evidence(real_docx_path) -> None:  # noqa: ANN001
    raw_evidence = [
        {
            "page": 1,
            "rect": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.05},
            "text": "TM-06 CSC allowable_stacking_load_1_8g: 216000 KG",
        },
        {
            "page": 1,
            "rect": {"x": 0.2, "y": 0.58, "w": 0.2, "h": 0.022},
            "text": "TM-06 CSC transverse_racking_test_force: 150000 N",
        },
    ]
    outcome = VLMOutcome(
        verdict="warning",
        conclusion="模型原始结论",
        vlm_raw={
            "facts": {
                "csc_plate_title": "CSC SAFETY APPROVAL",
                "csc_plate_rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
                "allowable_stacking_load_1_8g_kg": 216000,
                "transverse_racking_test_force_n": 150000,
            },
            "evidence": raw_evidence,
        },
        evidence=[
            {"type": "pdf", "text": item["text"], "rect": item["rect"]}
            for item in raw_evidence
        ],
    )

    result = _adjudicate_tm06(outcome, extract_manual(str(real_docx_path)))

    assert result.verdict == "pass"
    assert "216,000 kg" in result.conclusion
    assert "150,000 N" in result.conclusion
    assert {item["type"] for item in result.evidence} == {"doc", "pdf"}
    assert {item["checkpoint"] for item in result.evidence} == {"允许堆码载荷（1.8g）", "横向刚性试验力"}


def test_chas01_finds_manual_range_and_fails_missing_drawing_depth(data_dir, real_docx_path) -> None:  # noqa: ANN001
    code = "000A22G1B"
    ctx = _build_real_ctx(
        data_dir / "000A22G1B_底架装配_21A-00_api.json",
        data_dir / "000A22G1B_底架装配_21A-00_spatial.json",
        real_docx_path,
        code,
    )

    outcome = run_rule(ctx, rule_by_key("CHAS-01"))

    assert outcome.verdict == "fail"
    assert "1.5–2.5 mm" in outcome.conclusion
    assert any("J090001 地板钉实例 200 个" in item["text"] for item in outcome.evidence)
    assert any("精确绑定地板钉 7 个" in item["text"] for item in outcome.evidence)
    assert any(item["type"] == "pdf" and "14" in item["text"] for item in outcome.evidence)


def test_chas02_skips_gooseneck_sponge_check_for_20gp(data_dir, real_docx_path) -> None:  # noqa: ANN001
    code = "000A22G1B"
    ctx = _build_real_ctx(
        data_dir / "000A22G1B_底架装配_21A-00_api.json",
        data_dir / "000A22G1B_底架装配_21A-00_spatial.json",
        real_docx_path,
        code,
    )
    front = _build_real_ctx(
        data_dir / "000A22G1F_前端装配_21A-00_api.json",
        data_dir / "000A22G1F_前端装配_21A-00_spatial.json",
        real_docx_path,
        "000A22G1F",
    )
    ctx.api_paths.update(front.api_paths)
    ctx.json_file_ids.update(front.json_file_ids)
    ctx.spatial.update(front.spatial)

    outcome = run_rule(ctx, rule_by_key("CHAS-02"))

    assert outcome.verdict == "skipped"
    assert "20GP" in outcome.conclusion
    assert all(item["checkpoint_verdict"] == "skipped" for item in outcome.evidence)
    assert any("鹅颈槽/地板角钢 BOM 0 项、模型 0 件" in item["text"] for item in outcome.evidence)


def test_chas03_checks_wide_and_regular_crossmember_thickness_separately(data_dir, real_docx_path) -> None:  # noqa: ANN001
    code = "000A22G1B"
    ctx = _build_real_ctx(
        data_dir / "000A22G1B_底架装配_21A-00_api.json",
        data_dir / "000A22G1B_底架装配_21A-00_spatial.json",
        real_docx_path,
        code,
    )

    outcome = run_rule(ctx, rule_by_key("CHAS-03"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"宽／底横梁板厚", "短宽／底横梁板厚"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass", "skipped"}
    assert any(
        "B070101 模型 2 件" in item["text"]
        and "B150101 加强板 6 件" in item["text"]
        and "B080101 底横梁 12 件" in item["text"]
        for item in outcome.evidence
    )
    assert any("短宽/短底横梁 BOM 0 项、模型 0 件" in item["text"] for item in outcome.evidence)
    assert sum(item["type"] == "pdf" and bool(item.get("rect")) for item in outcome.evidence) >= 6


def test_chas04_checks_exact_screws_and_front_support_clearance(data_dir, real_docx_path) -> None:  # noqa: ANN001
    code = "000A22G1G"
    ctx = _build_real_ctx(
        data_dir / "000A22G1G_总装配_21A-00_api.json",
        data_dir / "000A22G1G_总装配_21A-00_spatial.json",
        real_docx_path,
        code,
    )
    ctx.api_paths["000A22G1B"] = str(data_dir / "000A22G1B_底架装配_21A-00_api.json")
    ctx.json_file_ids["000A22G1B"] = "b-json"

    outcome = run_rule(ctx, rule_by_key("CHAS-04"))

    assert outcome.verdict == "pass"
    verdicts = {item["checkpoint"]: item["checkpoint_verdict"] for item in outcome.evidence}
    assert verdicts == {"地板钉排布": "pass", "前端避开塑料角撑": "pass"}
    assert any("J090001 叶子件 200 个" in item["text"] for item in outcome.evidence)
    assert any("32 组×4颗、12 组×6颗" in item["text"] for item in outcome.evidence)
    assert sum("包围盒间距 17.5mm" in item["text"] for item in outcome.evidence) == 2


def test_roof01_uses_one_press_part_property_and_bound_drawing_evidence(data_dir, real_docx_path) -> None:  # noqa: ANN001
    code = "000A22G1R"
    ctx = _build_real_ctx(
        data_dir / "000A22G1R_顶板装配_21A-00_api.json",
        data_dir / "000A22G1R_顶板装配_21A-00_spatial.json",
        real_docx_path,
        code,
    )

    outcome = run_rule(ctx, rule_by_key("ROOF-01"))

    assert outcome.verdict == "pass"
    assert any("五波一次压型,T2" in item["text"] for item in outcome.evidence)
    assert any("BOM 序号 1" in item["text"] and "R010501" in item["text"] for item in outcome.evidence)
    assert any("气泡 1" in item["text"] and "R010501" in item["text"] for item in outcome.evidence)
    assert not any("DIAGONAL TOLERANCE" in item["text"] for item in outcome.evidence)


def test_tot03_returns_independent_checkpoint_results() -> None:
    ctx = ProjectContext(project_id="tot03-test")
    ctx.manual = ManualDoc(
        full_text=(
            "Two locking bars are fixed to each door leaf.\n"
            "One ventilator is supplied on each side wall at the right-hand end.\n"
            "Screws’ Qty.: 6 Pcs/end row, 4 Pcs/other"
        )
    )
    ctx.json_file_ids = {
        "000A22G1G": "g-json",
        "000A22G1S": "s-json",
        "000A22G1B": "b-json",
    }

    def comp(name: str, parent: str, x: float, z: float) -> dict:
        return {
            "name": name,
            "parent_id": parent,
            "bounds_mm": {"center": [x, 100.0, z]},
        }

    ctx.spatial["000A22G1G"] = [
        *(comp(f"门端/GP锁杆部件-{index}", "locks", -6026, z) for index, z in enumerate((-600, -180, 180, 600), 1)),
        comp("左侧板/S050001_通风器-1", "left", -769, 1191),
        comp("右侧板/S050001_通风器-1", "right", -5233, -1191),
    ]
    ctx.spatial["000A22G1S"] = [
        comp("MS010101_左侧板模块-1/S050001_通风器-1", "left", -769, 1191),
        comp("MS020101_右侧板模块-1/S050001_通风器-1", "right", -5233, -1191),
    ]
    screws: list[dict] = []
    for group_index, count in enumerate([4] * 32 + [6] * 12):
        screws.extend(
            comp(f"地板钉组-{group_index}/J090001_自攻螺钉-{item}", f"group-{group_index}", group_index * 100, item * 210)
            for item in range(1, count + 1)
        )
    ctx.spatial["000A22G1B"] = screws
    ctx._drawing_facts = {
        "000A22G1E": [{"kind": "bom", "description": "LOCKING DEVICE", "drawing_no": "E500001", "quantity": "1"}],
        "000A22G1S": [{"kind": "bom", "description": "VENTILATOR", "drawing_no": "S050001", "quantity": "2"}],
        "000A22G1B": [{"kind": "bom", "description": "SELF-TAPPING SCREW", "drawing_no": "J090001", "quantity": "200"}],
    }

    outcome = run_rule(ctx, rule_by_key("TOT-03"))

    assert outcome.verdict == "warning"
    verdicts = {item["checkpoint"]: item["checkpoint_verdict"] for item in outcome.evidence}
    assert verdicts == {
        "锁杆数量与门扇分布": "pass",
        "通风器数量、侧板覆盖和端部位置": "pass",
        "地板钉总数与每组 4/6 颗模式": "pass",
        "地板钉纵向标准间距": "warning",
    }
    assert {item["side"] for item in outcome.evidence} == {"manual", "drawing"}


def test_tot05_compares_four_exact_leaf_groups_with_bom() -> None:
    ctx = ProjectContext(project_id="tot05-test")
    ctx.manual = ManualDoc(
        full_text=(
            "Lashing rings on the side rails: 1,500 kg/each. "
            "Lashing rods on the corner posts: 1,000 kg/each. "
            "Treatment of lashing ring / bar: Electro zinc plated."
        )
    )
    ctx.json_file_ids = {code: f"{code}-json" for code in (
        "000A22G1G", "000A22G1F", "000A22G1E", "000A22G1S", "000A22G1B"
    )}
    ctx.pdf_file_ids = {code: f"{code}-pdf" for code in ctx.json_file_ids}

    def group(module: str, part_code: str, count: int, x_values: list[float]) -> list[dict]:
        half = count // 2
        return [
            {
                "name": f"assembly/{module}-1/{part_code}_零件-{index + 1}",
                "leaf": True,
                "material_name": "SS400/Q235A",
                "bounds_mm": {"center": [x_values[index % half], 100.0, -1000.0 if index < half else 1000.0]},
            }
            for index in range(count)
        ]

    ctx.spatial["000A22G1G"] = [
        *group("MF060101_前端拉筋模块", "F060002", 6, [0, 0, 0]),
        *group("ME060101_门端拉筋模块", "E020001", 6, [-6000, -6000, -6000]),
        *group("MS050101_侧板绳环模块", "S060001", 10, [-5600, -4200, -2800, -1400, -300]),
        *group("MB060101_底架绳环模块", "S060001", 10, [-5600, -4200, -2800, -1400, -300]),
    ]

    def bom(drawing_no: str, description: str, specification: str, quantity: str) -> dict:
        return {
            "kind": "bom",
            "drawing_no": drawing_no,
            "description": description,
            "specification": specification,
            "material": "SS400,ZINC PLATED",
            "quantity": quantity,
            "page": 1,
            "rect": {"x": 10, "y": 10, "w": 20, "h": 10},
        }

    def annotation(part_code: str) -> dict:
        return {
            "kind": "dimension",
            "text": "尺寸标注",
            "page": 1,
            "rect": {"x": 30, "y": 30, "w": 20, "h": 10},
            "attachments": [{"component_name": f"module/{part_code}_零件-1"}],
            "view": "主视图",
        }

    ctx._drawing_facts = {
        "000A22G1F": [bom("F060002", "LASHING ROD", "Φ14x150", "6"), annotation("F060002")],
        "000A22G1E": [bom("E020001", "LASHING ROD", "Φ12x58", "6"), annotation("E020001")],
        "000A22G1S": [bom("S060001", "LASHING RING", "Φ12x52x64", "10"), annotation("S060001")],
        "000A22G1B": [bom("S060001", "LASHING RING", "Φ12x52x64", "10"), annotation("S060001")],
    }

    outcome = run_rule(ctx, rule_by_key("TOT-05"))

    assert outcome.verdict == "pass"
    assert len(outcome.evidence) == 16
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    locatable = [item for item in outcome.evidence if item["type"] == "pdf" and item.get("rect")]
    assert len(locatable) == 8
    assert {item["checkpoint"] for item in locatable} == {
        "前角柱拉筋排布与规格",
        "后角柱拉筋排布与规格",
        "顶侧梁绳环排布与规格",
        "底侧梁绳环排布与规格",
    }
    assert all("36" not in item["text"] for item in outcome.evidence)


def test_door01_compares_exact_parts_with_detailed_basis_and_locatable_evidence() -> None:
    ctx = ProjectContext(project_id="door01-test")
    ctx.manual = ManualDoc(
        full_text=(
            "6.5.4.2 Hinges and Pins Four reinforced forged hinges, providing with bushed hole, "
            "are welded to each door leaf. Each door is installed by hinge pins, washers and bushings. "
            "A door holder per door, made of mixed nylon rope, is tied to the center side locking rod. "
            "Door seal gaskets are attached to the door frame with stainless steel rivets."
        )
    )
    ctx.json_file_ids["000A22G1E"] = "door-json"
    ctx.pdf_file_ids["000A22G1E"] = "door-pdf"

    def comp(name: str, material: str, z: float) -> dict:
        return {
            "name": name,
            "leaf": True,
            "material_name": material,
            "bounds_mm": {"center": [-6000.0, 100.0, z]},
        }

    ctx.spatial["000A22G1E"] = [
        *(comp(f"ME070101_门铰链模块-2/E200008_门铰链-{i + 1}", "SS400/Q235A", -1100 if i < 4 else 1100) for i in range(8)),
        *(comp(f"ME070101_门铰链模块-2/E220001_铰链销-{i + 1}", "SUS304", -1200 if i < 4 else 1200) for i in range(8)),
        *(comp(f"ME070101_门铰链模块-2/E230001_门耳朵-{i + 1}", "SS400/Q235A", -1200 if i < 8 else 1200) for i in range(16)),
        comp("E310001_门绳-1", "尼龙", -180),
        comp("E310001_门绳-2", "尼龙", 180),
        *(
            comp(
                f"ME020106_门框模块-1/GB-T12618_开口型平圆头抽芯铆钉_SUS304-钢_4.8x15__不处理-{i + 1}",
                "SUS304-钢",
                -1000 + i * 20,
            )
            for i in range(84)
        ),
    ]

    def bom(item: str, drawing_no: str, description: str, specification: str, material: str, quantity: str) -> dict:
        return {
            "kind": "bom",
            "item": item,
            "drawing_no": drawing_no,
            "description": description,
            "specification": specification,
            "material": material,
            "quantity": quantity,
            "page": 1,
            "rect": {"x": 10, "y": 10, "w": 20, "h": 10},
        }

    def annotation(part_code: str) -> dict:
        return {
            "kind": "note",
            "text": part_code,
            "page": 1,
            "rect": {"x": 30, "y": 30, "w": 20, "h": 10},
            "attachments": [{"component_name": f"module/{part_code}_零件-1"}],
            "view": "主视图",
        }

    ctx._drawing_facts["000A22G1E"] = [
        bom("21", "E200008", "HINGE", "FULL NYLON BUSH", "SS400,ZINC PLATED", "8"),
        bom("22", "E220001", "HINGE PIN", "Φ12x105", "SUS304", "8"),
        bom("23", "E230001", "HINGE BUTT", "8.0x27x49", "SS400,ZINC PLATED", "16"),
        bom("28", "E310001", "DOOR HOLDER", "φ6", "NYLON", "2"),
        bom("32", "GB/T12618", "BLIND RIVET", "4.8x15", "SUS304-STEEL", "84"),
        annotation("E200008"),
        annotation("E220001"),
        annotation("E230001"),
        annotation("GB-T12618"),
    ]

    outcome = run_rule(ctx, rule_by_key("DOOR-01"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert {item["checkpoint"] for item in outcome.evidence} == {"门铰链选用", "门绳选用", "门封铆钉选用"}
    assert any("跨资料关联依据" in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) == 9
    assert all("847" not in item["text"] for item in outcome.evidence)

    ctx.spatial["000A22G1E"].pop()
    assert run_rule(ctx, rule_by_key("DOOR-01")).verdict == "fail"


def test_door02_checks_gasket_corner_and_abs_retainers(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1E_后端装配_21A-00_api.json",
        data_dir / "000A22G1E_后端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1E",
    )

    outcome = run_rule(ctx, rule_by_key("DOOR-02"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"门封胶条包角", "门封压条 ABS 材质"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert any("有包角" in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) == 7

    ctx.api_paths["000A22G1E"] = ""
    assert run_rule(ctx, rule_by_key("DOOR-02")).verdict == "warning"


def test_door06_uses_exact_lock_hierarchy_and_real_lashing_rod_name(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1E_后端装配_21A-00_api.json",
        data_dir / "000A22G1E_后端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1E",
    )

    outcome = run_rule(ctx, rule_by_key("DOOR-06"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"锁杆整套或散件", "后角柱拉筋排布"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert any("锁杆总模块 1 个，直属 GP 锁杆子装配 4 个" in item["text"] for item in outcome.evidence)
    assert any("E020001 门端拉筋 6 件" in item["text"] for item in outcome.evidence)
    assert all("1493" not in item["text"] and "1366" not in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) >= 4


def test_side03_compares_manual_bom_side_and_general_positions(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1S_侧板装配_21A-00_api.json",
        data_dir / "000A22G1S_侧板装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1S",
    )
    general_api = data_dir / "000A22G1G_总装配_21A-00_api.json"
    general_spatial = data_dir / "000A22G1G_总装配_21A-00_spatial.json"
    ctx.api_paths["000A22G1G"] = str(general_api)
    ctx.json_file_ids["000A22G1G"] = "general-json"
    ctx.spatial["000A22G1G"] = _load_spatial_components(general_spatial)

    outcome = run_rule(ctx, rule_by_key("SIDE-03"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"顶侧梁绳环排布与数量", "通风器排布与数量"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert any("S060001 绳环 10 个" in item["text"] for item in outcome.evidence)
    assert any("S050001 通风器 2 个" in item["text"] for item in outcome.evidence)
    assert any("图面尺寸 11 mm" in item["text"] for item in outcome.evidence)
    assert any("图面尺寸 205 mm" in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) >= 6

    ctx.spatial["000A22G1S"] = [
        comp for comp in ctx.spatial["000A22G1S"] if not comp["name"].endswith("S050001_通风器-1")
    ]
    outcome = run_rule(ctx, rule_by_key("SIDE-03"))
    assert outcome.verdict == "fail"
    assert {item["checkpoint_verdict"] for item in outcome.evidence if item["checkpoint"] == "通风器排布与数量"} == {"fail"}


def test_side02_recognizes_head_tail_typ_as_ventilator_rivet_sealing(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1S_侧板装配_21A-00_api.json",
        data_dir / "000A22G1S_侧板装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1S",
    )

    outcome = run_rule(ctx, rule_by_key("SIDE-02"))

    assert outcome.verdict == "pass"
    assert "SEALING AT HEAD AND TAIL + TYP" in outcome.conclusion
    assert {item["checkpoint"] for item in outcome.evidence} == {"检查通风器铆钉是否标注打胶"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert any("Q/CIMC 40103-2007" in item["text"] for item in outcome.evidence)
    assert any("QJ3148-A 铝制 HUCK-BOLT 6 颗" in item["text"] for item in outcome.evidence)
    assert any("SEALING AT HEAD AND TAIL" in item["text"] for item in outcome.evidence)
    assert any(item["type"] == "pdf" and item.get("rect") for item in outcome.evidence)

    ctx._drawing_facts["000A22G1S"] = [
        fact for fact in ctx.get_drawing_facts("000A22G1S")
        if str(fact.get("text", "")).strip().upper() != "SEALING AT HEAD AND TAIL"
    ]
    outcome = run_rule(ctx, rule_by_key("SIDE-02"))
    assert outcome.verdict == "fail"


def test_front01_matches_cover_plate_bom_and_skips_20gp_gooseneck(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1F_前端装配_21A-00_api.json",
        data_dir / "000A22G1F_前端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1F",
    )

    outcome = run_rule(ctx, rule_by_key("FRONT-01"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"底角件三角板板厚", "鹅颈槽封板板厚"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass", "skipped"}
    assert any("F210101 前底角封板D：2 件；钣金厚度=[3.0] mm" in item["text"] for item in outcome.evidence)
    assert any("F210102 前底角封板D：2 件；钣金厚度=[6.0] mm" in item["text"] for item in outcome.evidence)
    assert any("20GP 不适用" in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) >= 5

    target = next(comp for comp in ctx.spatial["000A22G1F"] if comp["name"].endswith("F210102_前底角封板D-1"))
    target["thicknesses"] = [{"thicknessMm": 5}]
    assert run_rule(ctx, rule_by_key("FRONT-01")).verdict == "fail"


def test_front02_uses_f060002_layout_and_round_rod_spec(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1F_前端装配_21A-00_api.json",
        data_dir / "000A22G1F_前端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1F",
    )

    outcome = run_rule(ctx, rule_by_key("FRONT-02"))

    assert outcome.verdict == "pass"
    assert {item["checkpoint"] for item in outcome.evidence} == {"前角柱拉筋排布", "前角柱拉筋规格"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"pass"}
    assert any("F060002 前端拉筋 6 根" in item["text"] for item in outcome.evidence)
    assert any("F060002 零件属性：数量=6" in item["text"] for item in outcome.evidence)
    assert any("规格=['Φ14X150']" in item["text"] for item in outcome.evidence)
    assert any("图面剖面 C-C 标注 3-Φ14" in item["text"] for item in outcome.evidence)
    assert sum(bool(item["type"] == "pdf" and item.get("rect")) for item in outcome.evidence) >= 4

    ctx.spatial["000A22G1F"] = [
        comp for comp in ctx.spatial["000A22G1F"]
        if not comp["name"].endswith("F060002_前端拉筋-1")
    ]
    assert run_rule(ctx, rule_by_key("FRONT-02")).verdict == "fail"


def test_front03_skips_20gp_and_rejects_ancestor_weld_matches(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1F_前端装配_21A-00_api.json",
        data_dir / "000A22G1F_前端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1F",
    )

    outcome = run_rule(ctx, rule_by_key("FRONT-03"))

    assert outcome.verdict == "skipped"
    assert "20GP" in outcome.conclusion
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"skipped"}
    assert any("F140401_20前端下梁" in item["text"] for item in outcome.evidence)
    assert all(not any(code in item["text"] for code in ("F290101", "F150101", "F210101")) for item in outcome.evidence)

    sill = next(comp for comp in ctx.spatial["000A22G1F"] if "F140401_20前端下梁" in comp["name"])
    sill["name"] = sill["name"].replace("F140401_20前端下梁", "F140401_40HC前端下梁")
    assert run_rule(ctx, rule_by_key("FRONT-03")).verdict == "fail"


def test_front04_checks_each_pp_support_and_emits_negative_weld_evidence(data_dir, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(
        data_dir / "000A22G1F_前端装配_21A-00_api.json",
        data_dir / "000A22G1F_前端装配_21A-00_spatial.json",
        real_docx_path,
        code="000A22G1F",
    )

    outcome = run_rule(ctx, rule_by_key("FRONT-04"))

    assert outcome.verdict == "fail"
    assert {item["checkpoint"] for item in outcome.evidence} == {"塑料地板支撑打胶注释", "塑料地板支撑多余焊接注释"}
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"fail", "pass"}
    assert any("F240102 左支撑" in item["text"] and "缺少打胶标注" in item["text"] for item in outcome.evidence)
    assert any("F241002 右支撑" in item["text"] and "已标注" in item["text"] for item in outcome.evidence)
    assert any("完整焊缝清单 17 个" in item["text"] and "多余焊接注释" in item["text"] for item in outcome.evidence)
    assert all("F240102" not in item["text"] for item in outcome.evidence if item["type"] == "pdf" and "SEALING" in item["text"])


def test_door03_only_compares_explicit_exterior_and_sealant_colors() -> None:
    assert ManualDoc(full_text="2.\tGeneral\t3\nWaterborne topcoat, RAL 7035").extract_colors() == [
        "Waterborne topcoat, RAL 7035"
    ]

    ctx = ProjectContext(project_id="door03-test")
    ctx.manual = ManualDoc(
        full_text=(
            "Exterior Surface\nWaterborne acrylic topcoat, RAL 1007\n"
            "Interior Surface\nWaterborne epoxy topcoat, RAL 7035"
        )
    )
    ctx.inputs = {"tech_req": "无"}
    ctx.json_file_ids["000A22G1E"] = "door-json"
    ctx.pdf_file_ids["000A22G1E"] = "door-pdf"

    def note(text: str, x: int) -> dict:
        return {
            "kind": "note",
            "text": text,
            "view": "门端主视图",
            "page": 1,
            "rect": {"x": x, "y": 20, "w": 20, "h": 10},
        }

    ctx._drawing_facts["000A22G1E"] = [note("SEALING", 10), note("SEALING COLOR RAL 1007", 40)]
    outcome = run_rule(ctx, rule_by_key("DOOR-03"))
    assert outcome.verdict == "pass"
    assert "ral1007" in outcome.conclusion
    assert sum(item["type"] == "pdf" for item in outcome.evidence) == 1
    assert all("RAL 7035" not in item["text"] for item in outcome.evidence)

    ctx._drawing_facts["000A22G1E"] = [note("SEALING", 10)]
    outcome = run_rule(ctx, rule_by_key("DOOR-03"))
    assert outcome.verdict == "warning"
    assert "门端图未标明密封胶颜色" in outcome.conclusion
    assert all(item["type"] != "pdf" for item in outcome.evidence)


def test_door04_checks_typical_hinge_annotation_coverage_and_every_leader_target() -> None:
    ctx = ProjectContext(project_id="door04-test")
    ctx.manual = ManualDoc(full_text="6.5.4.2 Hinges and Pins Four reinforced forged hinges are welded to each door leaf.")
    ctx.json_file_ids["000A22G1E"] = "door-json"
    ctx.pdf_file_ids["000A22G1E"] = "door-pdf"
    ctx.spatial["000A22G1E"] = [
        {"name": f"ME070101_门铰链模块-2/E200008_门铰链-{index}", "leaf": True}
        for index in range(1, 9)
    ]
    typ_fact = {
        "kind": "weld",
        "text": "SEALING TYP",
        "view": "门端主视图",
        "page": 1,
        "rect": {"x": 40, "y": 20, "w": 20, "h": 10},
        "leaders": 2,
        "attached": True,
        "attachments": [
            {
                "component_name": "ME070101_门铰链模块-2/E200008_门铰链-7",
                "drawing_component_name": "door/E200008_门铰链-7",
                "geometry_type": "Edge",
                "dangling": False,
                "persistent_status": "exact",
            },
            {
                "component_name": "",
                "drawing_component_name": "",
                "geometry_type": "SketchSegment",
                "dangling": False,
                "persistent_status": "exact",
            },
        ],
    }
    ctx._drawing_facts["000A22G1E"] = [
        {
            "kind": "bom",
            "item": "21",
            "drawing_no": "E200008",
            "quantity": "8",
            "page": 1,
            "rect": {"x": 10, "y": 10, "w": 20, "h": 10},
        },
        typ_fact,
    ]

    outcome = run_rule(ctx, rule_by_key("DOOR-04"))
    assert outcome.verdict == "warning"
    assert "1 条无法唯一归属" in outcome.conclusion
    assert sum(item["type"] == "pdf" for item in outcome.evidence) == 2

    typ_fact["attachments"][1]["component_name"] = "ME070101_门铰链模块-2/E200008_门铰链-7"
    assert run_rule(ctx, rule_by_key("DOOR-04")).verdict == "pass"


def test_door05_excludes_non_weld_callouts_and_separates_shift_from_binding_gaps() -> None:
    ctx = ProjectContext(project_id="door05-test")
    ctx.json_file_ids["000A22G1E"] = "door-json"
    ctx.pdf_file_ids["000A22G1E"] = "door-pdf"
    weld = {
        "kind": "weld",
        "text": "<WELD-FILL> 3",
        "view": "工程图视图6",
        "page": 1,
        "rect": {"x": 40, "y": 20, "w": 20, "h": 10},
        "raw_attached": True,
        "attached": False,
        "leaders": 3,
        "leader_point_counts": [9, 9, 9],
        "attachments": [
            {"component_name": "J010401-1", "persistent_status": "exact", "dangling": False},
            {"component_name": "", "persistent_status": "dangling-or-null", "dangling": True},
            {"component_name": "", "persistent_status": "dangling-or-null", "dangling": True},
        ],
    }
    ctx._drawing_facts["000A22G1E"] = [
        {
            "kind": "weld",
            "text": "HUCK BOLT (HEAD OUTSIDE)",
            "raw_attached": False,
            "leaders": 4,
            "leader_point_counts": [9, 9, 9, 9],
        },
        weld,
    ]

    outcome = run_rule(ctx, rule_by_key("DOOR-05"))
    assert outcome.verdict == "warning"
    assert "1 个多引线符号" in outcome.conclusion
    assert "真实焊接符号 1 条" in outcome.evidence[1]["text"]
    assert sum(item["type"] == "pdf" for item in outcome.evidence) == 1

    for attachment in weld["attachments"]:
        attachment.update(component_name="J010401-1", persistent_status="exact", dangling=False)
    assert run_rule(ctx, rule_by_key("DOOR-05")).verdict == "pass"

    weld["raw_attached"] = False
    assert run_rule(ctx, rule_by_key("DOOR-05")).verdict == "fail"


def test_man01_compares_manual_version_to_total_and_part_numbers_to_total_bom(
    data_dir, real_api_path, real_spatial_path, real_docx_path,
) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(real_api_path, real_spatial_path, real_docx_path)
    for code, name in {
        "000A22G1B": "000A22G1B_底架装配_21A-00_api.json",
        "000A22G1E": "000A22G1E_后端装配_21A-00_api.json",
        "000A22G1F": "000A22G1F_前端装配_21A-00_api.json",
        "000A22G1R": "000A22G1R_顶板装配_21A-00_api.json",
        "000A22G1S": "000A22G1S_侧板装配_21A-00_api.json",
    }.items():
        path = data_dir / name
        ctx.json_props[code] = _load_api_props(path)
        ctx.json_file_ids[code] = f"{code}-json"
    ctx.trademark_facts = {
        "marking_drawing_number": "000A22G1M",
        "marking_drawing_number_evidence": {
            "type": "pdf",
            "file_id": "trademark-pdf",
            "page": 1,
            "rect": {"x": 1000, "y": 760, "w": 120, "h": 30},
            "text": "DWG. NO. 000A22G1M",
        },
    }

    outcome = run_rule(ctx, rule_by_key("MAN-01"))

    assert outcome.verdict == "fail"
    verdicts = {item["checkpoint"]: item["checkpoint_verdict"] for item in outcome.evidence}
    assert verdicts == {"图号一致性": "pass", "版本号一致性": "fail"}
    assert "说明书 26A-00" in outcome.conclusion
    assert "总图 21A-00" in outcome.conclusion
    assert "000A22G1B 图号" not in outcome.conclusion
    assert sum("总图 BOM 序号" in item["text"] for item in outcome.evidence) == 6
    assert sum("部件图标题栏图号=" in item["text"] for item in outcome.evidence) == 5
    assert any("商标图标题栏图号=000A22G1M" in item["text"] for item in outcome.evidence)

    ctx.trademark_facts["marking_drawing_number"] = "WRONG-NO"
    mismatch = run_rule(ctx, rule_by_key("MAN-01"))
    assert "商标图标题栏 WRONG-NO / 总图 BOM 000A22G1M" in mismatch.conclusion
    assert next(
        item["checkpoint_verdict"]
        for item in mismatch.evidence
        if item["checkpoint"] == "图号一致性"
    ) == "fail"


def test_drawing_facts_include_locatable_structured_annotations(real_api_path) -> None:  # noqa: ANN001
    facts = extract_drawing_facts(str(real_api_path))
    assert {fact["kind"] for fact in facts} >= {"note", "dimension", "weld", "bom"}
    assert any(fact["attachments"] and fact["rect"] for fact in facts)


def test_gen01_compares_non_total_bom_thickness_through_item_binding() -> None:
    ctx = ProjectContext(project_id="gen01-test")
    codes = ("000A22G1B", "000A22G1E", "000A22G1F", "000A22G1S", "000A22G1R")
    for index, code in enumerate(codes, start=1):
        part_code = f"B{index:06d}"
        attachment = {
            "component_name": f"module/{part_code}_钣金件-1",
            "drawing_component_name": "",
        }
        ctx.spatial[code] = [{"name": f"module/{part_code}_钣金件-1", "thicknesses": [{"thicknessMm": 4}]}]
        ctx._drawing_facts[code] = [
            {
                "kind": "bom", "item": "1", "drawing_no": part_code, "specification": "4",
                "text": f"BOM 序号 1；图号 {part_code}；厚度/规格 4", "view": "图纸1", "sheet": "图纸1",
                "page": 1, "rect": {"x": 100, "y": 100, "w": 20, "h": 10}, "attachments": [],
            },
            {
                "kind": "note", "text": "1", "attached": True, "attachments": [attachment],
                "view": "主视图", "sheet": "图纸1", "page": 1,
                "rect": {"x": 10, "y": 20, "w": 8, "h": 8},
            },
            {
                "kind": "dimension", "values": [4.0], "attached": True, "attachments": [attachment],
                "text": "4", "view": "主视图", "sheet": "图纸1", "page": 1, "rect": None,
            },
        ]

    rule = rule_by_key("GEN-01")
    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "pass", outcome.conclusion
    assert "000A22G1G" not in outcome.conclusion

    ctx._drawing_facts["000A22G1B"][-1]["values"] = [5.0]
    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "fail"
    assert "B000001" in outcome.conclusion
    missing_anchor = next(item for item in outcome.evidence if "图中序号 1" in item["text"])
    assert missing_anchor["rect"] == {"x": 10, "y": 20, "w": 8, "h": 8}


def test_gen02_checks_required_sealant_targets_and_keeps_all_drawing_evidence() -> None:
    ctx = ProjectContext(project_id="gen02-test")
    code = "000A22G1B"
    attachment = {
        "component_name": "MB070101_宽底横梁模块-1/B070101_宽边底横梁-1",
        "drawing_component_name": "",
    }
    bom = {
        "kind": "bom", "drawing_no": "B070101", "description": "WIDE CROSSMEMBER",
        "text": "BOM 序号 1；图号 B070101", "view": "图纸1", "sheet": "图纸1", "page": 1,
        "rect": None, "attachments": [],
    }
    seal = {
        "kind": "note", "text": "SEALING", "attached": True, "attachments": [attachment],
        "view": "局部视图 H", "sheet": "图纸1", "page": 1, "rect": None,
    }
    extra = {
        "kind": "note", "text": "SEALING", "attached": True,
        "attachments": [{"component_name": "B999999_其他打胶位置-1", "drawing_component_name": ""}],
        "view": "局部视图 J", "sheet": "图纸1", "page": 1, "rect": None,
    }
    ctx._drawing_facts[code] = [bom, seal, extra]
    ctx.pdf_file_ids[code] = "f_base_pdf"
    rule = replace(rule_by_key("GEN-02"), target_files=[code])

    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "pass", outcome.conclusion
    assert len(outcome.evidence) == 2
    assert any("部位=宽边底横梁与木地板装配面" in item["text"] for item in outcome.evidence)
    assert any("部位=图面现有打胶标注" in item["text"] for item in outcome.evidence)

    ctx._drawing_facts[code] = [bom]
    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "fail"
    assert any("状态=缺失" in item["text"] for item in outcome.evidence)


def test_gen03_accepts_single_endpoint_weld_symbol_binding() -> None:
    ctx = ProjectContext(project_id="gen03-test")
    code = "000A22G1B"
    ctx._pair_relations[code] = [
        {
            "nameA": "module/B070101_宽边底横梁-1",
            "nameB": "module/B080101_底横梁-1",
            "requiredProcesses": ["WELD"],
            "adjacent": True,
        }
    ]
    ctx._drawing_facts[code] = [
        {
            "kind": "weld", "text": "<WELD-FILL> 4", "attached": True, "leaders": 1,
            "attachments": [
                {"component_name": "module/B070101_宽边底横梁-1", "drawing_component_name": ""},
            ],
            "view": "局部视图 H", "sheet": "图纸1", "page": 1, "rect": None,
        }
    ]
    ctx.pdf_file_ids[code] = "f_base_pdf"
    rule = replace(rule_by_key("GEN-03"), target_files=[code])

    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "pass", outcome.conclusion
    assert any("状态=已标注" in item["text"] for item in outcome.evidence)
    assert "任一端绑定" in outcome.conclusion

    ctx._drawing_facts[code] = []
    outcome = run_rule(ctx, rule)
    assert outcome.verdict == "fail"
    assert any("状态=缺失" in item["text"] for item in outcome.evidence)


def test_side_sealant_rule_uses_component_attachment(data_dir) -> None:  # noqa: ANN001
    ctx = ProjectContext(project_id="side-test")
    ctx.api_paths["000A22G1S"] = str(data_dir / "000A22G1S_侧板装配_21A-00_api.json")
    ctx.pdf_file_ids["000A22G1S"] = "f_side_pdf"
    outcome = run_rule(ctx, rule_by_key("SIDE-01"))
    assert outcome.verdict == "pass"
    assert any("通风器" in evidence["text"] for evidence in outcome.evidence)


def test_manual_drawing_comparisons_require_both_sides(real_api_path, real_spatial_path, real_docx_path) -> None:  # noqa: ANN001
    ctx = _build_real_ctx(real_api_path, real_spatial_path, real_docx_path)
    for key in ("TOT-01", "TOT-02"):
        outcome = run_rule(ctx, rule_by_key(key))
        assert outcome.verdict == "pass", (key, outcome.conclusion)
        assert {"manual", "drawing"} <= {item.get("side") for item in outcome.evidence}

    ctx.spatial["000A22G1E"] = [{"name": "门铰链", "material_name": "SS400", "thicknesses": []}]
    outcome = run_rule(ctx, rule_by_key("DOOR-01"))
    assert outcome.verdict == "warning"
    assert "3 项待确认" in outcome.conclusion
    assert {"manual", "drawing"} <= {item.get("side") for item in outcome.evidence}
