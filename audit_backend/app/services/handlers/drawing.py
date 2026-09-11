"""总图/门端图/侧板图/前端图/底架图 handlers."""

import re
from collections import Counter

import ijson

from app.services.handlers.common import (
    _COLOR_WORDS,
    _MM_RE,
    _NUM_RE,
    _RAL_RE,
    RuleOutcome,
    _find_comps,
    _thickness_set,
)

# 总图域 (5)
# ---------------------------------------------------------------------------


def h_tot_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    dims = manual.extract_dimensions()
    tol = float(rule.params.get("tolerance_mm", 6))
    evidence = [{"type": "doc", "side": "manual", "text": v} for v in dims.values()]
    required = ("external_L", "external_W", "external_H", "internal_L", "internal_W", "internal_H", "cubic_capacity")
    if any(not dims.get(key) for key in required):
        return RuleOutcome("warning", "说明书尺寸或容积取值不完整，无法与总图逐项比对", evidence)

    drawing_facts = _facts(ctx, "000A22G1G", "dimension")
    matched: dict[str, tuple[float, dict]] = {}
    for key in required[:-1]:
        manual_value = float(_NUM_RE.findall(dims[key])[0].replace(",", ""))
        hit = next(
            (
                (float(value), fact)
                for fact in drawing_facts
                for value in fact.get("values") or []
                if abs(float(value) - manual_value) <= tol
            ),
            None,
        )
        if hit:
            matched[key] = hit
            evidence.append(_fact_evidence(ctx, "000A22G1G", hit[1]))
    missing = [key for key in required[:-1] if key not in matched]
    if missing:
        return RuleOutcome("warning", f"总图未找到可与说明书唯一对应的尺寸标注：{missing}", evidence)

    manual_capacity = float(_NUM_RE.findall(dims["cubic_capacity"])[0])
    drawing_capacity = (
        matched["internal_L"][0] * matched["internal_W"][0] * matched["internal_H"][0] / 1_000_000_000
    )
    evidence.append({"type": "json", "side": "drawing", "text": f"依据总图内尺寸计算容积={drawing_capacity:.2f} cu.m"})
    if abs(drawing_capacity - manual_capacity) > float(rule.params.get("capacity_tolerance_m3", 0.2)):
        return RuleOutcome("fail", f"总图计算容积 {drawing_capacity:.2f} cu.m 与说明书 {manual_capacity:g} cu.m 不一致", evidence)
    return RuleOutcome("pass", f"总图六项尺寸及计算容积与说明书一致（尺寸容差 {tol:g}mm）", evidence)


def h_tot_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    ratings = manual.extract_ratings()
    evidence = [{"type": "doc", "side": "manual", "text": f"说明书额定值: {ratings}"}]
    drawing_texts = ctx.get_pdf_texts("000A22G1G")
    drawing_text = "\n".join(drawing_texts)
    patterns = {
        "max_gross_kg": r"(?:MAX\s+GROSS\s+WEIGHT|最大总重|总重)\s*([\d,]+)\s*KGS?",
        "tare_weight_kg": r"(?:TARE\s+WEIGHT|净重|自重)\s*([\d,]+)\s*KGS?",
        "payload_kg": r"(?:MAX\s+PAY\s*LOAD|最大载重|载重)\s*([\d,]+)\s*KGS?",
        "stacking_test_kg": r"(?:STACKING\s+TEST\s+LOAD|堆码(?:试验)?载荷)\s*([\d,]+)\s*KGS?",
        "floor_strength_kg": r"(?:FLOOR\s+STRENGTH|地板强度)\s*([\d,]+)\s*KGS?",
    }
    drawing_ratings = {
        key: float(match.group(1).replace(",", ""))
        for key, pattern in patterns.items()
        if (match := re.search(pattern, drawing_text, re.IGNORECASE))
    }
    if drawing_ratings:
        evidence.append({
            "type": "pdf",
            "side": "drawing",
            "file_id": ctx.pdf_file_ids.get("000A22G1G", ""),
            "page": 1,
            "text": f"总图额定值: {drawing_ratings}",
        })
    tol_pct = float(rule.params.get("tolerance_pct", 2))
    required = tuple(patterns)
    missing = [key for key in required if key not in ratings or key not in drawing_ratings]
    if missing:
        return RuleOutcome("warning", f"说明书或总图缺少可比对的额定值：{missing}", evidence)
    mismatches = [
        f"{key} 总图 {drawing_ratings[key]:g} vs 说明书 {ratings[key]:g}kg"
        for key in required
        if abs(drawing_ratings[key] - ratings[key]) / ratings[key] * 100 > tol_pct
    ]
    if mismatches:
        return RuleOutcome("fail", "；".join(mismatches), evidence)
    return RuleOutcome("pass", f"总图净重、总重、载重、堆码试验载荷及地板强度与说明书一致（容差 {tol_pct:g}%）", evidence)


def h_tot_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    evidence: list[dict] = []
    checkpoint_verdicts: list[str] = []

    def add(checkpoint: str, verdict: str, side: str, text: str, code: str = "") -> None:
        item = {
            "type": "doc" if side == "manual" else "json",
            "side": side,
            "checkpoint": checkpoint,
            "checkpoint_verdict": verdict,
            "text": text,
        }
        if code:
            item["file_id"] = ctx.json_file_ids.get(code, "")
        evidence.append(item)

    def parts(code: str, pattern: str) -> list[dict]:
        return [
            comp
            for comp in ctx.spatial.get(code, [])
            if re.fullmatch(pattern, comp.get("name", "").rsplit("/", 1)[-1])
        ]

    def centers(comps: list[dict]) -> list[list[float]]:
        return [
            [round(float(value), 3) for value in center]
            for comp in comps
            if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
        ]

    def bom(code: str, *tokens: str) -> dict | None:
        return next(
            (
                fact
                for fact in _facts(ctx, code, "bom")
                if any(
                    token.lower()
                    in " ".join(
                        str(fact.get(key) or "")
                        for key in ("description", "drawing_no", "text")
                    ).lower()
                    for token in tokens
                )
            ),
            None,
        )

    # 锁杆：说明书要求每扇门 2 根；BOM 的 1 是整套锁杆装置，实际杆数取总图 spatial。
    checkpoint = "锁杆数量与门扇分布"
    rods = parts("000A22G1G", r"GP锁杆部件-\d+")
    rod_centers = centers(rods)
    rod_sides = Counter("left" if point[2] < 0 else "right" for point in rod_centers)
    rod_ok = len(rods) == 4 and rod_sides == {"left": 2, "right": 2}
    rod_verdict = "pass" if manual and rod_ok else "fail" if rods else "warning"
    checkpoint_verdicts.append(rod_verdict)
    rod_basis = manual.find_around("Two locking bars", 180) if manual else ""
    add(
        checkpoint,
        rod_verdict,
        "manual",
        f"说明书要求：{rod_basis}" if rod_basis else "说明书未提取到每扇门 2 根锁杆的条款",
    )
    locking_bom = bom("000A22G1E", "LOCKING DEVICE", "E500001")
    add(
        checkpoint,
        rod_verdict,
        "drawing",
        (
            f"门端图 BOM：锁杆装置数量 {locking_bom.get('quantity')}（整套模块，不作为单根锁杆数量）；"
            if locking_bom
            else "门端图 BOM 未找到锁杆装置；"
        )
        + f"总图实际锁杆 {len(rods)} 根；中心坐标(mm)={rod_centers}",
        "000A22G1G",
    )

    # 通风器：每块侧板 1 个；左右侧板各一个，且坐标位于相反纵向端部。
    checkpoint = "通风器数量、侧板覆盖和端部位置"
    vents_g = parts("000A22G1G", r"S050001_通风器-\d+")
    vents_s = parts("000A22G1S", r"S050001_通风器-\d+")
    vent_centers = centers(vents_g)
    vent_names = [comp.get("name", "") for comp in vents_s]
    side_covered = any("左侧板" in name for name in vent_names) and any("右侧板" in name for name in vent_names)
    opposite_ends = len(vent_centers) == 2 and abs(vent_centers[0][0] - vent_centers[1][0]) > 3000
    vent_bom = bom("000A22G1S", "VENTILATOR", "S050001")
    vent_bom_qty = int(vent_bom.get("quantity") or 0) if vent_bom else 0
    vent_ok = len(vents_g) == len(vents_s) == vent_bom_qty == 2 and side_covered and opposite_ends
    vent_verdict = "pass" if manual and vent_ok else "fail" if vents_g or vents_s else "warning"
    checkpoint_verdicts.append(vent_verdict)
    vent_basis = manual.find_around("One ventilator", 220) if manual else ""
    add(
        checkpoint,
        vent_verdict,
        "manual",
        f"说明书要求：{vent_basis}" if vent_basis else "说明书未提取到每块侧板 1 个通风器及右端位置条款",
    )
    add(
        checkpoint,
        vent_verdict,
        "drawing",
        f"侧板图 BOM 通风器数量 {vent_bom_qty or '未找到'}；总图 {len(vents_g)} 个、侧板图 {len(vents_s)} 个；"
        f"左右侧板覆盖={side_covered}；相反纵向端部={opposite_ends}；中心坐标(mm)={vent_centers}",
        "000A22G1S",
    )

    # 地板钉数量与每组模式：说明书只规定端排 6 颗、其余 4 颗；BOM 总数为 200。
    checkpoint = "地板钉总数与每组 4/6 颗模式"
    screws = parts("000A22G1B", r"J090001_自攻螺钉-\d+")
    group_counts = Counter(
        comp.get("parent_id", "") for comp in screws if comp.get("parent_id")
    )
    pattern_counts = Counter(group_counts.values())
    screw_bom = bom("000A22G1B", "SELF-TAPPING SCREW", "J090001")
    screw_bom_qty = int(screw_bom.get("quantity") or 0) if screw_bom else 0
    screw_ok = (
        len(screws) == screw_bom_qty == 200
        and set(pattern_counts) == {4, 6}
        and pattern_counts[4] == 32
        and pattern_counts[6] == 12
    )
    screw_verdict = "pass" if manual and screw_ok else "fail" if screws else "warning"
    checkpoint_verdicts.append(screw_verdict)
    screw_basis = manual.find_around("Screws’ Qty.", 140) if manual else ""
    add(
        checkpoint,
        screw_verdict,
        "manual",
        f"说明书要求：{screw_basis}" if screw_basis else "说明书未提取到端排 6 颗、其他排 4 颗条款",
    )
    add(
        checkpoint,
        screw_verdict,
        "drawing",
        f"底架图 BOM 自攻螺钉数量 {screw_bom_qty or '未找到'}；spatial 实际 {len(screws)} 颗；"
        f"共 {len(group_counts)} 组，4 颗组 {pattern_counts[4]} 个，6 颗组 {pattern_counts[6]} 个",
        "000A22G1B",
    )

    # 纵向间距：保留实际排布证据，但说明书没有标准值，不能据图纸自身反证图纸正确。
    checkpoint = "地板钉纵向标准间距"
    spacing_verdict = "warning"
    checkpoint_verdicts.append(spacing_verdict)
    x_stations = sorted({round(point[0], 3) for point in centers(screws)})
    x_gaps = Counter(round(right - left, 3) for left, right in zip(x_stations, x_stations[1:]))
    screw_dimensions = [
        fact
        for fact in _facts(ctx, "000A22G1B", "dimension")
        if _fact_matches(fact, "J090001_自攻螺钉", "地板钉")
    ]
    actual_dimensions = sorted(
        {
            f"{fact.get('text') or ''}{'/'.join(f'{value:g}' for value in fact.get('values') or [])}"
            for fact in screw_dimensions
            if fact.get("text") or fact.get("values")
        }
    )
    add(
        checkpoint,
        spacing_verdict,
        "manual",
        "说明书只规定端排 6 颗、其他排 4 颗，未提供地板钉纵向标准间距(mm)",
    )
    add(
        checkpoint,
        spacing_verdict,
        "drawing",
        f"底架图实际标注={actual_dimensions}；spatial 共 {len(x_stations)} 个纵向站位；相邻站位差(mm)={dict(x_gaps)}。"
        "以上仅为实际排布，缺少标准值，暂不判定正确性",
        "000A22G1B",
    )

    verdict = "fail" if "fail" in checkpoint_verdicts else "warning" if "warning" in checkpoint_verdicts else "pass"
    return RuleOutcome(
        verdict,
        "锁杆、通风器及地板钉数量/4或6颗模式已分别核对；地板钉纵向标准间距因说明书未规定而待确认",
        evidence,
    )


def h_tot_04(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    g_comps = _find_comps(ctx, "000A22G1G", "板", "梁")
    thickness_set = _thickness_set(g_comps)
    evidence: list[dict] = []
    requirements = ctx.fact_bundle.for_rule("TOT-04") if ctx.fact_bundle else []
    if not requirements:
        customer_text = "；".join(
            str(ctx.inputs.get(key) or "") for key in ("tech_req", "new_material")
        )
        requirements = [
            {
                "source_text": match.group(0),
                "expected_value": float(match.group(1)),
                "object": "",
                "id": f"fallback:{index}",
            }
            for index, match in enumerate(
                re.finditer(
                    r"(?:板厚|厚度|厚)\D{0,12}(\d+(?:\.\d+)?)\s*mm",
                    customer_text,
                    re.IGNORECASE,
                ),
                1,
            )
        ]
    if not requirements:
        return RuleOutcome("warning", "客户要求中未提取到钣金厚度检查点", [])

    verdicts: list[str] = []
    for requirement in requirements:
        if isinstance(requirement, dict):
            requirement_id = requirement.get("id")
            source_text = requirement.get("source_text")
            expected_raw = requirement.get("expected_value")
            object_name = requirement.get("object")
        else:
            requirement_id = requirement.id
            source_text = requirement.source_text
            expected_raw = requirement.expected_value
            object_name = requirement.object
        try:
            expected = float(expected_raw)
        except (TypeError, ValueError):
            verdicts.append("warning")
            evidence.append({
                "type": "input",
                "checkpoint": source_text,
                "checkpoint_verdict": "warning",
                "text": f"客户原文={source_text}；未提取到明确厚度数值",
            })
            continue

        resolution = ctx.fact_bundle.resolution_for(requirement_id) if ctx.fact_bundle else None
        if object_name and (not resolution or resolution.status != "resolved"):
            verdict = "warning"
            candidates = resolution.candidate_labels if resolution else []
            actual: list[float] = []
            actual_text = f"对象“{object_name}”无法唯一映射；候选={candidates or '无'}"
        elif resolution:
            actual = [
                float(item.value)
                for item in ctx.fact_bundle.observations_for(requirement_id, "thickness")
            ]
            verdict = (
                "warning"
                if not actual
                else "pass"
                if any(abs(expected - value) < 0.01 for value in actual)
                else "fail"
            )
            actual_text = f"映射={resolution.label}；spatial 厚度={actual or '未提取'}mm"
        else:
            actual = sorted(thickness_set)
            verdict = (
                "warning"
                if not actual
                else "pass"
                if any(abs(expected - value) < 0.01 for value in actual)
                else "fail"
            )
            actual_text = f"未指定对象；总图钣金厚度集合={actual or '未提取'}mm"
        verdicts.append(verdict)
        evidence.extend([
            {
                "type": "input",
                "checkpoint": source_text,
                "checkpoint_verdict": verdict,
                "text": f"客户原文={source_text}；期望厚度={expected:g}mm",
            },
            {
                "type": "json",
                "side": "drawing",
                "file_id": ctx.json_file_ids.get("000A22G1G", ""),
                "checkpoint": source_text,
                "checkpoint_verdict": verdict,
                "text": actual_text,
            },
        ])
    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    passed = sum(item == "pass" for item in verdicts)
    return RuleOutcome(verdict, f"客户钣金厚度要求已核验 {passed}/{len(verdicts)} 项", evidence)


def h_tot_05(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    evidence: list[dict] = []
    verdicts: list[str] = []
    checks = (
        ("前角柱拉筋排布与规格", "MF060101_前端拉筋模块", "F060002", "000A22G1F", "Lashing rods on the corner posts"),
        ("后角柱拉筋排布与规格", "ME060101_门端拉筋模块", "E020001", "000A22G1E", "Lashing rods on the corner posts"),
        ("顶侧梁绳环排布与规格", "MS050101_侧板绳环模块", "S060001", "000A22G1S", "Lashing rings on the side rails"),
        ("底侧梁绳环排布与规格", "MB060101_底架绳环模块", "S060001", "000A22G1B", "Lashing rings on the side rails"),
    )

    for checkpoint, module_name, part_code, drawing_code, manual_keyword in checks:
        comps = [
            comp
            for comp in ctx.spatial.get("000A22G1G", [])
            if comp.get("leaf")
            and module_name in comp.get("name", "")
            and comp.get("name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_")
        ]
        points = [
            [round(float(value), 3) for value in center]
            for comp in comps
            if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
        ]
        left = sorted((round(x, 1), round(y, 1), round(abs(z), 1)) for x, y, z in points if z < 0)
        right = sorted((round(x, 1), round(y, 1), round(abs(z), 1)) for x, y, z in points if z > 0)
        symmetric = bool(left) and left == right
        materials = sorted({comp.get("material_name", "") for comp in comps if comp.get("material_name")})
        actual_code_ok = bool(comps) and all(
            comp.get("name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_") for comp in comps
        )
        material_ok = bool(materials) and all("SS400" in material or "Q235A" in material for material in materials)

        bom_row = next(
            (fact for fact in _facts(ctx, drawing_code, "bom") if fact.get("drawing_no") == part_code),
            None,
        )
        try:
            bom_quantity = int(float(str(bom_row.get("quantity") or 0))) if bom_row else 0
        except ValueError:
            bom_quantity = 0
        manual_basis = manual.find_around(manual_keyword, 220) if manual else ""

        if bom_row and comps and (
            len(comps) != bom_quantity or not symmetric or not actual_code_ok or not material_ok
        ):
            verdict = "fail"
        elif not bom_row or not comps or len(points) != len(comps) or not manual_basis:
            verdict = "warning"
        else:
            verdict = "pass"
        verdicts.append(verdict)

        evidence.append(
            {
                "type": "doc",
                "side": "manual",
                "checkpoint": checkpoint,
                "checkpoint_verdict": verdict,
                "text": f"说明书要求：{manual_basis}" if manual_basis else f"说明书未提取到 {manual_keyword} 条款",
            }
        )
        bom_text = (
            f"{drawing_code} BOM：图号 {part_code}；名称 {bom_row.get('description') or ''}；"
            f"规格 {bom_row.get('specification') or ''}；材质/表面处理 {bom_row.get('material') or ''}；"
            f"数量 {bom_quantity}"
            if bom_row
            else f"{drawing_code} BOM 未找到图号 {part_code}"
        )
        if bom_row:
            bom_evidence = _fact_evidence(ctx, drawing_code, bom_row)
            bom_evidence.update(
                checkpoint=checkpoint,
                checkpoint_verdict=verdict,
                text=bom_text,
            )
            evidence.append(bom_evidence)
        else:
            evidence.append(
                {
                    "type": "json",
                    "side": "drawing",
                    "checkpoint": checkpoint,
                    "checkpoint_verdict": verdict,
                    "file_id": ctx.json_file_ids.get(drawing_code, ""),
                    "text": bom_text,
                }
            )

        drawing_anchor = next(
            (
                fact
                for kind in ("dimension", "note", "weld")
                for fact in _facts(ctx, drawing_code, kind)
                if fact.get("rect") and _fact_matches(fact, part_code)
            ),
            None,
        )
        if drawing_anchor:
            anchor_evidence = _fact_evidence(ctx, drawing_code, drawing_anchor)
            anchor_evidence.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
            evidence.append(anchor_evidence)

        total_anchor = next(
            (
                fact
                for kind in ("dimension", "note", "weld")
                for fact in _facts(ctx, "000A22G1G", kind)
                if fact.get("rect") and _fact_matches(fact, part_code)
            ),
            None,
        )
        if total_anchor:
            total_evidence = _fact_evidence(ctx, "000A22G1G", total_anchor)
            total_evidence.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
            evidence.append(total_evidence)
        evidence.append(
            {
                "type": "json",
                "side": "drawing",
                "checkpoint": checkpoint,
                "checkpoint_verdict": verdict,
                "file_id": ctx.json_file_ids.get("000A22G1G", ""),
                "text": (
                    f"总图实际叶子零件 {len(comps)} 件；图号匹配={actual_code_ok}；材质={materials}；"
                    f"左右对称={symmetric}；中心坐标(mm)={points}"
                ),
            }
        )

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    passed = verdicts.count("pass")
    return RuleOutcome(
        verdict,
        f"前后角柱拉筋及顶、底侧梁绳环已分为 4 个审查点：{passed} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


# ---------------------------------------------------------------------------
# 门端图 (3) / 侧板图 (1) / 前端图 (2) / 底架图 (3)
# ---------------------------------------------------------------------------


def h_door_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    evidence: list[dict] = []
    verdicts: list[str] = []
    all_comps = ctx.spatial.get("000A22G1E", [])
    bom_rows = _facts(ctx, "000A22G1E", "bom")

    def parts(prefix: str, parent: str = "") -> list[dict]:
        return [
            comp
            for comp in all_comps
            if comp.get("leaf")
            and comp.get("name", "").rsplit("/", 1)[-1].startswith(prefix)
            and (not parent or parent in comp.get("name", ""))
        ]

    def points(comps: list[dict]) -> list[list[float]]:
        return [
            [round(float(value), 3) for value in center]
            for comp in comps
            if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
        ]

    def bom(drawing_no: str) -> dict | None:
        return next((row for row in bom_rows if row.get("drawing_no") == drawing_no), None)

    def quantity(row: dict | None) -> int:
        try:
            return int(float(str(row.get("quantity") or 0))) if row else 0
        except ValueError:
            return 0

    def add_manual(checkpoint: str, verdict: str, text: str) -> None:
        evidence.append(
            {
                "type": "doc",
                "side": "manual",
                "checkpoint": checkpoint,
                "checkpoint_verdict": verdict,
                "text": text,
            }
        )

    def add_bom(checkpoint: str, verdict: str, row: dict | None) -> None:
        if not row:
            evidence.append(
                {
                    "type": "json",
                    "side": "drawing",
                    "checkpoint": checkpoint,
                    "checkpoint_verdict": verdict,
                    "file_id": ctx.json_file_ids.get("000A22G1E", ""),
                    "text": "门端图 BOM 未找到对应行",
                }
            )
            return
        item = _fact_evidence(ctx, "000A22G1E", row)
        item.update(
            checkpoint=checkpoint,
            checkpoint_verdict=verdict,
            text=(
                f"门端图 BOM：序号 {row.get('item') or ''}；图号 {row.get('drawing_no') or ''}；"
                f"名称 {row.get('description') or ''}；规格 {row.get('specification') or ''}；"
                f"材质 {row.get('material') or ''}；数量 {row.get('quantity') or ''}"
            ),
        )
        evidence.append(item)

    def add_anchor(checkpoint: str, verdict: str, *part_codes: str) -> None:
        for part_code in part_codes:
            anchor = next(
                (
                    fact
                    for kind in ("dimension", "note", "weld")
                    for fact in _facts(ctx, "000A22G1E", kind)
                    if fact.get("rect") and _fact_matches(fact, part_code)
                ),
                None,
            )
            if anchor:
                item = _fact_evidence(ctx, "000A22G1E", anchor)
                item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
                evidence.append(item)

    def add_spatial(checkpoint: str, verdict: str, text: str) -> None:
        evidence.append(
            {
                "type": "json",
                "side": "drawing",
                "checkpoint": checkpoint,
                "checkpoint_verdict": verdict,
                "file_id": ctx.json_file_ids.get("000A22G1E", ""),
                "text": text,
            }
        )

    # 门铰链：每扇门 4 套；同时核对铰链本体、铰链销和铰链座。
    checkpoint = "门铰链选用"
    hinges = parts("E200008_门铰链-")
    pins = parts("E220001_铰链销-")
    butts = parts("E230001_门耳朵-")
    hinge_bom, pin_bom, butt_bom = bom("E200008"), bom("E220001"), bom("E230001")
    hinge_points = points(hinges)
    hinge_sides = Counter("left" if point[2] < 0 else "right" for point in hinge_points)
    hinge_materials = sorted({c.get("material_name", "") for c in hinges if c.get("material_name")})
    pin_materials = sorted({c.get("material_name", "") for c in pins if c.get("material_name")})
    hinge_basis = manual.find_around("6.5.4.2 Hinges and Pins", 300) if manual else ""
    hinge_ok = (
        len(hinges) == quantity(hinge_bom) == 8
        and len(pins) == quantity(pin_bom) == 8
        and len(butts) == quantity(butt_bom) == 16
        and hinge_sides == {"left": 4, "right": 4}
        and all("SS400" in value or "Q235A" in value for value in hinge_materials)
        and all("SUS304" in value for value in pin_materials)
    )
    hinge_verdict = "pass" if hinge_basis and hinge_ok else "fail" if hinges or pins or butts else "warning"
    verdicts.append(hinge_verdict)
    add_manual(
        checkpoint,
        hinge_verdict,
        f"说明书要求：{hinge_basis}" if hinge_basis else "说明书未提取到每扇门 4 套铰链及销的条款",
    )
    for row in (hinge_bom, pin_bom, butt_bom):
        add_bom(checkpoint, hinge_verdict, row)
    add_anchor(checkpoint, hinge_verdict, "E200008", "E220001", "E230001")
    add_spatial(
        checkpoint,
        hinge_verdict,
        f"门端图实际：门铰链 {len(hinges)} 件、铰链销 {len(pins)} 件、铰链座 {len(butts)} 件；"
        f"每扇门铰链 4 件={hinge_sides == {'left': 4, 'right': 4}}；"
        f"铰链材质={hinge_materials}；销材质={pin_materials}；铰链中心坐标(mm)={hinge_points}",
    )

    # 门绳：说明书规定每扇门 1 根混合尼龙绳。
    checkpoint = "门绳选用"
    ropes = parts("E310001_门绳-")
    rope_bom = bom("E310001")
    rope_points = points(ropes)
    rope_sides = Counter("left" if point[2] < 0 else "right" for point in rope_points)
    rope_materials = sorted({c.get("material_name", "") for c in ropes if c.get("material_name")})
    rope_basis = manual.find_around("A door holder per door", 220) if manual else ""
    rope_ok = (
        len(ropes) == quantity(rope_bom) == 2
        and rope_sides == {"left": 1, "right": 1}
        and all("尼龙" in value or "NYLON" in value.upper() for value in rope_materials)
    )
    rope_verdict = "pass" if rope_basis and rope_ok else "fail" if ropes else "warning"
    verdicts.append(rope_verdict)
    add_manual(
        checkpoint,
        rope_verdict,
        f"说明书要求：{rope_basis}" if rope_basis else "说明书未提取到每扇门 1 根混合尼龙门绳的条款",
    )
    add_bom(checkpoint, rope_verdict, rope_bom)
    add_anchor(checkpoint, rope_verdict, "E310001")
    add_spatial(
        checkpoint,
        rope_verdict,
        f"门端图实际门绳 {len(ropes)} 根；每扇门 1 根={rope_sides == {'left': 1, 'right': 1}}；"
        f"材质={rope_materials}；中心坐标(mm)={rope_points}",
    )

    # 门封铆钉：JSON 没有直接的门封语义，按说明书、BOM 和门框模块层级进行跨资料关联。
    checkpoint = "门封铆钉选用"
    rivets = parts(
        "GB-T12618_开口型平圆头抽芯铆钉_SUS304-钢_4.8x15",
        "ME020106_门框模块",
    )
    rivet_bom = bom("GB/T12618")
    rivet_points = points(rivets)
    rivet_materials = sorted({c.get("material_name", "") for c in rivets if c.get("material_name")})
    rivet_basis = manual.find_around("attached to the door frame with stainless steel rivets", 260) if manual else ""
    rivet_ok = (
        len(rivets) == quantity(rivet_bom) == 84
        and all("SUS304" in value for value in rivet_materials)
    )
    rivet_verdict = "pass" if rivet_basis and rivet_ok else "fail" if rivets else "warning"
    verdicts.append(rivet_verdict)
    add_manual(
        checkpoint,
        rivet_verdict,
        (
            f"跨资料关联依据：{rivet_basis}；门端图 BOM 与门框模块中同规格铆钉共同确认，"
            "JSON 未直接声明“门封铆钉”语义"
            if rivet_basis
            else "说明书未提取到门封使用不锈钢铆钉的条款"
        ),
    )
    add_bom(checkpoint, rivet_verdict, rivet_bom)
    add_anchor(checkpoint, rivet_verdict, "GB-T12618")
    add_spatial(
        checkpoint,
        rivet_verdict,
        f"门框模块下 GB/T12618 4.8×15 铆钉 {len(rivets)} 颗；材质={rivet_materials}；"
        f"跨资料关联成立={rivet_ok}；中心坐标数量={len(rivet_points)}。"
        "JSON 未提供门扇归属字段，不推断左右门扇各自数量",
    )

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"门铰链、门绳、门封铆钉已分为 3 个审查点：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_door_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1E"
    facts = _facts(ctx, code)
    evidence: list[dict] = []
    verdicts: list[str] = []

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        evidence.append({**item, "checkpoint": checkpoint, "checkpoint_verdict": verdict})

    def add_bom(checkpoint: str, verdict: str, fact: dict | None) -> None:
        if not fact:
            return
        add(checkpoint, verdict, {
            "type": "pdf",
            "side": "drawing",
            "file_id": ctx.pdf_file_ids.get(code, ""),
            "page": fact.get("page", 1),
            "rect": fact.get("rect"),
            "precision": "approximate",
            "text": (
                f"门端图 BOM：序号 {fact.get('item')}；图号 {fact.get('drawing_no')}；"
                f"名称 {fact.get('description')}；材质 {fact.get('material')}；数量 {fact.get('quantity')}"
            ),
        })

    def add_anchor(checkpoint: str, verdict: str, part_code: str, text: str) -> None:
        fact = next(
            (
                item for item in facts
                if item.get("kind") == "note"
                and str(item.get("text", "")).strip().upper() == text.upper()
                and part_code in _attachment_text(item)
            ),
            None,
        )
        if fact:
            add(checkpoint, verdict, _fact_evidence(ctx, code, fact))

    bom = {item.get("drawing_no"): item for item in facts if item.get("kind") == "bom"}
    manual_basis = ctx.manual.find_around("6.5.4.5 Seal Gaskets", 340) if ctx.manual else ""

    corner_values: set[str] = set()
    api_path = ctx.api_paths.get(code, "")
    if api_path:
        with open(api_path, "rb") as handle:
            for component in ijson.items(handle, "referencedComponents.item"):
                if "E100007" not in str(component.get("name", "")):
                    continue
                corner_values.update(
                    str(prop.get("resolvedValue", "") or "").strip()
                    for prop in component.get("customProperties", [])
                    if prop.get("name") in {"备注", "查重", "识别特征信息"} and prop.get("resolvedValue")
                )
                if corner_values:
                    break
    corner_text = "；".join(sorted(corner_values))
    if "无包角" in corner_text:
        corner_verdict = "fail"
    elif "包角" in corner_text:
        corner_verdict = "pass"
    else:
        corner_verdict = "warning"
    verdicts.append(corner_verdict)
    checkpoint = "门封胶条包角"
    add(checkpoint, corner_verdict, {"type": "input", "side": "manual", "text": "审查要求：门封胶条应有包角"})
    add(checkpoint, corner_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": f"E100007 门封胶条属性：{corner_text or '未提取到包角信息'}",
    })
    add_bom(checkpoint, corner_verdict, bom.get("E100007"))
    add_anchor(checkpoint, corner_verdict, "E100007", "12")
    add_anchor(checkpoint, corner_verdict, "E100007", "SEALING")

    retainers = [
        comp for comp in ctx.spatial.get(code, [])
        if comp.get("leaf") and ("E110001" in comp.get("name", "") or "E120004" in comp.get("name", ""))
    ]
    retainer_boms = [bom.get("E110001"), bom.get("E120004")]
    bom_ok = all(item and str(item.get("material", "")).upper() == "ABS" for item in retainer_boms)
    quantities_ok = [str(item.get("quantity")) if item else "" for item in retainer_boms] == ["4", "3"]
    spatial_ok = len(retainers) == 7 and all(
        str(comp.get("material_name", "")).upper() == "ABS" for comp in retainers
    )
    if not all(retainer_boms) or not retainers:
        abs_verdict = "warning"
    elif bom_ok and quantities_ok and spatial_ok:
        abs_verdict = "pass"
    else:
        abs_verdict = "fail"
    verdicts.append(abs_verdict)
    checkpoint = "门封压条 ABS 材质"
    add(checkpoint, abs_verdict, {"type": "input", "side": "manual", "text": "审查要求：门封压条材质为 ABS"})
    if manual_basis:
        add(checkpoint, abs_verdict, {"type": "doc", "side": "manual", "text": f"说明书要求：{manual_basis}"})
    for item in retainer_boms:
        add_bom(checkpoint, abs_verdict, item)
    add_anchor(checkpoint, abs_verdict, "E110001", "13")
    add_anchor(checkpoint, abs_verdict, "E120004", "14")
    add(checkpoint, abs_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": f"门封压条实际 7 件（横向 4、竖向 3）；材质={sorted({c.get('material_name') for c in retainers})}",
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"门封胶条包角、门封压条 ABS 材质已分为 2 个审查点：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_door_06(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1E"
    comps = ctx.spatial.get(code, [])
    bom_rows = _facts(ctx, code, "bom")
    evidence: list[dict] = []
    verdicts: list[str] = []

    def segment(comp: dict) -> str:
        return comp.get("name", "").rsplit("/", 1)[-1]

    def bom(drawing_no: str) -> dict | None:
        return next((row for row in bom_rows if row.get("drawing_no") == drawing_no), None)

    def quantity(row: dict | None) -> int:
        try:
            return int(float(str(row.get("quantity") or 0))) if row else 0
        except ValueError:
            return 0

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    def add_bom(checkpoint: str, verdict: str, row: dict | None) -> None:
        if not row:
            return
        item = _fact_evidence(ctx, code, row)
        item["text"] = (
            f"门端图 BOM：序号 {row.get('item') or ''}；图号 {row.get('drawing_no') or ''}；"
            f"名称 {row.get('description') or ''}；规格 {row.get('specification') or ''}；"
            f"材质 {row.get('material') or ''}；数量 {row.get('quantity') or ''}"
        )
        add(checkpoint, verdict, item)

    def add_anchors(checkpoint: str, verdict: str, *tokens: str) -> None:
        anchors = [
            fact
            for kind in ("note", "dimension")
            for fact in _facts(ctx, code, kind)
            if fact.get("rect") and _fact_matches(fact, *tokens)
        ]
        for fact in anchors[:2]:
            item = _fact_evidence(ctx, code, fact)
            if fact.get("kind") == "dimension" and fact.get("values"):
                value = round(float(fact["values"][0]), 3)
                item["text"] = f"{fact.get('view')}: 图面尺寸 {value:g} mm；绑定={_attachment_text(fact)}"
            add(checkpoint, verdict, item)

    # 只看精确节点名，避免把“锁杆模块”路径下的所有后代都统计为锁杆。
    checkpoint = "锁杆整套或散件"
    lock_bom = bom("E500001")
    lock_modules = [
        comp for comp in comps
        if not comp.get("leaf") and segment(comp).startswith("ME080101_锁杆模块-")
    ]
    lock_rod_assemblies = [
        comp for comp in comps
        if not comp.get("leaf")
        and segment(comp).startswith("GP锁杆部件-")
        and "ME080101_锁杆模块" in comp.get("name", "")
    ]
    lock_ok = (
        len(lock_modules) == 1
        and bool(lock_rod_assemblies)
        and quantity(lock_bom) == 1
        and "LOCKING DEVICE" in str((lock_bom or {}).get("description", "")).upper()
    )
    lock_verdict = "pass" if lock_ok else "fail" if lock_bom or lock_modules or lock_rod_assemblies else "warning"
    verdicts.append(lock_verdict)
    add(checkpoint, lock_verdict, {
        "type": "input",
        "side": "manual",
        "text": (
            "判定规则：BOM 以 LOCKING DEVICE / E500001 / 数量 1 列项，且空间树存在独立的非叶子锁杆模块并包含锁杆子装配，"
            "判为整套；只有散件叶子且不存在总模块时判为散件"
        ),
    })
    add_bom(checkpoint, lock_verdict, lock_bom)
    add_anchors(checkpoint, lock_verdict, "GP锁杆")
    add(checkpoint, lock_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"空间树实际：锁杆总模块 {len(lock_modules)} 个，直属 GP 锁杆子装配 {len(lock_rod_assemblies)} 个；"
            f"装配形式={'整套' if lock_ok else '散件或结构不完整'}。未统计锁杆模块路径下的普通后代零件"
        ),
    })

    checkpoint = "后角柱拉筋排布"
    brace_bom = bom("E020001")
    braces = [
        comp for comp in comps
        if comp.get("leaf")
        and segment(comp).startswith("E020001_门端拉筋-")
        and "ME060101_门端拉筋模块" in comp.get("name", "")
    ]
    points = sorted(
        (
            [round(float(value), 3) for value in center]
            for comp in braces
            if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
        ),
        key=lambda point: (point[2], point[1]),
    )
    negative = sorted((point for point in points if point[2] < 0), key=lambda point: point[1])
    positive = sorted((point for point in points if point[2] > 0), key=lambda point: point[1])
    symmetric = (
        len(negative) == len(positive) == 3
        and all(abs(left[0] - right[0]) <= 1 for left, right in zip(negative, positive, strict=True))
        and all(abs(left[1] - right[1]) <= 1 for left, right in zip(negative, positive, strict=True))
        and all(abs(abs(left[2]) - abs(right[2])) <= 1 for left, right in zip(negative, positive, strict=True))
        and len({round(point[1], 1) for point in negative}) == 3
    )
    specification = str((brace_bom or {}).get("specification", ""))
    normalized_spec = specification.upper().replace("Φ", "").replace("φ", "").replace("×", "X").replace(" ", "")
    brace_ok = len(braces) == quantity(brace_bom) == 6 and len(points) == 6 and symmetric and "12X58" in normalized_spec
    brace_verdict = "pass" if brace_ok else "fail" if brace_bom or braces else "warning"
    verdicts.append(brace_verdict)
    add(checkpoint, brace_verdict, {
        "type": "input",
        "side": "manual",
        "text": "判定规则：E020001 实际数量须与 BOM 一致，规格为 Φ12×58，并且门端左右各 3 件、上中下高度一一对应",
    })
    add_bom(checkpoint, brace_verdict, brace_bom)
    add_anchors(checkpoint, brace_verdict, "E020001")
    add(checkpoint, brace_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"空间树实际：E020001 门端拉筋 {len(braces)} 件；负 Z 侧 {len(negative)} 件、正 Z 侧 {len(positive)} 件；"
            f"左右上中下对称={symmetric}；中心坐标(mm)={points}"
        ),
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"锁杆装配形式、后角柱拉筋排布已分为 2 个审查点：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_side_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    side_code, general_code = "000A22G1S", "000A22G1G"
    side_comps = ctx.spatial.get(side_code, [])
    general_comps = ctx.spatial.get(general_code, [])
    side_facts = _facts(ctx, side_code)
    evidence: list[dict] = []
    verdicts: list[str] = []

    def segment(comp: dict) -> str:
        return comp.get("name", "").rsplit("/", 1)[-1]

    def parts(comps: list[dict], prefix: str, parent: str) -> list[dict]:
        return [
            comp for comp in comps
            if comp.get("leaf") and segment(comp).startswith(prefix) and parent in comp.get("name", "")
        ]

    def points(comps: list[dict]) -> list[list[float]]:
        return sorted(
            (
                [round(float(value), 1) for value in center]
                for comp in comps
                if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
            ),
            key=lambda point: (point[2], point[0], point[1]),
        )

    def bom(drawing_no: str) -> dict | None:
        return next(
            (fact for fact in side_facts if fact.get("kind") == "bom" and fact.get("drawing_no") == drawing_no),
            None,
        )

    def quantity(row: dict | None) -> int:
        try:
            return int(float(str(row.get("quantity") or 0))) if row else 0
        except ValueError:
            return 0

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    def add_bom(checkpoint: str, verdict: str, row: dict | None) -> None:
        if not row:
            return
        item = _fact_evidence(ctx, side_code, row)
        item["text"] = (
            f"侧板图 BOM：序号 {row.get('item') or ''}；图号 {row.get('drawing_no') or ''}；"
            f"名称 {row.get('description') or ''}；规格 {row.get('specification') or ''}；"
            f"材质 {row.get('material') or ''}；数量 {row.get('quantity') or ''}"
        )
        add(checkpoint, verdict, item)

    def add_fact(checkpoint: str, verdict: str, fact: dict | None) -> None:
        if not fact:
            return
        item = _fact_evidence(ctx, side_code, fact)
        if fact.get("kind") == "dimension" and fact.get("values"):
            value = round(float(fact["values"][0]), 3)
            item["text"] = f"{fact.get('view')}: 图面尺寸 {value:g} mm；绑定={_attachment_text(fact)}"
        add(checkpoint, verdict, item)

    ring_bom, vent_bom = bom("S060001"), bom("S050001")
    side_rings = parts(side_comps, "S060001_绳钩-", "MS050101_侧板绳环模块")
    general_rings = parts(general_comps, "S060001_绳钩-", "MS050101_侧板绳环模块")
    side_vents = parts(side_comps, "S050001_通风器-", "MS040101_通风器模块")
    general_vents = parts(general_comps, "S050001_通风器-", "MS040101_通风器模块")
    side_ring_points, general_ring_points = points(side_rings), points(general_rings)
    side_vent_points, general_vent_points = points(side_vents), points(general_vents)

    manual_lines = ctx.manual.full_text.splitlines() if ctx.manual else []
    ring_basis = next((line.strip() for line in manual_lines if "Lashing ring Qty." in line), "")
    ring_match = re.search(
        r"Each bottom or top side rail:\s*(\d+)\s*,\s*Total:\s*(\d+)",
        ring_basis,
        re.IGNORECASE,
    )
    expected_per_top_side = int(ring_match.group(1)) if ring_match else 0
    side_ring_counts = Counter("negative_z" if point[2] < 0 else "positive_z" for point in side_ring_points)
    ring_spec = str((ring_bom or {}).get("specification", ""))
    ring_spec = ring_spec.upper().replace("Φ", "").replace("φ", "").replace("×", "X").replace(" ", "")
    ring_ok = (
        expected_per_top_side == 5
        and quantity(ring_bom) == len(side_rings) == len(general_rings) == 10
        and side_ring_counts == {"negative_z": 5, "positive_z": 5}
        and side_ring_points == general_ring_points
        and "12X52X64" in ring_spec
    )
    ring_missing = not ring_match or not ring_bom or side_code not in ctx.spatial or general_code not in ctx.spatial
    ring_verdict = "warning" if ring_missing else "pass" if ring_ok else "fail"
    verdicts.append(ring_verdict)
    checkpoint = "顶侧梁绳环排布与数量"
    add(checkpoint, ring_verdict, {
        "type": "doc",
        "side": "manual",
        "text": f"说明书要求：{ring_basis}" if ring_basis else "说明书未提取到顶侧梁绳环数量要求",
    })
    add_bom(checkpoint, ring_verdict, ring_bom)
    ring_note = next(
        (fact for fact in side_facts if fact.get("kind") == "note" and str(fact.get("text", "")).strip() == "6" and _fact_matches(fact, "S060001")),
        None,
    )
    ring_dimension = next(
        (
            fact for fact in side_facts
            if fact.get("kind") == "dimension"
            and all(token in _attachment_text(fact) for token in ("S060001", "S040101"))
            and any(abs(float(value) - 11) <= 0.1 for value in fact.get("values", []))
        ),
        None,
    )
    add_fact(checkpoint, ring_verdict, ring_note)
    add_fact(checkpoint, ring_verdict, ring_dimension)
    add(checkpoint, ring_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(side_code, ""),
        "text": (
            f"侧板图实际：顶侧梁 S060001 绳环 {len(side_rings)} 个，负 Z 侧 {side_ring_counts['negative_z']} 个、"
            f"正 Z 侧 {side_ring_counts['positive_z']} 个；中心坐标(mm)={side_ring_points}"
        ),
    })
    add(checkpoint, ring_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(general_code, ""),
        "text": f"总图复核：同一顶侧梁绳环 {len(general_rings)} 个；与侧板图数量及坐标一致={side_ring_points == general_ring_points}",
    })

    vent_basis = next((line.strip() for line in manual_lines if "One ventilator" in line), "")
    vent_requirement = bool(re.search(r"One ventilator.+each side wall.+right-hand end", vent_basis, re.IGNORECASE | re.DOTALL))
    vent_side_counts = Counter("negative_z" if point[2] < 0 else "positive_z" for point in side_vent_points)
    vent_spec = str((vent_bom or {}).get("specification", ""))
    vent_spec = vent_spec.upper().replace("×", "X").replace(" ", "")
    ring_xs = [point[0] for point in side_ring_points]
    negative_vent = next((point for point in side_vent_points if point[2] < 0), None)
    positive_vent = next((point for point in side_vent_points if point[2] > 0), None)
    mirrored_ends = bool(
        ring_xs and negative_vent and positive_vent
        and abs(negative_vent[0] - min(ring_xs)) <= 1000
        and abs(positive_vent[0] - max(ring_xs)) <= 1000
    )
    vent_ok = (
        vent_requirement
        and quantity(vent_bom) == len(side_vents) == len(general_vents) == 2
        and vent_side_counts == {"negative_z": 1, "positive_z": 1}
        and side_vent_points == general_vent_points
        and mirrored_ends
        and "26X67X205" in vent_spec
    )
    vent_missing = not vent_requirement or not vent_bom or side_code not in ctx.spatial or general_code not in ctx.spatial
    vent_verdict = "warning" if vent_missing else "pass" if vent_ok else "fail"
    verdicts.append(vent_verdict)
    checkpoint = "通风器排布与数量"
    add(checkpoint, vent_verdict, {
        "type": "doc",
        "side": "manual",
        "text": f"说明书要求：{vent_basis}" if vent_basis else "说明书未提取到通风器数量及端部位置要求",
    })
    add_bom(checkpoint, vent_verdict, vent_bom)
    vent_facts = [
        fact for fact in side_facts
        if fact.get("rect") and _fact_matches(fact, "S050001") and (
            (fact.get("kind") == "note" and str(fact.get("text", "")).strip() in {"5", "VENTILATOR"})
            or (fact.get("kind") == "dimension" and any(abs(float(value) - target) <= 0.1 for value in fact.get("values", []) for target in (67, 205)))
        )
    ]
    for fact in vent_facts[:4]:
        add_fact(checkpoint, vent_verdict, fact)
    add(checkpoint, vent_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(side_code, ""),
        "text": (
            f"侧板图实际：S050001 通风器 {len(side_vents)} 个，每侧墙 1 个={vent_side_counts == {'negative_z': 1, 'positive_z': 1}}；"
            f"左右右端镜像排布={mirrored_ends}；中心坐标(mm)={side_vent_points}"
        ),
    })
    add(checkpoint, vent_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(general_code, ""),
        "text": f"总图复核：同一通风器 {len(general_vents)} 个；与侧板图数量及坐标一致={side_vent_points == general_vent_points}",
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"顶侧梁绳环、通风器已分为 2 个审查点：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_front_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1F"
    comps = ctx.spatial.get(code, [])
    facts = _facts(ctx, code)
    bom_rows = [fact for fact in facts if fact.get("kind") == "bom"]
    evidence: list[dict] = []
    verdicts: list[str] = []

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    def quantity(row: dict | None) -> int:
        try:
            return int(float(str(row.get("quantity") or 0))) if row else 0
        except ValueError:
            return 0

    def spec(row: dict | None) -> float | None:
        match = _NUM_RE.search(str(row.get("specification") or "")) if row else None
        return float(match.group(1)) if match else None

    def parts(part_code: str) -> list[dict]:
        return [
            comp for comp in comps
            if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_")
        ]

    def bom(part_code: str) -> dict | None:
        return next((row for row in bom_rows if row.get("drawing_no") == part_code), None)

    def add_bom(checkpoint: str, verdict: str, row: dict | None) -> None:
        if row:
            add(checkpoint, verdict, _fact_evidence(ctx, code, row))

    def add_balloon(checkpoint: str, verdict: str, row: dict | None, part_code: str) -> None:
        if not row:
            return
        balloon = next(
            (
                fact for fact in facts
                if fact.get("kind") == "note"
                and str(fact.get("text", "")).strip() == str(row.get("item", ""))
                and _fact_matches(fact, part_code)
            ),
            None,
        )
        if balloon:
            add(checkpoint, verdict, _fact_evidence(ctx, code, balloon))

    checkpoint = "底角件三角板板厚"
    plate_codes = ("F210101", "F210102")
    plate_rows = {part_code: bom(part_code) for part_code in plate_codes}
    plate_parts = {part_code: parts(part_code) for part_code in plate_codes}
    source_ready = bool(bom_rows and comps)
    plate_ok = source_ready and all(
        plate_rows[part_code]
        and len(plate_parts[part_code]) == quantity(plate_rows[part_code]) == 2
        and _thickness_set(plate_parts[part_code]) == {spec(plate_rows[part_code])}
        for part_code in plate_codes
    )
    plate_verdict = "pass" if plate_ok else "fail" if source_ready else "warning"
    verdicts.append(plate_verdict)
    add(checkpoint, plate_verdict, {
        "type": "input",
        "side": "manual",
        "text": "判定规则：F210101/F210102 前底角封板D的 BOM 规格、数量应与模型钣金厚度、实例数量一致",
    })
    for part_code in plate_codes:
        row = plate_rows[part_code]
        found = plate_parts[part_code]
        add_bom(checkpoint, plate_verdict, row)
        add_balloon(checkpoint, plate_verdict, row, part_code)
        add(checkpoint, plate_verdict, {
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": (
                f"模型 {part_code} 前底角封板D：{len(found)} 件；"
                f"钣金厚度={sorted(_thickness_set(found))} mm；"
                f"BOM 要求={spec(row) if row else '未找到'} mm，数量={quantity(row)}"
            ),
        })

    checkpoint = "鹅颈槽封板板厚"
    gooseneck_terms = ("鹅颈", "GOOSENECK")
    gooseneck_rows = [
        row for row in bom_rows
        if any(term.lower() in f"{row.get('description', '')} {row.get('drawing_no', '')}".lower() for term in gooseneck_terms)
    ]
    gooseneck_parts = [
        comp for comp in comps
        if comp.get("leaf") and any(term.lower() in comp.get("name", "").lower() for term in gooseneck_terms)
    ]
    front_sill = bom("F140401")
    is_20gp = any("F140401_20前端下梁" in comp.get("name", "") for comp in comps)
    inventory_complete = bool(bom_rows and comps)
    gooseneck_verdict = (
        "skipped"
        if is_20gp and inventory_complete and not gooseneck_rows and not gooseneck_parts
        else "warning"
    )
    verdicts.append(gooseneck_verdict)
    add(checkpoint, gooseneck_verdict, {
        "type": "input",
        "side": "manual",
        "text": "适用性规则：20GP 且前端图 BOM、模型完整清单均无鹅颈槽结构时，本审查点不适用",
    })
    add_bom(checkpoint, gooseneck_verdict, front_sill)
    add(checkpoint, gooseneck_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"箱型识别：F140401_20前端下梁存在={is_20gp}；"
            f"BOM 鹅颈槽封板 {len(gooseneck_rows)} 项；模型鹅颈槽封板 {len(gooseneck_parts)} 件；"
            f"结论={'20GP 不适用' if gooseneck_verdict == 'skipped' else '适用性待确认'}"
        ),
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"底角封板 F210101/F210102 板厚与 BOM {'一致' if plate_ok else '未闭环'}；"
        f"鹅颈槽封板{'对当前 20GP 不适用' if gooseneck_verdict == 'skipped' else '适用性待确认'}",
        evidence,
    )


def h_front_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1F"
    facts = _facts(ctx, code)
    comps = ctx.spatial.get(code, [])
    rods = [
        comp for comp in comps
        if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith("F060002_")
    ]
    bom_row = next(
        (fact for fact in facts if fact.get("kind") == "bom" and fact.get("drawing_no") == "F060002"),
        None,
    )
    manual_basis = (
        ctx.manual.find_around("Lashing rods Qty. / Each front corner post", 360)
        if ctx.manual else ""
    )
    dimension = next(
        (
            fact for fact in facts
            if fact.get("kind") == "dimension"
            and _fact_matches(fact, "F060002")
            and any(abs(float(value) - 14) <= 0.1 for value in fact.get("values") or [])
        ),
        None,
    )
    balloon = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip() == "5"
            and _fact_matches(fact, "F060002")
        ),
        None,
    )

    properties_by_component: dict[str, dict[str, str]] = {}
    api_path = ctx.api_paths.get(code, "")
    if api_path:
        try:
            with open(api_path, "rb") as handle:
                for component in ijson.items(handle, "referencedComponents.item"):
                    component_name = str(component.get("name", ""))
                    if not component_name.rsplit("/", 1)[-1].startswith("F060002_"):
                        continue
                    properties_by_component[component_name] = {
                        str(prop.get("name", "")): str(prop.get("resolvedValue", "") or "")
                        for prop in component.get("customProperties", [])
                    }
        except OSError:
            properties_by_component = {}
    properties = list(properties_by_component.values())

    def norm(value: str) -> str:
        return value.upper().replace("Ф", "Φ").replace("×", "X").replace(" ", "")

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    def add_basis(checkpoint: str, verdict: str) -> None:
        add(checkpoint, verdict, {
            "type": "doc",
            "side": "manual",
            "text": f"说明书要求：{manual_basis}" if manual_basis else "说明书未提取到前角柱拉筋数量及镀锌要求",
        })

    def add_fact(checkpoint: str, verdict: str, fact: dict | None) -> None:
        if fact:
            add(checkpoint, verdict, _fact_evidence(ctx, code, fact))

    evidence: list[dict] = []
    verdicts: list[str] = []
    points = [
        [round(float(value), 3) for value in center]
        for comp in rods
        if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
    ]
    side_counts = Counter("negative_z" if point[2] < 0 else "positive_z" for point in points)
    negative_y = sorted(point[1] for point in points if point[2] < 0)
    positive_y = sorted(point[1] for point in points if point[2] >= 0)
    try:
        bom_quantity = int(float(str(bom_row.get("quantity") or 0))) if bom_row else 0
    except ValueError:
        bom_quantity = 0

    checkpoint = "前角柱拉筋排布"
    layout_ready = bool(manual_basis and bom_row and comps)
    layout_ok = (
        layout_ready
        and len(rods) == bom_quantity == 6
        and side_counts == {"negative_z": 3, "positive_z": 3}
        and negative_y == positive_y
    )
    layout_verdict = "pass" if layout_ok else "fail" if layout_ready else "warning"
    verdicts.append(layout_verdict)
    add_basis(checkpoint, layout_verdict)
    add_fact(checkpoint, layout_verdict, bom_row)
    add_fact(checkpoint, layout_verdict, balloon)
    add(checkpoint, layout_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"模型 F060002 前端拉筋 {len(rods)} 根；左右数量={dict(side_counts)}；"
            f"左右高度坐标(mm)={negative_y}/{positive_y}；左右对称={negative_y == positive_y}；"
            f"中心坐标(mm)={points}"
        ),
    })

    checkpoint = "前角柱拉筋规格"
    bom_spec = norm(str(bom_row.get("specification", ""))) if bom_row else ""
    bom_material = norm(str(bom_row.get("material", ""))) if bom_row else ""
    api_specs = {norm(item.get("规格", "")) for item in properties if item.get("规格")}
    api_materials = {norm(item.get("材质", "")) for item in properties if item.get("材质")}
    api_surfaces = {norm(item.get("表面处理", "")) for item in properties if item.get("表面处理")}
    spec_ready = bool(manual_basis and bom_row and dimension and properties)
    spec_ok = (
        spec_ready
        and bom_spec == "Φ14X150"
        and "SS400" in bom_material
        and "ZINCPLATED" in bom_material
        and api_specs == {"Φ14X150"}
        and api_surfaces == {"ZINCPLATED"}
        and all("SS400" in value or "Q235A" in value for value in api_materials)
    )
    spec_verdict = "pass" if spec_ok else "fail" if spec_ready else "warning"
    verdicts.append(spec_verdict)
    add_basis(checkpoint, spec_verdict)
    add_fact(checkpoint, spec_verdict, bom_row)
    add_fact(checkpoint, spec_verdict, dimension)
    add(checkpoint, spec_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"F060002 零件属性：数量={len(properties)}；规格={sorted(api_specs)}；"
            f"材质={sorted(api_materials)}；表面处理={sorted(api_surfaces)}；"
            "图面剖面 C-C 标注 3-Φ14 并精确绑定 F060002"
        ),
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"前角柱拉筋已分为排布、规格 2 个审查点：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_chas_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1B"
    checkpoint = "地板钉下沉深度"
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])

    manual_line = next(
        (
            line.strip()
            for line in re.split(r"[\r\n]+", manual.full_text)
            if re.search(
                r"floor screws?.*(?:countersunk|below)|地板钉.*(?:下沉|沉头|低于)",
                line,
                re.IGNORECASE,
            )
        ),
        "",
    )
    manual_values = [float(value) for value in _MM_RE.findall(manual_line)]
    manual_range = tuple(sorted(manual_values[:2])) if len(manual_values) >= 2 else None

    facts = _facts(ctx, code)
    bom = next(
        (
            fact
            for fact in facts
            if fact.get("kind") == "bom"
            and "SELF-TAPPING SCREW" in str(fact.get("description", "")).upper()
        ),
        None,
    )
    balloon = next(
        (
            fact
            for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip() == str((bom or {}).get("item", "14"))
            and "J090001" in _attachment_text(fact)
        ),
        None,
    )
    screws = [
        comp
        for comp in ctx.spatial.get(code, [])
        if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith("J090001_自攻螺钉-")
    ]
    bound_dimensions = [
        fact
        for fact in facts
        if fact.get("kind") == "dimension" and fact.get("attached") and "J090001" in _attachment_text(fact)
    ]
    depth_facts: list[dict] = []
    drawing_depths: list[float] = []
    for fact in facts:
        if fact.get("kind") not in ("note", "dimension"):
            continue
        text = str(fact.get("text", ""))
        attachments = _attachment_text(fact)
        semantic = bool(re.search(r"countersunk|sink|below|depth|下沉|沉头|低于", text, re.IGNORECASE))
        screw_to_floor = "J090001" in attachments and any(
            token in attachments for token in ("B103001", "B103003", "胶合板")
        )
        values = list(fact.get("values") or []) + [float(value) for value in _MM_RE.findall(text)]
        plausible = [float(value) for value in values if 0 < float(value) <= 10]
        if plausible and (semantic or screw_to_floor):
            depth_facts.append(fact)
            drawing_depths.extend(plausible)

    tol = float(rule.params.get("tolerance_mm", 0.5))
    depths_match = bool(manual_range and drawing_depths) and all(
        manual_range[0] - tol <= value <= manual_range[1] + tol for value in drawing_depths
    )
    evidence: list[dict] = []
    verdict = "warning" if manual_range is None else "pass" if depths_match else "fail"
    evidence.append({
        "type": "doc",
        "side": "manual",
        "checkpoint": checkpoint,
        "checkpoint_verdict": verdict,
        "text": (
            f"说明书 6.8.1：地板钉头应低于地板上表面 {manual_range[0]:g}–{manual_range[1]:g} mm；原文：{manual_line}"
            if manual_range
            else "说明书未提取到地板钉下沉深度范围"
        ),
    })
    for fact in (bom, balloon, *depth_facts):
        if not fact:
            continue
        item = _fact_evidence(ctx, code, fact)
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)
    evidence.append({
        "type": "json",
        "side": "drawing",
        "checkpoint": checkpoint,
        "checkpoint_verdict": verdict,
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"底架图实际：BOM 序号={(bom or {}).get('item') or '未找到'}，"
            f"J090001 地板钉实例 {len(screws)} 个；完整扫描尺寸 {sum(fact.get('kind') == 'dimension' for fact in facts)} 个，"
            f"其中精确绑定地板钉 {len(bound_dimensions)} 个，"
            f"可识别为下沉深度的标注 {len(depth_facts)} 个；提取值={sorted(set(drawing_depths)) or '无'}"
        ),
    })
    if manual_range is None:
        return RuleOutcome("warning", "说明书未提取到地板钉下沉深度范围", evidence)
    if not drawing_depths:
        return RuleOutcome(
            "fail",
            f"说明书要求地板钉头下沉 {manual_range[0]:g}–{manual_range[1]:g} mm；底架图未标注下沉深度",
            evidence,
        )

    if depths_match:
        return RuleOutcome(
            "pass",
            f"地板钉下沉深度 {sorted(set(drawing_depths))} mm，符合说明书 {manual_range[0]:g}–{manual_range[1]:g} mm",
            evidence,
        )
    return RuleOutcome(
        "fail",
        f"地板钉下沉深度 图面 {sorted(set(drawing_depths))} mm 与说明书 {manual_range[0]:g}–{manual_range[1]:g} mm 不一致",
        evidence,
    )


def h_chas_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1B"
    facts = _facts(ctx, code)
    comps = ctx.spatial.get(code, [])
    evidence: list[dict] = []
    verdicts: list[str] = []
    manual_basis = next(
        (
            line.strip()
            for line in re.split(r"[\r\n]+", ctx.manual.full_text if ctx.manual else "")
            if "crossmembers are composed" in line.lower()
        ),
        "",
    )

    def parts(part_code: str) -> list[dict]:
        return [
            comp
            for comp in comps
            if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_")
        ]

    def bom(part_code: str) -> dict | None:
        return next(
            (fact for fact in facts if fact.get("kind") == "bom" and fact.get("drawing_no") == part_code),
            None,
        )

    def number(row: dict | None, field: str) -> float:
        try:
            return float(str((row or {}).get(field) or 0))
        except ValueError:
            return 0

    def anchor(kind: str, part_code: str, value: float | None = None) -> dict | None:
        return next(
            (
                fact
                for fact in facts
                if fact.get("kind") == kind
                and fact.get("attached")
                and part_code in _attachment_text(fact)
                and (
                    value is None
                    or any(abs(float(item) - value) < 0.01 for item in fact.get("values") or [])
                )
            ),
            None,
        )

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    def add_bom(checkpoint: str, verdict: str, row: dict | None) -> None:
        if row:
            add(checkpoint, verdict, _fact_evidence(ctx, code, row))

    def add_anchor(checkpoint: str, verdict: str, fact: dict | None) -> None:
        if fact:
            add(checkpoint, verdict, _fact_evidence(ctx, code, fact))

    # 常规宽/底横梁：2 件 B070101、6 件 B150101 加强板、12 件 B080101。
    checkpoint = "宽／底横梁板厚"
    wide = parts("B070101")
    webs = parts("B150101")
    regular = parts("B080101")
    wide_bom = bom("B070101")
    web_bom = bom("B150101")
    regular_bom = bom("B080101")
    wide_ok = (
        len(wide) == number(wide_bom, "quantity") == 2
        and len(webs) == number(web_bom, "quantity") == 6
        and len(regular) == number(regular_bom, "quantity") == 12
        and number(wide_bom, "specification")
        == number(web_bom, "specification")
        == number(regular_bom, "specification")
        == 4
        and _thickness_set(wide) == _thickness_set(webs) == _thickness_set(regular) == {4.0}
    )
    wide_verdict = (
        "pass"
        if wide_ok
        else "fail"
        if wide_bom or web_bom or regular_bom or wide or webs or regular
        else "warning"
    )
    verdicts.append(wide_verdict)
    add(checkpoint, wide_verdict, {
        "type": "doc",
        "side": "manual",
        "text": f"说明书 6.3.2：{manual_basis}" if manual_basis else "说明书未提取到横梁结构条款",
    })
    for row in (wide_bom, web_bom, regular_bom):
        add_bom(checkpoint, wide_verdict, row)
    add_anchor(checkpoint, wide_verdict, anchor("note", "B070101"))
    add_anchor(checkpoint, wide_verdict, anchor("note", "B150101"))
    add_anchor(checkpoint, wide_verdict, anchor("note", "B080101"))
    add_anchor(checkpoint, wide_verdict, anchor("dimension", "B070101", 4))
    add_anchor(checkpoint, wide_verdict, anchor("dimension", "B080101", 4))
    add(checkpoint, wide_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"宽底横梁：B070101 模型 {len(wide)} 件、精确板厚={sorted(_thickness_set(wide))} mm；"
            f"B150101 加强板 {len(webs)} 件、精确板厚={sorted(_thickness_set(webs))} mm；"
            f"B080101 底横梁 {len(regular)} 件、精确板厚={sorted(_thickness_set(regular))} mm；"
            f"BOM 要求分别为 {number(wide_bom, 'quantity'):g}/{number(web_bom, 'quantity'):g}/"
            f"{number(regular_bom, 'quantity'):g} 件、4 mm"
        ),
    })

    # 当前 20GP 清单没有短宽/短底横梁；不把普通 B080101 错当成短横梁。
    checkpoint = "短宽／底横梁板厚"
    short_terms = ("短宽", "短底横梁", "SHORT WIDE", "SHORT CROSSMEMBER")
    short_rows = [
        fact
        for fact in facts
        if fact.get("kind") == "bom"
        and any(
            term.lower() in f"{fact.get('description', '')} {fact.get('drawing_no', '')}".lower()
            for term in short_terms
        )
    ]
    short_parts = [
        comp
        for comp in comps
        if any(term.lower() in comp.get("name", "").lower() for term in short_terms)
    ]
    manual_type = next(
        (
            line.strip()
            for line in re.split(r"[\r\n]+", ctx.manual.full_text if ctx.manual else "")
            if re.search(r"20['′]?\s*x\s*8['′]?\s*x\s*8['′]?6|ISO\s+1CC", line, re.IGNORECASE)
        ),
        "",
    )
    inventory_complete = bool(facts and comps)
    if manual_type and inventory_complete and not short_rows and not short_parts:
        short_verdict = "skipped"
    else:
        short_verdict = "warning"
    verdicts.append(short_verdict)
    add(checkpoint, short_verdict, {
        "type": "doc",
        "side": "manual",
        "text": f"说明书箱型：{manual_type or '未提取到 20GP/1CC 标识'}",
    })
    add(checkpoint, short_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"底架图完整清单：短宽/短底横梁 BOM {len(short_rows)} 项、模型 {len(short_parts)} 件；"
            f"B080101 为普通底横梁，不计入短横梁；"
            f"结论={'当前 20GP 不适用' if short_verdict == 'skipped' else '适用性待确认'}"
        ),
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"宽／底横梁板厚已核对；短宽／底横梁对当前 20GP 不适用："
        f"{verdicts.count('pass')} 项通过，{verdicts.count('skipped')} 项不适用，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


def h_chas_04(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    base_code = "000A22G1B"
    total_code = "000A22G1G"
    total = ctx.spatial.get(total_code, [])
    nails = [
        comp for comp in total
        if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith("J090001_")
    ]
    supports = [
        comp for comp in total
        if comp.get("leaf")
        and comp.get("name", "").rsplit("/", 1)[-1].startswith(("F240102_", "F241002_"))
    ]
    evidence: list[dict] = []
    verdicts: list[str] = []

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    checkpoint = "地板钉排布"
    manual_match = re.search(
        r"Screws[’']\s*Qty\.\s*:\s*6\s*Pcs/end row,\s*4\s*Pcs/other",
        ctx.manual.full_text if ctx.manual else "",
        re.IGNORECASE,
    )
    bom = next(
        (
            fact for fact in _facts(ctx, base_code, "bom")
            if "SELF-TAPPING SCREW" in str(fact.get("description", "")).upper()
        ),
        None,
    )
    groups = Counter(comp.get("parent_id") for comp in nails if comp.get("parent_id"))
    group_sizes = Counter(groups.values())
    try:
        bom_qty = int(float(str((bom or {}).get("quantity") or 0)))
    except ValueError:
        bom_qty = 0
    layout_complete = bool(manual_match and bom and nails and groups)
    layout_passed = (
        layout_complete
        and bom_qty == len(nails) == 200
        and group_sizes == Counter({4: 32, 6: 12})
    )
    layout_verdict = "pass" if layout_passed else "fail" if layout_complete else "warning"
    verdicts.append(layout_verdict)
    if manual_match:
        add(checkpoint, layout_verdict, {
            "type": "doc",
            "side": "manual",
            "text": "说明书：Screws’ Qty. = 6 Pcs/end row, 4 Pcs/other",
        })
    if bom:
        add(checkpoint, layout_verdict, _fact_evidence(ctx, base_code, bom))
    add(checkpoint, layout_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(total_code, ""),
        "text": (
            f"总装配：J090001 叶子件 {len(nails)} 个；BOM {bom_qty or '未提取'} 个；"
            f"{group_sizes.get(4, 0)} 组×4颗、{group_sizes.get(6, 0)} 组×6颗"
        ),
    })

    checkpoint = "前端避开塑料角撑"

    def axis_gap(a_min: float, a_max: float, b_min: float, b_max: float) -> float:
        return float(max(a_min - b_max, b_min - a_max, 0))

    clearances: list[tuple[dict, dict, float]] = []
    bounded_nails = [comp for comp in nails if comp.get("bounds_mm")]
    for support in supports:
        support_bounds = support.get("bounds_mm") or {}
        if not support_bounds:
            continue
        candidates: list[tuple[float, dict]] = []
        for nail in bounded_nails:
            nail_bounds = nail["bounds_mm"]
            gaps = [
                axis_gap(nail_bounds[f"min{axis}"], nail_bounds[f"max{axis}"],
                         support_bounds[f"min{axis}"], support_bounds[f"max{axis}"])
                for axis in "XYZ"
            ]
            candidates.append((sum(gap * gap for gap in gaps) ** 0.5, nail))
        if candidates:
            distance, nearest = min(candidates, key=lambda item: item[0])
            clearances.append((support, nearest, distance))

    avoidance_complete = len(supports) == len(clearances) == 2
    avoidance_passed = avoidance_complete and all(distance > 0 for _, _, distance in clearances)
    avoidance_verdict = "pass" if avoidance_passed else "warning"
    verdicts.append(avoidance_verdict)
    for support, nearest, distance in clearances:
        add(checkpoint, avoidance_verdict, {
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(total_code, ""),
            "text": (
                f"总装配：塑料角撑（前端地板支撑）{support['name'].rsplit('/', 1)[-1]}；"
                f"最近地板钉 {nearest['name'].rsplit('/', 1)[-1]}；近似包围盒间距 {distance:.1f}mm"
            ),
        })
    if not clearances:
        add(checkpoint, avoidance_verdict, {
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(total_code, ""),
            "text": f"总装配：J090001 地板钉 {len(nails)} 个；F240102/F241002 塑料地板支撑 {len(supports)} 个",
        })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    return RuleOutcome(
        verdict,
        f"地板钉排布与前端塑料角撑避让：{verdicts.count('pass')} 项通过，"
        f"{verdicts.count('fail')} 项不通过，{verdicts.count('warning')} 项待确认",
        evidence,
    )


# ---------------------------------------------------------------------------
# api.json drawing annotation rules (the only VLM domain is trademark)
# ---------------------------------------------------------------------------


def _facts(ctx, code: str, *kinds: str) -> list[dict]:  # noqa: ANN001
    items = ctx.get_drawing_facts(code)
    return [item for item in items if not kinds or item.get("kind") in kinds]


def _attachment_text(fact: dict) -> str:
    return " | ".join(
        value
        for item in fact.get("attachments") or []
        for value in (item.get("component_name", ""), item.get("drawing_component_name", ""))
        if value
    )


def _fact_matches(fact: dict, *keywords: str) -> bool:
    target = f"{fact.get('text', '')} {_attachment_text(fact)}".lower()
    return any(keyword.lower() in target for keyword in keywords)


def _sealing_facts(ctx, code: str) -> list[dict]:  # noqa: ANN001
    return [
        fact
        for fact in _facts(ctx, code, "note", "dimension", "weld")
        if _fact_matches(fact, "sealing", "sealant", "密封", "打胶", "涂胶")
    ]


# Q/CIMC 40103-2007 中可由当前六张装配图直接核对的必打胶部位。
# 元组字段：部位、条款、图中存在性关键词、打胶标注绑定关键词、是否条件项。
_SEALANT_TARGETS = {
    "000A22G1B": (
        ("地板中梁与木地板装配面", "4.1、4.6", ("B110001",), ("B110001",), False),
        ("底侧梁与木地板装配面", "4.1、4.6", ("B130306",), ("B130306",), False),
        ("宽边底横梁与木地板装配面", "4.1", ("B070101",), ("B070101",), False),
    ),
    "000A22G1E": (
        ("门铰链背面立缝及顶部", "4.2、4.12", ("E200008",), ("E200008",), False),
        ("门搭扣背面立缝及顶部", "4.2、4.12", ("E260101",), ("E260101",), False),
        ("门封胶条背面及外侧", "4.5、4.11", ("E100007",), ("E100007",), False),
        ("门封铆钉", "4.10", ("QJ3148-A",), ("E110001", "E120004", "QJ3148-A"), False),
        ("门槛与木地板装配面", "4.1、4.6", ("E181201",), ("E181201", "E190101"), False),
        ("锁杆螺栓、铆钉（箱内）", "4.14", ("E500001",), ("E500001",), False),
        ("门端箱内搭接间断焊缝", "4.7", ("E240101",), ("E240101", "E250101"), False),
    ),
    "000A22G1F": (
        ("前端下梁与木地板装配面", "4.1、4.6", ("F140401",), ("F140401",), False),
        ("前端地板支撑与木地板装配面", "4.1.5", ("F240102", "F241002"), ("F240102", "F241002"), False),
        ("前墙板与角柱箱内搭接缝", "4.7", ("F080101",), ("F080101", "F050201", "F052001"), False),
        ("前楣与顶板箱内搭接缝", "4.7", ("F200201",), ("F200201",), False),
    ),
    "000A22G1S": (
        ("通风器周边及铆钉（箱内外）", "4.3、4.9", ("S050001",), ("S050001",), False),
        ("顶侧梁与侧板箱内搭接缝", "4.7、4.8", ("S040101",), ("S040101",), False),
        ("绳钩背后侧板与底侧梁缝隙", "4.8.7c", ("S060001",), ("S060001",), False),
    ),
    "000A22G1R": (
        ("顶板薄板焊道", "4.8.5（按产品规范选择）", ("R010501",), ("R010501",), True),
    ),
    "000A22G1G": (
        ("地板与底架周边装配面", "4.1、4.6", ("000A22G1B",), ("B110001", "B130306", "B070101", "胶合板"), False),
        ("侧板与顶侧梁箱内搭接缝", "4.7、4.8", ("000A22G1S",), ("S040101",), False),
        ("前端搭接及地板接口", "4.6、4.7", ("000A22G1F",), ("F140401", "F200201", "F080101"), False),
        ("门端搭接及地板接口", "4.6、4.7", ("000A22G1E",), ("E060101", "E170101", "E010101"), False),
    ),
}


def _sealant_evidence(ctx, code: str, label: str, section: str, status: str, fact: dict | None = None) -> dict:  # noqa: ANN001
    evidence = _fact_evidence(ctx, code, fact) if fact else {
        "type": "input",
        "side": "drawing",
        "file_id": ctx.pdf_file_ids.get(code, ""),
        "page": None,
        "rect": None,
        "text": "",
    }
    drawing_text = evidence["text"] if fact else "图中未找到对应打胶标注"
    evidence["text"] = (
        f"打胶检查｜状态={status}｜部位={label}｜依据=Q/CIMC 40103-2007 {section}｜图面={drawing_text}"
    )[:500]
    return evidence


def _fact_evidence(ctx, code: str, fact: dict) -> dict:  # noqa: ANN001
    attachments = _attachment_text(fact)
    value = fact.get("text") or fact.get("values") or fact.get("name") or "图面标注"
    text = f"{fact.get('view') or fact.get('sheet')}: {value}"
    if attachments:
        text += f"；绑定={attachments}"
    return {
        "type": "pdf",
        "side": "drawing",
        "file_id": ctx.pdf_file_ids.get(code, ""),
        "page": fact.get("page", 1),
        "rect": fact.get("rect"),
        "text": text[:400],
        "precision": "approximate",
    }


def _color_tokens(text: str) -> set[str]:
    tokens = {f"ral{value}" for value in _RAL_RE.findall(text)}
    tokens.update(word for word in _COLOR_WORDS if word in text)
    english = ("white", "black", "gray", "grey", "blue", "red", "green", "yellow")
    tokens.update(word for word in english if word in text.lower())
    return tokens


def h_door_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1E"
    facts = _sealing_facts(ctx, code)
    input_text = (
        ctx.fact_bundle.customer_text
        if ctx.fact_bundle
        else "；".join(str(ctx.inputs.get(key) or "") for key in ("tech_req", "new_material"))
    )
    input_is_exterior = bool(re.search(r"外(?:面|部)?漆|外部颜色|exterior", input_text, re.IGNORECASE))
    input_color = _color_tokens(input_text) if input_is_exterior else set()

    manual_section = ""
    if ctx.manual:
        match = re.search(
            r"Exterior\s+Surface(?P<section>.*?)Interior\s+Surface",
            ctx.manual.full_text,
            re.IGNORECASE | re.DOTALL,
        )
        manual_section = match.group(0) if match else ""
    manual_color = _color_tokens(manual_section)

    evidence: list[dict] = []
    if input_color:
        evidence.append({"type": "input", "side": "manual", "text": f"客户要求外面漆颜色：{sorted(input_color)}"})
    if manual_color:
        evidence.append({
            "type": "doc",
            "side": "manual",
            "text": f"说明书 Exterior Surface 外面漆颜色：{sorted(manual_color)}",
        })
    if not input_color and not manual_color:
        evidence.append({
            "type": "doc",
            "side": "manual",
            "text": "说明书 Exterior Surface 仅规定涂层与膜厚，未标明外面漆颜色；客户要求也未提供外面漆颜色",
        })

    color_facts = [fact for fact in facts if _color_tokens(str(fact.get("text") or ""))]
    evidence.extend(_fact_evidence(ctx, code, fact) for fact in color_facts[:4])
    actual = _color_tokens(" ".join(str(fact.get("text") or "") for fact in color_facts))
    if not actual:
        evidence.append({
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": f"门端图提取到 {len(facts)} 条 SEALING/密封胶位置注释，但均未标明颜色；不作为颜色定位证据",
        })

    if input_color and manual_color and input_color.isdisjoint(manual_color):
        return RuleOutcome("warning", "客户要求与说明书的外面漆颜色基准冲突，无法唯一比对", evidence)
    expected = input_color or manual_color
    if not expected and not actual:
        return RuleOutcome("warning", "外面漆颜色和密封胶颜色均未提供，无法进行颜色比对", evidence)
    if not expected:
        return RuleOutcome("warning", "未提供外面漆颜色基准，无法核验密封胶颜色", evidence)
    if not actual:
        return RuleOutcome("warning", f"外面漆颜色为 {sorted(expected)}，但门端图未标明密封胶颜色", evidence)
    if actual & expected:
        return RuleOutcome("pass", f"密封胶颜色与外面漆颜色匹配：{sorted(actual & expected)}", evidence)
    return RuleOutcome("fail", f"密封胶颜色 {sorted(actual)} 与外面漆颜色 {sorted(expected)} 不一致", evidence)


def h_door_04(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1E"
    part_code = str(rule.params.get("part_code", "E200008"))
    expected_count = int(rule.params.get("expected_quantity", 8))
    typ_token = str(rule.params.get("typ_token", "TYP"))
    min_typ_leaders = int(rule.params.get("min_typ_leaders", 2))
    facts = [fact for fact in _sealing_facts(ctx, code) if part_code in _attachment_text(fact)]
    hinges = [
        comp for comp in ctx.spatial.get(code, [])
        if comp.get("leaf") and part_code in str(comp.get("name", ""))
    ]
    bom = next(
        (fact for fact in _facts(ctx, code, "bom") if fact.get("drawing_no") == part_code),
        None,
    )
    evidence: list[dict] = [{
        "type": "input",
        "side": "manual",
        "text": (
            f"判定规则：门端图应有 {expected_count} 个 {part_code} 门铰链；逐个标注，或使用 {typ_token} 共用标注。"
            "共用标注的每条引线都必须能归属到门铰链或其安装界面"
        ),
    }]
    if ctx.manual and (basis := ctx.manual.find_around("6.5.4.2 Hinges and Pins", 260)):
        evidence.append({"type": "doc", "side": "manual", "text": f"说明书要求：{basis}"})
    if bom:
        evidence.append({
            "type": "pdf",
            "side": "drawing",
            "file_id": ctx.pdf_file_ids.get(code, ""),
            "page": bom.get("page", 1),
            "rect": bom.get("rect"),
            "precision": "approximate",
            "text": f"门端图 BOM：序号 {bom.get('item')}；图号 {part_code}；数量 {bom.get('quantity')}",
        })
    evidence.append({
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": f"门端空间 JSON：{part_code} 门铰链 {len(hinges)} 个",
    })
    evidence.extend(_fact_evidence(ctx, code, fact) for fact in facts[:5])

    if not facts:
        return RuleOutcome("fail", "未找到绑定到门铰链的打胶注释", evidence)
    invalid = [
        fact for fact in facts
        if not fact.get("attached") or not fact.get("leaders")
        or any(item.get("dangling") or item.get("persistent_status") != "exact" for item in fact.get("attachments") or [])
    ]
    if invalid:
        return RuleOutcome("fail", f"发现 {len(invalid)} 条门铰链打胶注释未有效绑定", evidence)
    if not bom or str(bom.get("quantity")) != str(expected_count) or len(hinges) != expected_count:
        return RuleOutcome(
            "warning",
            f"门铰链数量基准不完整或不一致：规则={expected_count}，BOM={bom.get('quantity') if bom else '缺失'}，JSON={len(hinges)}",
            evidence,
        )

    typical = [fact for fact in facts if re.search(rf"\b{re.escape(typ_token)}\b", str(fact.get("text", "")), re.IGNORECASE)]
    if typical:
        unresolved = [
            item
            for fact in typical
            for item in fact.get("attachments") or []
            if not item.get("component_name")
        ]
        leader_count = sum(int(fact.get("leaders") or 0) for fact in typical)
        evidence.append({
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": (
                f"{len(typical)} 条 SEALING {typ_token} 共用标注；引线 {leader_count} 条；"
                f"无法归属到具体组件的引线目标 {len(unresolved)} 个"
            ),
        })
        if leader_count < min_typ_leaders or unresolved:
            return RuleOutcome(
                "warning",
                f"SEALING {typ_token} 已指向代表性门铰链，但 {leader_count} 条引线中有 {len(unresolved)} 条无法唯一归属到门铰链安装界面",
                evidence,
            )
        return RuleOutcome("pass", f"SEALING {typ_token} 的全部引线均有效，可覆盖 {expected_count} 个同型门铰链", evidence)

    covered = {
        item.get("component_name")
        for fact in facts
        for item in fact.get("attachments") or []
        if part_code in str(item.get("component_name", ""))
    }
    if len(covered) == expected_count:
        return RuleOutcome("pass", f"{expected_count} 个门铰链均有独立且有效的打胶注释", evidence)
    return RuleOutcome("fail", f"未使用 {typ_token} 共用标注，只有 {len(covered)}/{expected_count} 个门铰链有明确打胶注释", evidence)


def h_door_05(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1E"
    all_symbols = _facts(ctx, code, "weld")
    facts = _welding_facts(ctx, code)
    evidence: list[dict] = [{
        "type": "input",
        "side": "manual",
        "text": (
            "判定规则：仅检查包含真实焊接标记的符号；符号必须处于 attached 状态且每条引线具有完整坐标。"
            "引线坐标完整但组件持久化绑定缺失时降级为待确认，不直接判定偏移"
        ),
    }]
    if not facts:
        return RuleOutcome("warning", "门端图未提取到焊接符号", evidence)

    shifted = [
        fact for fact in facts
        if not fact.get("raw_attached", fact.get("attached"))
        or not fact.get("leader_point_counts")
        or any(count < 6 for count in fact.get("leader_point_counts") or [])
    ]
    review = [
        fact for fact in facts
        if fact not in shifted
        and any(item.get("dangling") or item.get("persistent_status") != "exact" for item in fact.get("attachments") or [])
    ]
    evidence.append({
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"SolidWorks weldSymbols 共 {len(all_symbols)} 条；排除 SEALING/HUCK BOLT/SECURA 等非焊接说明 "
            f"{len(all_symbols) - len(facts)} 条；真实焊接符号 {len(facts)} 条；疑似偏移 {len(shifted)} 条；绑定待确认 {len(review)} 条"
        ),
    })

    for fact in (shifted + review)[:6]:
        item = _fact_evidence(ctx, code, fact)
        exact = sum(
            attachment.get("persistent_status") == "exact" and not attachment.get("dangling")
            for attachment in fact.get("attachments") or []
        )
        unresolved = len(fact.get("attachments") or []) - exact
        item["text"] += (
            f"；审核详情=引线 {fact.get('leaders', 0)} 条，坐标点数 {fact.get('leader_point_counts') or []}，"
            f"精确绑定 {exact} 个，未解析绑定 {unresolved} 个，"
            f"状态={'疑似偏移' if fact in shifted else '绑定待确认'}"
        )
        evidence.append(item)

    if shifted:
        return RuleOutcome("fail", f"{len(facts)} 个真实焊接符号中有 {len(shifted)} 个缺少有效引线坐标或整体附着状态，疑似偏移", evidence)
    if review:
        return RuleOutcome(
            "warning",
            f"{len(facts)} 个真实焊接符号未发现明显偏移；其中 {len(review)} 个多引线符号存在组件绑定不完整，需人工确认",
            evidence,
        )
    return RuleOutcome("pass", f"{len(facts)} 个真实焊接符号均处于附着状态且所有引线坐标完整", evidence)


def h_side_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    facts = [fact for fact in _sealing_facts(ctx, "000A22G1S") if _fact_matches(fact, "通风器", "vent")]
    evidence = [_fact_evidence(ctx, "000A22G1S", fact) for fact in facts[:4]]
    if not facts:
        return RuleOutcome("fail", "未找到绑定到通风器的打胶注释", evidence)
    if any(not fact.get("attached") for fact in facts):
        return RuleOutcome("fail", "通风器打胶注释存在悬空绑定", evidence)
    return RuleOutcome("pass", f"找到 {len(facts)} 条绑定到通风器的打胶注释", evidence)


def h_side_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1S"
    checkpoint = "检查通风器铆钉是否标注打胶"
    facts = _facts(ctx, code)
    evidence: list[dict] = []

    def add(item: dict, verdict: str) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    bom = next(
        (fact for fact in facts if fact.get("kind") == "bom" and fact.get("drawing_no") == "QJ3148-A"),
        None,
    )
    balloon = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip() == "7"
            and _fact_matches(fact, "QJ3148-A")
        ),
        None,
    )
    head_tail = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip().upper() == "SEALING AT HEAD AND TAIL"
            and "局部视图 C" in str(fact.get("view", ""))
        ),
        None,
    )
    typ = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note" and str(fact.get("text", "")).strip().upper() == "TYP"
        ),
        None,
    )
    head_pos = (head_tail or {}).get("position") or []
    typ_pos = (typ or {}).get("position") or []
    typ_nearby = bool(
        len(head_pos) >= 2 and len(typ_pos) >= 2
        and (float(head_pos[0]) - float(typ_pos[0])) ** 2 + (float(head_pos[1]) - float(typ_pos[1])) ** 2 <= 0.03 ** 2
    )
    valid_leader = bool(
        head_tail
        and head_tail.get("attached")
        and int(head_tail.get("leaders") or 0) >= 1
        and all(not item.get("dangling") for item in head_tail.get("attachments") or [])
    )
    rivets = [
        comp for comp in ctx.spatial.get(code, [])
        if comp.get("leaf")
        and "MS040101_通风器模块" in comp.get("name", "")
        and comp.get("name", "").rsplit("/", 1)[-1].startswith("QJ3148-A_")
    ]
    rivet_points = sorted(
        (
            [round(float(value), 1) for value in center]
            for comp in rivets
            if (center := (comp.get("bounds_mm") or {}).get("center")) and len(center) >= 3
        ),
        key=lambda point: (point[2], point[0], point[1]),
    )
    side_counts = Counter("negative_z" if point[2] < 0 else "positive_z" for point in rivet_points)
    bom_ok = bool(
        bom
        and str(bom.get("description", "")).upper() == "HUCK-BOLT"
        and str(bom.get("material", "")).upper() == "ALU-ALU"
        and str(bom.get("specification", "")).lower().replace("×", "x") == "4.8x6"
        and str(bom.get("quantity", "")) == "6"
    )
    passed = (
        bom_ok
        and balloon is not None
        and valid_leader
        and typ_nearby
        and len(rivets) == len(rivet_points) == 6
        and side_counts == {"negative_z": 3, "positive_z": 3}
    )
    source_missing = not ctx.api_paths.get(code) or code not in ctx.spatial
    verdict = "warning" if source_missing else "pass" if passed else "fail"

    add({
        "type": "input",
        "side": "manual",
        "text": (
            "Q/CIMC 40103-2007 第4.3.5～4.3.6：通风器铆钉箱外应封胶；"
            "HUCK 钉头封胶直径 Φ10～12 mm、高度 8～10 mm"
        ),
    }, verdict)
    if bom:
        item = _fact_evidence(ctx, code, bom)
        item["text"] = (
            f"侧板图 BOM：序号 {bom.get('item') or ''}；图号 {bom.get('drawing_no') or ''}；"
            f"名称 {bom.get('description') or ''}；规格 {bom.get('specification') or ''}；"
            f"材质 {bom.get('material') or ''}；数量 {bom.get('quantity') or ''}"
        )
        add(item, verdict)
    for fact in (balloon, head_tail, typ):
        if fact:
            add(_fact_evidence(ctx, code, fact), verdict)
    add({
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"通风器模块实际 QJ3148-A 铝制 HUCK-BOLT {len(rivets)} 颗；负 Z 侧 {side_counts['negative_z']} 颗、"
            f"正 Z 侧 {side_counts['positive_z']} 颗；HEAD AND TAIL 引线有效={valid_leader}；邻近 TYP={typ_nearby}；"
            f"中心坐标(mm)={rivet_points}"
        ),
    }, verdict)

    if verdict == "pass":
        conclusion = "局部视图 C 已用 SEALING AT HEAD AND TAIL + TYP 标注通风器铆钉打胶，6 颗 HUCK-BOLT 与 BOM 一致"
    elif verdict == "warning":
        conclusion = "侧板 API JSON 或空间 JSON 缺失，无法完成通风器铆钉打胶检查"
    else:
        conclusion = "通风器铆钉数量、BOM 或 SEALING AT HEAD AND TAIL + TYP 图面标注不完整"
    return RuleOutcome(verdict, conclusion, evidence)


def h_front_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1F"
    checkpoint = "40HC前端下梁是否标注了焊缝朝向"
    facts = _facts(ctx, code)
    comps = ctx.spatial.get(code, [])
    bom_rows = [fact for fact in facts if fact.get("kind") == "bom"]
    front_sill = next((fact for fact in bom_rows if fact.get("drawing_no") == "F140401"), None)
    front_sill_balloon = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip() == "7"
            and _fact_matches(fact, "F140401")
        ),
        None,
    )
    is_20gp = any("F140401_20前端下梁" in comp.get("name", "") for comp in comps)
    parts_40hc = [
        comp for comp in comps
        if comp.get("leaf")
        and "前端下梁" in comp.get("name", "")
        and any(term in comp.get("name", "").upper() for term in ("40HC", "40前端"))
    ]
    inventory_complete = bool(bom_rows and comps)
    evidence: list[dict] = []

    def add(item: dict, verdict: str) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    if is_20gp and inventory_complete and not parts_40hc:
        verdict = "skipped"
        add({
            "type": "input",
            "side": "manual",
            "text": "适用性规则：仅 40HC 前端下梁需要检查焊缝朝向；20GP 不适用",
        }, verdict)
        for fact in (front_sill, front_sill_balloon):
            if fact:
                add(_fact_evidence(ctx, code, fact), verdict)
        add({
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": (
                "箱型识别：模型存在 F140401_20前端下梁；"
                f"40HC 前端下梁组件 {len(parts_40hc)} 件；BOM、模型清单完整={inventory_complete}；结论=20GP 不适用"
            ),
        }, verdict)
        return RuleOutcome(verdict, "当前为 20GP，40HC 前端下梁焊缝朝向检查不适用", evidence)

    if not inventory_complete:
        verdict = "warning"
        add({
            "type": "input",
            "side": "manual",
            "text": "前端图 BOM 或模型清单缺失，无法确认是否适用 40HC 焊缝朝向检查",
        }, verdict)
        return RuleOutcome(verdict, "箱型与前端下梁数据不完整，适用性待确认", evidence)

    candidate_segments = {comp.get("name", "").rsplit("/", 1)[-1] for comp in parts_40hc}
    welds = [
        fact for fact in facts
        if fact.get("kind") == "weld"
        and any(
            item.get("component_name", "").rsplit("/", 1)[-1] in candidate_segments
            for item in fact.get("attachments") or []
        )
    ]
    verdict = "fail" if parts_40hc and not welds else "warning"
    add({
        "type": "input",
        "side": "manual",
        "text": "40HC 适用时仅接受精确绑定到 40HC 前端下梁零件的焊缝符号；父级前槛模块匹配无效",
    }, verdict)
    for fact in welds:
        add(_fact_evidence(ctx, code, fact), verdict)
    add({
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": f"40HC 前端下梁组件={sorted(candidate_segments)}；精确绑定焊缝符号={len(welds)} 个",
    }, verdict)
    if verdict == "fail":
        conclusion = "40HC 前端下梁未找到精确绑定的焊缝朝向符号"
    else:
        conclusion = "已找到 40HC 前端下梁绑定焊缝，但 JSON 未提供可验证朝向正确性的受控基准，需确认"
    return RuleOutcome(verdict, conclusion, evidence)


def h_front_04(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1F"
    part_codes = ("F240102", "F241002")
    facts = _facts(ctx, code)
    comps = ctx.spatial.get(code, [])
    bom_rows = {
        part_code: next(
            (fact for fact in facts if fact.get("kind") == "bom" and fact.get("drawing_no") == part_code),
            None,
        )
        for part_code in part_codes
    }
    supports = {
        part_code: [
            comp for comp in comps
            if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_")
        ]
        for part_code in part_codes
    }

    def attached_to(fact: dict, part_code: str) -> bool:
        return any(
            item.get("component_name", "").rsplit("/", 1)[-1].startswith(f"{part_code}_")
            for item in fact.get("attachments") or []
        )

    sealings = {
        part_code: [fact for fact in _sealing_facts(ctx, code) if attached_to(fact, part_code)]
        for part_code in part_codes
    }
    balloons = {
        part_code: next(
            (
                fact for fact in facts
                if fact.get("kind") == "note"
                and str(fact.get("text", "")).strip() == str((bom_rows[part_code] or {}).get("item", ""))
                and attached_to(fact, part_code)
            ),
            None,
        )
        for part_code in part_codes
    }
    evidence: list[dict] = []
    verdicts: list[str] = []

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    checkpoint = "塑料地板支撑打胶注释"
    source_ready = bool(facts and comps and all(bom_rows.values()) and all(supports.values()))
    valid_sealings = {
        part_code: [
            fact for fact in sealings[part_code]
            if fact.get("attached")
            and int(fact.get("leaders") or 0) >= 1
            and all(not item.get("dangling") for item in fact.get("attachments") or [])
        ]
        for part_code in part_codes
    }
    sealing_ok = source_ready and all(valid_sealings.values())
    sealing_verdict = "pass" if sealing_ok else "fail" if source_ready else "warning"
    verdicts.append(sealing_verdict)
    add(checkpoint, sealing_verdict, {
        "type": "input",
        "side": "manual",
        "text": "判定规则：F240102 左、F241002 右两个 PP 地板支撑均应有精确绑定的打胶注释；无 TYP 时不得共享推断",
    })
    for part_code in part_codes:
        for fact in (bom_rows[part_code], balloons[part_code], *valid_sealings[part_code]):
            if fact:
                add(checkpoint, sealing_verdict, _fact_evidence(ctx, code, fact))
        add(checkpoint, sealing_verdict, {
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": (
                f"{part_code} {'左' if part_code == 'F240102' else '右'}支撑："
                f"模型数量={len(supports[part_code])}；材质={sorted({item.get('material_name') for item in supports[part_code]})}；"
                f"有效打胶绑定={len(valid_sealings[part_code])} 条；"
                f"状态={'已标注' if valid_sealings[part_code] else '缺少打胶标注'}"
            ),
        })

    checkpoint = "塑料地板支撑多余焊接注释"
    all_welds = [fact for fact in facts if fact.get("kind") == "weld"]
    support_welds = [
        fact for fact in all_welds
        if not _fact_matches(fact, "sealing", "sealant", "密封", "打胶")
        and any(attached_to(fact, part_code) for part_code in part_codes)
    ]
    weld_ready = bool(ctx.api_paths.get(code) and facts)
    weld_verdict = "pass" if weld_ready and not support_welds else "fail" if weld_ready else "warning"
    verdicts.append(weld_verdict)
    add(checkpoint, weld_verdict, {
        "type": "input",
        "side": "manual",
        "text": "判定规则：PP 塑料地板支撑不得绑定焊缝符号；必须扫描前端图完整焊缝清单证明不存在",
    })
    for fact in support_welds:
        add(checkpoint, weld_verdict, _fact_evidence(ctx, code, fact))
    add(checkpoint, weld_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"前端图完整焊缝清单 {len(all_welds)} 个；"
            f"精确绑定 F240102/F241002 的焊缝 {len(support_welds)} 个；"
            f"结论={'未发现多余焊接注释' if not support_welds else '发现多余焊接注释'}"
        ),
    })

    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    missing = [part_code for part_code in part_codes if not valid_sealings[part_code]]
    return RuleOutcome(
        verdict,
        f"塑料地板支撑打胶：{'左右均已标注' if not missing else '缺少 ' + '/'.join(missing) + ' 打胶标注'}；"
        f"精确绑定支撑的多余焊缝 {len(support_welds)} 个",
        evidence,
    )


def h_chas_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    checkpoint = "鹅颈槽地板角钢海绵条"
    codes = ("000A22G1B", "000A22G1F")
    terms = ("鹅颈", "GOOSENECK", "GOOSE NECK", "地板角钢", "FLOOR ANGLE")
    sponge_terms = ("海绵", "海棉", "SPONGE", "FOAM")
    facts = {code: _facts(ctx, code) for code in codes}
    components = [comp for code in codes for comp in ctx.spatial.get(code, [])]

    def matches(value: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in value.lower() for keyword in keywords)

    target_rows = [
        fact
        for code in codes
        for fact in facts[code]
        if fact.get("kind") == "bom"
        and matches(f"{fact.get('description', '')} {fact.get('drawing_no', '')}", terms)
    ]
    sponge_rows = [
        fact
        for code in codes
        for fact in facts[code]
        if fact.get("kind") == "bom"
        and matches(f"{fact.get('description', '')} {fact.get('drawing_no', '')}", sponge_terms)
    ]
    target_parts = [comp for comp in components if matches(comp.get("name", ""), terms)]
    sponge_parts = [comp for comp in components if matches(comp.get("name", ""), sponge_terms)]
    manual_type = next(
        (
            line.strip()
            for line in re.split(r"[\r\n]+", ctx.manual.full_text if ctx.manual else "")
            if re.search(r"20['′]?\s*x\s*8['′]?\s*x\s*8['′]?6|ISO\s+1CC", line, re.IGNORECASE)
        ),
        "",
    )
    front_sill = next(
        (
            comp
            for comp in ctx.spatial.get("000A22G1F", [])
            if "F140401_20前端下梁" in comp.get("name", "")
        ),
        None,
    )
    is_20gp = bool(manual_type or front_sill)
    inventory_complete = bool(facts["000A22G1B"] and ctx.spatial.get("000A22G1B"))
    if is_20gp and inventory_complete and not target_rows and not target_parts:
        verdict = "skipped"
        conclusion = "当前为 20GP，底架无鹅颈槽结构，鹅颈槽地板角钢海绵条检查不适用"
    elif target_rows or target_parts:
        verdict = "pass" if sponge_rows or sponge_parts else "warning"
        conclusion = (
            "鹅颈槽地板角钢区域已配置海绵条"
            if verdict == "pass"
            else "已识别鹅颈槽地板角钢，但缺少海绵条要求或配置证据"
        )
    else:
        verdict = "warning"
        conclusion = "箱型或鹅颈槽结构清单不完整，无法判断海绵条适用性"

    evidence = [
        {
            "type": "doc",
            "side": "manual",
            "checkpoint": checkpoint,
            "checkpoint_verdict": verdict,
            "text": f"说明书箱型：{manual_type or '未提取到 20GP/1CC 标识'}",
        },
        {
            "type": "json",
            "side": "drawing",
            "checkpoint": checkpoint,
            "checkpoint_verdict": verdict,
            "file_id": ctx.json_file_ids.get("000A22G1B", ""),
            "text": (
                f"底架图与前端图完整清单：F140401_20前端下梁存在={bool(front_sill)}；"
                f"鹅颈槽/地板角钢 BOM {len(target_rows)} 项、模型 {len(target_parts)} 件；"
                f"海绵条 BOM {len(sponge_rows)} 项、模型 {len(sponge_parts)} 件；"
                f"结论={'20GP 不适用' if verdict == 'skipped' else conclusion}"
            ),
        },
    ]
    return RuleOutcome(verdict, conclusion, evidence)


def h_roof_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    code = "000A22G1R"
    checkpoint = rule.title.lstrip("*")
    facts = _facts(ctx, code)
    bom = next(
        (fact for fact in facts if fact.get("kind") == "bom" and fact.get("drawing_no") == "R010501"),
        None,
    )
    balloon = next(
        (
            fact for fact in facts
            if fact.get("kind") == "note"
            and str(fact.get("text", "")).strip() == str((bom or {}).get("item", "1"))
            and "R010501" in _attachment_text(fact)
        ),
        None,
    )
    panels = [
        comp for comp in ctx.spatial.get(code, [])
        if comp.get("leaf") and comp.get("name", "").rsplit("/", 1)[-1].startswith("R010501_")
    ]
    property_notes: set[str] = set()
    api_path = ctx.api_paths.get(code, "")
    if api_path:
        try:
            with open(api_path, "rb") as handle:
                for component in ijson.items(handle, "referencedComponents.item"):
                    if not str(component.get("name", "")).rsplit("/", 1)[-1].startswith("R010501_"):
                        continue
                    property_notes.update(
                        str(prop.get("resolvedValue", "") or "").strip()
                        for prop in component.get("customProperties", [])
                        if prop.get("name") == "备注" and prop.get("resolvedValue")
                    )
        except OSError:
            property_notes = set()

    one_press = any(re.search(r"一次(?:压型|成型)|one[- ]?piece", note, re.IGNORECASE) for note in property_notes)
    passed = bool(one_press and bom and balloon and len(panels) == 5)
    verdict = "pass" if passed else "warning"

    def add(item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    evidence: list[dict] = []
    add({
        "type": "input",
        "side": "manual",
        "text": "判定规则：R010501 零件属性明确标识“一次压型/一次成型”，且 BOM 与图面气泡均绑定该零件",
    })
    if bom:
        item = _fact_evidence(ctx, code, bom)
        item["text"] = (
            f"顶板图 BOM 序号 {bom.get('item')}：名称={bom.get('description')}；图号={bom.get('drawing_no')}；"
            f"材质={bom.get('material')}；规格={bom.get('specification')}；数量={bom.get('quantity')}"
        )
        add(item)
    if balloon:
        item = _fact_evidence(ctx, code, balloon)
        item["text"] = f"工程图视图1：气泡 1；绑定={_attachment_text(balloon)}"
        add(item)
    add({
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(code, ""),
        "text": (
            f"R010501 零件属性：备注={sorted(property_notes) or '未提取'}；"
            f"叶子件={len(panels)}；钣金厚度={sorted(_thickness_set(panels))}mm"
        ),
    })
    return RuleOutcome(
        verdict,
        "R010501 顶板零件属性明确为五波一次压型，BOM、图面气泡与模型证据一致"
        if passed else "R010501 缺少一次压型属性、BOM、图面绑定或完整模型数量，需人工确认",
        evidence,
    )


def _part_name(value: str) -> str:
    return re.sub(r"-\d+$", "", value.split("/")[-1].split("@")[0]).strip()


def _part_code(value: str) -> str:
    match = re.search(r"[A-Z]\d{6}", _part_name(value).upper())
    return match.group(0) if match else ""


def _attachment_codes(fact: dict) -> set[str]:
    return {
        code
        for attachment in fact.get("attachments") or []
        for code in (
            _part_code(str(attachment.get("component_name") or "")),
            _part_code(str(attachment.get("drawing_component_name") or "")),
        )
        if code
    }


_WELD_TOKEN_RE = re.compile(r"<(?:J?WELD|WLDSI)|焊(?:缝|接)|\bWELD(?:ING)?\b", re.IGNORECASE)
_MECHANICAL_JOIN_RE = re.compile(r"螺栓|螺钉|铆钉|紧固件|插销|销轴|\bBOLT\b|\bSCREW\b|\bRIVET\b|\bPIN\b", re.IGNORECASE)


def _welding_facts(ctx, code: str) -> list[dict]:  # noqa: ANN001
    return [
        fact
        for fact in _facts(ctx, code, "weld")
        if _WELD_TOKEN_RE.search(str(fact.get("text") or ""))
        and not _fact_matches(fact, "sealing", "sealant", "密封", "打胶", "涂胶")
    ]


def _weld_pair(relation: dict) -> tuple[str, str] | None:
    processes = {str(value).upper() for value in relation.get("requiredProcesses") or []}
    names = f"{relation.get('nameA', '')} {relation.get('nameB', '')}"
    if "WELD" not in processes or not relation.get("adjacent") or _MECHANICAL_JOIN_RE.search(names):
        return None
    codes = tuple(sorted((_part_code(str(relation.get("nameA") or "")), _part_code(str(relation.get("nameB") or "")))))
    return codes if all(codes) and codes[0] != codes[1] else None


def _weld_evidence(
    ctx,  # noqa: ANN001
    code: str,
    label: str,
    status: str,
    basis: str,
    drawing: str,
    fact: dict | None = None,
) -> dict:
    return {
        "type": "pdf",
        "side": "drawing",
        "file_id": ctx.pdf_file_ids.get(code, ""),
        "page": fact.get("page", 1) if fact else 1,
        "rect": fact.get("rect") if fact else None,
        "text": f"焊接检查｜状态={status}｜部位={label}｜依据={basis}｜图面={drawing}"[:400],
        "precision": "approximate",
    }


def h_gen_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    codes = [
        code
        for code in rule.target_files
        if code not in ("manual", "trademark", "000A22G1G")
    ]
    tolerance = float(rule.params.get("tolerance_mm", 0.01))
    evidence: list[dict] = []
    failures: list[str] = []
    unresolved: list[str] = []
    checked = 0

    for code in codes:
        sheet_metal_codes = {
            part_code
            for comp in ctx.spatial.get(code, [])
            if comp.get("thicknesses")
            if (part_code := _part_code(str(comp.get("name") or "")))
        }
        if not sheet_metal_codes:
            unresolved.append(f"{code}: spatial 未识别到钣金件")
            continue

        facts = ctx.get_drawing_facts(code)
        bom_rows = [
            fact
            for fact in facts
            if fact.get("kind") == "bom" and fact.get("drawing_no") in sheet_metal_codes
        ]
        if not bom_rows:
            unresolved.append(f"{code}: BOM 未找到可与 spatial 对应的钣金件")
            continue
        notes = [fact for fact in facts if fact.get("kind") == "note"]
        dimensions = [fact for fact in facts if fact.get("kind") == "dimension"]

        for row in bom_rows:
            item = str(row.get("item") or "")
            part_code = str(row.get("drawing_no") or "")
            match = re.search(r"[-+]?\d+(?:\.\d+)?", str(row.get("specification") or ""))
            evidence.append(_fact_evidence(ctx, code, row))
            if match is None:
                unresolved.append(f"{code} 序号{item} {part_code}: BOM 厚度无法解析")
                continue
            bom_thickness = float(match.group(0))
            balloon = next(
                (
                    fact
                    for fact in notes
                    if str(fact.get("text") or "").strip() == item
                    and fact.get("attached")
                    and part_code in _attachment_codes(fact)
                ),
                None,
            )
            if balloon is None:
                unresolved.append(f"{code} 序号{item} {part_code}: 序号气泡未绑定该零件")
                continue

            checked += 1
            thickness_fact = next(
                (
                    fact
                    for fact in dimensions
                    if fact.get("attached")
                    and part_code in _attachment_codes(fact)
                    and any(
                        abs(float(value) - bom_thickness) <= tolerance
                        for value in fact.get("values") or []
                    )
                ),
                None,
            )
            if thickness_fact is None:
                failures.append(f"{code} 序号{item} {part_code}（BOM {bom_thickness:g}mm）")
                balloon_evidence = _fact_evidence(ctx, code, balloon)
                balloon_evidence["text"] = (
                    f"{balloon.get('view') or balloon.get('sheet')}: 图中序号 {item}；"
                    f"绑定={_attachment_text(balloon)}"
                )[:400]
                evidence.append(balloon_evidence)
                continue
            evidence.append(_fact_evidence(ctx, code, thickness_fact))

    if failures:
        detail = "；".join(failures[:8])
        suffix = f"；另有 {len(unresolved)} 项数据链不完整" if unresolved else ""
        return RuleOutcome(
            "fail",
            f"{len(failures)}/{checked} 项钣金件未找到与 BOM 一致的绑定厚度标注：{detail}{suffix}",
            evidence,
        )
    if unresolved:
        return RuleOutcome("warning", f"钣金厚度审核数据链不完整：{'；'.join(unresolved[:8])}", evidence)
    if checked == 0:
        return RuleOutcome("warning", "未找到可审核的非总图钣金件", evidence)
    return RuleOutcome("pass", f"非总图 {len(codes)} 张图共 {checked} 项钣金件厚度标注均与 BOM 一致", evidence)


def h_gen_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    codes = [code for code in rule.target_files if code not in ("manual", "trademark")]
    evidence: list[dict] = []
    missing: list[str] = []
    invalid: list[str] = []
    conditional: list[str] = []
    found = 0
    observed = 0

    for code in codes:
        facts = _sealing_facts(ctx, code)
        source_text = " ".join(
            [
                str(value)
                for fact in ctx.get_drawing_facts(code)
                if fact.get("kind") == "bom"
                for value in (fact.get("drawing_no", ""), fact.get("description", ""))
            ]
            + [str(comp.get("name") or "") for comp in ctx.spatial.get(code, [])]
        ).lower()
        matched: set[int] = set()

        for label, section, presence_keywords, binding_keywords, is_conditional in _SEALANT_TARGETS.get(code, ()):
            if not any(keyword.lower() in source_text for keyword in presence_keywords):
                continue
            fact = next(
                (
                    item
                    for item in facts
                    if item.get("attached") and _fact_matches(item, *binding_keywords)
                ),
                None,
            )
            if fact:
                found += 1
                matched.add(id(fact))
                evidence.append(_sealant_evidence(ctx, code, label, section, "已标注", fact))
            elif is_conditional:
                conditional.append(f"{code} {label}")
                evidence.append(_sealant_evidence(ctx, code, label, section, "待确认"))
            else:
                missing.append(f"{code} {label}")
                evidence.append(_sealant_evidence(ctx, code, label, section, "缺失"))

        for index, fact in enumerate(facts, start=1):
            if id(fact) in matched:
                continue
            observed += 1
            status = "已标注" if fact.get("attached") else "绑定无效"
            evidence.append(
                _sealant_evidence(ctx, code, f"图面现有打胶标注 {index}", "图纸原有标注", status, fact)
            )
            if not fact.get("attached"):
                invalid.append(f"{code} 第{index}条打胶标注绑定无效")

    if missing or invalid:
        problems = missing + invalid
        detail = "；".join(problems[:8])
        suffix = f"；另有 {len(problems) - 8} 项" if len(problems) > 8 else ""
        return RuleOutcome(
            "fail",
            f"依据 Q/CIMC 40103-2007，发现 {len(missing)} 个应打胶部位未标注、{len(invalid)} 条图面标注绑定无效；已确认 {found} 个规范部位、另展示 {observed} 条图面打胶标注：{detail}{suffix}",
            evidence,
        )
    if conditional:
        return RuleOutcome(
            "warning",
            f"已确认 {found} 个规范部位，另展示 {observed} 条图面打胶标注；{len(conditional)} 个条件项需结合产品规范确认",
            evidence,
        )
    if not evidence:
        return RuleOutcome("warning", "未找到可审核的非商标图打胶部位或图面标注", evidence)
    return RuleOutcome("pass", f"{len(codes)} 张非商标图的 {found} 个规范打胶部位均已标注，另展示 {observed} 条图面标注", evidence)


def h_gen_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    codes = [code for code in rule.target_files if code not in ("manual", "trademark")]
    evidence: list[dict] = []
    missing: list[str] = []
    invalid: list[str] = []
    unavailable: list[str] = []
    expected = confirmed = observed = 0

    for code in codes:
        facts = _welding_facts(ctx, code)
        valid = [fact for fact in facts if fact.get("attached") and fact.get("leaders")]
        groups: dict[tuple[str, str], dict] = {}
        for relation in ctx.get_pair_relations(code):
            pair = _weld_pair(relation)
            if pair:
                groups.setdefault(pair, {"relation": relation, "count": 0})["count"] += 1

        matched: set[int] = set()
        if not groups:
            unavailable.append(code)
            evidence.append(
                _weld_evidence(
                    ctx,
                    code,
                    "应焊连接候选全集",
                    "待确认",
                    "项目判定原则：金属件非机械连接时应提供焊接连接证据",
                    "spatial 未生成相邻 WELD pairRelations；仅能展示图面已有焊接符号",
                )
            )

        for pair, group in groups.items():
            expected += 1
            pair_set = set(pair)
            bound = next((fact for fact in valid if pair_set & _attachment_codes(fact)), None)
            label = f"{pair[0]} ↔ {pair[1]}（{group['count']} 处相邻连接）"
            basis = (
                "项目制图规则：焊接符号只需绑定应焊连接任一端零件；"
                "spatial requiredProcesses=WELD；符号表达参考 AWS A2.4 / ISO 2553"
            )
            if bound:
                confirmed += 1
                matched.add(id(bound))
                bound_codes = sorted(pair_set & _attachment_codes(bound))
                drawing = f"{_fact_evidence(ctx, code, bound)['text']}；已绑定连接端={','.join(bound_codes)}"
                evidence.append(_weld_evidence(ctx, code, label, "已标注", basis, drawing, bound))
            else:
                missing.append(f"{code} {pair[0]}↔{pair[1]}")
                evidence.append(_weld_evidence(ctx, code, label, "缺失", basis, "图中未找到绑定到该连接任一端的焊接符号"))

        for index, fact in enumerate(facts, start=1):
            if id(fact) in matched:
                continue
            observed += 1
            is_valid = fact in valid
            status = "已标注" if is_valid else "绑定无效"
            drawing = _fact_evidence(ctx, code, fact)["text"]
            evidence.append(_weld_evidence(ctx, code, f"图面现有焊接标注 {index}", status, "图纸原有焊接符号", drawing, fact))
            if not is_valid:
                invalid.append(f"{code} 第{index}条焊接标注绑定无效")

    if missing or invalid:
        problems = missing + invalid
        detail = "；".join(problems[:8])
        suffix = f"；另有 {len(problems) - 8} 项" if len(problems) > 8 else ""
        return RuleOutcome(
            "fail",
            f"发现 {len(missing)} 个应焊连接未标注、{len(invalid)} 条焊接符号绑定无效；"
            f"已确认 {confirmed}/{expected} 个连接，{len(unavailable)} 张图缺少候选全集，"
            f"展示 {observed} 条图面标注：{detail}{suffix}",
            evidence,
        )
    if unavailable:
        return RuleOutcome(
            "warning",
            f"已确认 {confirmed}/{expected} 个应焊连接；{len(unavailable)} 张图缺少应焊候选全集，"
            f"另展示 {observed} 条图面标注",
            evidence,
        )
    if not evidence:
        return RuleOutcome("warning", "未找到可审核的非商标图焊接连接或图面标注", evidence)
    return RuleOutcome("pass", f"{len(codes)} 张非商标图的 {expected} 个应焊连接均有任一端绑定焊接符号", evidence)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
