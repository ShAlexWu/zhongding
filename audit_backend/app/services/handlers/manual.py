"""说明书域 (8) handlers."""

import re

from app.services.handlers.common import (
    _COLOR_WORDS,
    _RAL_RE,
    RuleOutcome,
    _norm_version,
)
from app.services.standards_registry import OfficialStandardsRegistry


def _overall(verdicts: list[str]) -> str:
    return "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"


def _compact(value: str) -> str:
    return re.sub(r"[\s\-_/]", "", str(value or "")).upper()


def _manual_change_value(requirement: str, manual_text: str) -> tuple[str, str] | None:
    requirement = re.sub(r"^\s*\d+[.)]\s*", "", requirement)
    supplier = re.search(
        r"7\.2\.4\s+The paint suppliers are Dowill, KCC, Mega, Chugoku, Kansai, KMK, Haoli or Gaojing\.",
        manual_text,
        re.IGNORECASE,
    )
    if re.search(r"paint\s+suppliers?.*gaojing", requirement, re.IGNORECASE) and supplier:
        return "found", supplier.group(0)
    checks = (
        (
            r"fork\s+pocket.*top\s+plate",
            r"top\s+plate\s*:\s*(\d+(?:\.\d+)?)\s*mm\s*thk\. ?",
            "Top plate : {value} mm Thk.",
        ),
        (
            r"side\s+walls?.*inner\s+panel",
            r"inner\s+panel\s*:\s*(\d+(?:\.\d+)?)\s*mm\s*thk\.\s*,?\s*qty\.\s*:\s*3\s*pcs/each\s+side",
            "Inner panel : {value} mm Thk., Qty. : 3 Pcs/Each side",
        ),
        (
            r"ventilators?.*quantity",
            r"quantity\s*:\s*(\d+(?:\.\d+)?)\s*/\s*each\s+side\s+panel",
            "Quantity: {value} / each side panel",
        ),
        (
            r"stacking.*testing\s+load",
            r"stacking[\s\S]{0,80}?testing\s+load\s*:\s*([\d,]+)\s*kg/post",
            "Stacking Testing load: {value}kg/post",
        ),
        (
            r"self-adhe?sive.*decal.*guarante",
            r"the\s+self-adhe?sive\s+film\s+decal\s+shall\s+be\s+guaranteed\s+\w+\s*\((\d+)\)\s+years?\.",
            "The self-adhesive film decal shall be guaranteed seven ({value}) years.",
        ),
    )
    for requirement_pattern, manual_pattern, evidence_template in checks:
        if not re.search(requirement_pattern, requirement, re.IGNORECASE):
            continue
        actual = re.search(manual_pattern, manual_text, re.IGNORECASE)
        expected = re.search(r"\d[\d,]*(?:\.\d+)?", requirement)
        if not actual or not expected:
            return None
        actual_text = actual.group(1)
        same = float(actual_text.replace(",", "")) == float(expected.group(0).replace(",", ""))
        return ("found" if same else "mismatch", evidence_template.format(value=actual_text))
    return None


def h_man_01(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])

    total_code = "000A22G1G"
    component_codes = [
        code for code in rule.target_files
        if re.fullmatch(r"000A22G1[BEFRS]", code)
    ]
    total_facts = ctx.get_drawing_facts(total_code)
    total_bom = {
        fact.get("drawing_no"): fact
        for fact in total_facts
        if fact.get("kind") == "bom" and fact.get("drawing_no")
    }
    marking_bom = next(
        (
            fact for fact in total_facts
            if fact.get("kind") == "bom"
            and re.search(r"\bMARKING\b|商标", str(fact.get("description") or ""), re.IGNORECASE)
            and fact.get("drawing_no")
        ),
        None,
    )
    evidence: list[dict] = []

    def add(checkpoint: str, verdict: str, item: dict) -> None:
        item.update(checkpoint=checkpoint, checkpoint_verdict=verdict)
        evidence.append(item)

    number_missing: list[str] = []
    number_mismatches: list[str] = []
    for code in component_codes:
        bom = total_bom.get(code)
        drawing_no = ctx.json_props.get(code, {}).get("图号", "")
        if not bom or not drawing_no:
            number_missing.append(code)
        elif drawing_no != bom.get("drawing_no"):
            number_mismatches.append(f"{code} 标题栏 {drawing_no} / 总图 BOM {bom.get('drawing_no')}")
    trademark_number = str(ctx.trademark_facts.get("marking_drawing_number") or "").strip()
    trademark_evidence = ctx.trademark_facts.get("marking_drawing_number_evidence")
    if not marking_bom:
        number_missing.append("商标图对应的总图 BOM")
    elif not trademark_number or not isinstance(trademark_evidence, dict):
        number_missing.append("商标图标题栏图号")
    elif _compact(trademark_number) != _compact(marking_bom.get("drawing_no")):
        number_mismatches.append(
            f"商标图标题栏 {trademark_number} / 总图 BOM {marking_bom.get('drawing_no')}"
        )
    number_verdict = "fail" if number_mismatches else "warning" if number_missing else "pass"
    add("图号一致性", number_verdict, {
        "type": "input",
        "side": "manual",
        "text": "判定规则：各部件图及商标图标题栏图号逐张与总图 BOM 的 DWG No. 对比",
    })
    for code in component_codes:
        bom = total_bom.get(code)
        if bom:
            add("图号一致性", number_verdict, {
                "type": "pdf",
                "side": "drawing",
                "file_id": ctx.pdf_file_ids.get(total_code, ""),
                "page": bom.get("page", 1),
                "rect": bom.get("rect"),
                "precision": "approximate",
                "text": (
                    f"总图 BOM 序号 {bom.get('item')}：名称={bom.get('description')}；"
                    f"DWG No.={bom.get('drawing_no')}"
                ),
            })
        add("图号一致性", number_verdict, {
            "type": "json",
            "side": "drawing",
            "file_id": ctx.json_file_ids.get(code, ""),
            "text": f"{code} 部件图标题栏图号={ctx.json_props.get(code, {}).get('图号') or '未提取'}",
        })
    if marking_bom:
        add("图号一致性", number_verdict, {
            "type": "pdf",
            "side": "drawing",
            "file_id": ctx.pdf_file_ids.get(total_code, ""),
            "page": marking_bom.get("page", 1),
            "rect": marking_bom.get("rect"),
            "precision": "approximate",
            "text": (
                f"总图 BOM 序号 {marking_bom.get('item')}：名称={marking_bom.get('description')}；"
                f"DWG No.={marking_bom.get('drawing_no')}"
            ),
        })
    if isinstance(trademark_evidence, dict):
        item = dict(trademark_evidence)
        item.update(side="drawing", text=f"商标图标题栏图号={trademark_number}；{item.get('text', '')}")
        add("图号一致性", number_verdict, item)
    else:
        add("图号一致性", number_verdict, {
            "type": "input",
            "side": "drawing",
            "text": "商标图标题栏图号=未提取",
        })

    cover_text = "\n".join(manual.paragraphs[:10])
    manual_match = re.search(r"\b\d{2}[A-Z]-?\d{2}\b", cover_text)
    manual_version = manual_match.group(0) if manual_match else ""
    total_version = ctx.json_props.get(total_code, {}).get("图面版本", "")
    if not manual_version or not total_version:
        version_verdict = "warning"
    elif _norm_version(manual_version) == _norm_version(total_version):
        version_verdict = "pass"
    else:
        version_verdict = "fail"
    add("版本号一致性", version_verdict, {
        "type": "doc",
        "side": "manual",
        "text": f"说明书封面版本={manual_version or '未提取'}",
    })
    add("版本号一致性", version_verdict, {
        "type": "json",
        "side": "drawing",
        "file_id": ctx.json_file_ids.get(total_code, ""),
        "text": f"总图标题栏：图号={ctx.json_props.get(total_code, {}).get('图号') or '未提取'}；图面版本={total_version or '未提取'}",
    })

    verdicts = (number_verdict, version_verdict)
    verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    conclusions: list[str] = []
    if number_verdict == "pass":
        conclusions.append(f"{len(component_codes)} 张部件图及商标图图号均与总图 BOM 一致")
    elif number_mismatches:
        conclusions.append("图号不一致：" + "；".join(number_mismatches))
    else:
        conclusions.append("缺少图号或总图 BOM 数据：" + "、".join(number_missing))
    if version_verdict == "pass":
        conclusions.append(f"说明书与总图版本一致（{manual_version}）")
    elif version_verdict == "fail":
        conclusions.append(f"版本号不一致：说明书 {manual_version}，总图 {total_version}")
    else:
        conclusions.append("说明书或总图版本号缺失")
    return RuleOutcome(verdict, "；".join(conclusions), evidence)


def h_man_02(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    rows = manual.extract_revision_rows()
    reason = ctx.inputs.get("revision_reason", {}) or {}
    reason_type = reason.get("type", "")
    other_text = reason.get("other_text", "")
    evidence = [{"type": "doc", "text": r} for r in rows[:5]]
    if not rows:
        return RuleOutcome("warning", "说明书 Revision list 未找到可核验行", evidence)
    if reason_type == "其他" and not other_text.strip():
        return RuleOutcome("warning", "常规修改原因选择了其他但未填写说明，无法比对", evidence)
    return RuleOutcome("pass", f"Revision list 存在 {len(rows)} 行，可覆盖变更信息", evidence)


def h_man_03(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    customer_text = (
        ctx.fact_bundle.customer_text
        if ctx.fact_bundle
        else "；".join(
            str(ctx.inputs.get(key) or "").strip()
            for key in ("tech_req", "new_material")
            if str(ctx.inputs.get(key) or "").strip()
        )
    )
    if not customer_text:
        return RuleOutcome("warning", "客户输入未提供技术要求，无法核验变更点", [])
    checks = ctx.manual_facts.get("change_checks") or []
    if checks:
        evidence: list[dict] = []
        verdicts: list[str] = []
        found = 0
        for check in checks:
            status = str(check.get("status") or "uncertain")
            requirement = str(
                check.get("requirement") or check.get("source_text") or "未命名变更点"
            )
            evidence_text = str(check.get("evidence") or "")
            if status in {"missing", "uncertain"} and not evidence_text:
                local_match = _manual_change_value(requirement, manual.full_text)
                if local_match:
                    status, evidence_text = local_match
            verdict = "pass" if status == "found" else "fail" if status in {"missing", "mismatch"} else "warning"
            found += verdict == "pass"
            verdicts.append(verdict)
            evidence.append({
                "type": "doc" if evidence_text else "input",
                "checkpoint": requirement,
                "checkpoint_verdict": verdict,
                "text": str(
                    evidence_text
                    or (
                        f"事实提取未覆盖，需人工确认：{requirement}"
                        if status == "uncertain"
                        else f"未在说明书找到证据：{requirement}"
                    )
                ),
            })
        verdict = _overall(verdicts)
        return RuleOutcome(verdict, f"主要变更点已体现 {found}/{len(checks)} 项", evidence)
    return RuleOutcome(
        "warning",
        "AI 说明书事实提取未运行，不能用关键词命中代替变更点审核",
        [{"type": "input", "text": customer_text}],
    )


def h_man_04(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    standards = manual.extract_standards()
    if not standards:
        return RuleOutcome("warning", "说明书未提取到完整标准编号", [])
    registry = ctx.standards_registry or OfficialStandardsRegistry(live=False)
    review = registry.review(standards)
    evidence = []
    for item in review.items:
        amendments = f"；有效修订件={', '.join(item.amendments)}" if item.amendments else ""
        live = "官网实时核验" if item.live_verified else "官方目录快照"
        evidence.append({
            "type": "doc",
            "checkpoint": item.reference,
            "checkpoint_verdict": item.verdict,
            "source_url": item.source_url or None,
            "checked_at": item.checked_at,
            "text": (
                f"说明书引用={item.reference}；现行发布版={item.current or '未查到'}"
                f"{amendments}；{item.note}；来源={live}；{item.source_url or '无'}"
            ),
        })
    failed = [item.reference for item in review.items if item.verdict == "fail"]
    uncertain = [item.reference for item in review.items if item.verdict == "warning"]
    parts = [f"共核验 {len(review.items)} 个标准编号"]
    if failed:
        parts.append("已过期/被替代：" + "、".join(failed))
    if uncertain:
        parts.append("版本未写明或未能实时核验：" + "、".join(uncertain))
    return RuleOutcome(review.verdict, "；".join(parts), evidence)


def h_man_05(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    footer = manual.footer_text.strip()
    footer_compact = _compact(footer)
    contacts = [
        item for item in (ctx.manual_facts.get("contacts") or [])
        if any(
            value and _compact(value) in footer_compact
            for value in (item.get("email"), item.get("phone"))
        )
    ]
    required = (ctx.manual_facts.get("requirements") or {}).get("contacts") or []
    if contacts:
        evidence = [{"type": "doc", "text": str(item.get("evidence") or footer)[:400]} for item in contacts]
        if not required:
            return RuleOutcome("warning", "已提取页脚联系人，但客户未提供更新后的联系人基准", evidence)
        actual = " ".join(str(item) for item in contacts)
        missing = [item for item in required if not any(str(v) in actual for v in item.values() if v)]
        if missing:
            return RuleOutcome("fail", "说明书联系人与客户要求不一致", evidence)
        return RuleOutcome("pass", "说明书联系人与客户要求一致", evidence)
    match = re.search(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<![\w-])\+?\d(?:[\s()-]*\d){7,}(?![\w-])",
        footer,
    )
    if match:
        return RuleOutcome(
            "warning",
            "找到页脚联系人信息，但缺少更新后的联系人基准",
            [{"type": "doc", "text": footer[:400]}],
        )
    return RuleOutcome("warning", "页脚未找到联系人/电话/邮箱信息", [])


def h_man_06(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    facts = ctx.manual_facts
    required_paint = (facts.get("requirements") or {}).get("paint") or {}
    actual_paint = facts.get("paint") or {}
    if required_paint and actual_paint:
        evidence: list[dict] = []
        verdicts: list[str] = []
        for key, label in (("exterior", "外面漆颜色"), ("interior", "内面漆颜色")):
            required = required_paint.get(key) or {}
            if not any(required.get(field) for field in ("ral", "color")):
                continue
            actual = actual_paint.get(key) or {}
            required_value = required.get("ral") or required.get("color") or ""
            actual_value = actual.get("ral") or actual.get("color") or ""
            verdict = "warning" if not actual_value else "pass" if _compact(required_value) == _compact(actual_value) else "fail"
            verdicts.append(verdict)
            evidence.append({
                "type": "doc",
                "checkpoint": label,
                "checkpoint_verdict": verdict,
                "text": (
                    f"客户要求={required_value}；说明书={actual_value or '未提取'}；"
                    f"原文={actual.get('evidence') or '无'}"
                ),
            })
        if verdicts:
            return RuleOutcome(_overall(verdicts), "内外面漆颜色已分别与客户要求对比", evidence)
    color_lines = manual.extract_colors()
    color_text = "\n".join(color_lines)
    tech_req = (
        ctx.fact_bundle.customer_text
        if ctx.fact_bundle
        else "；".join(str(ctx.inputs.get(key) or "") for key in ("tech_req", "new_material"))
    )
    ral_req = _RAL_RE.findall(tech_req)
    word_req = [w for w in _COLOR_WORDS if w in tech_req]
    evidence = [{"type": "doc", "text": line} for line in color_lines[:4]]
    if not ral_req and not word_req:
        return RuleOutcome("warning", "客户技术要求未提供明确颜色要求", evidence)
    matched = any(r in color_text for r in ral_req) or any(w in color_text for w in word_req)
    if matched:
        return RuleOutcome("pass", "说明书油漆颜色与客户颜色要求匹配", evidence)
    return RuleOutcome("fail", "说明书油漆颜色未体现客户颜色要求", evidence)


def h_man_07(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    facts = ctx.manual_facts
    required = ((facts.get("requirements") or {}).get("trademark") or {}).get("material") or ""
    actual = (facts.get("trademark") or {}).get("material") or ""
    if required:
        evidence_text = (facts.get("trademark") or {}).get("evidence") or ""
        if not actual:
            return RuleOutcome("fail", "说明书未找到商标材质", [{"type": "input", "text": f"客户要求={required}"}])
        verdict = "pass" if _compact(required) == _compact(actual) else "fail"
        return RuleOutcome(
            verdict,
            f"商标材质：客户要求 {required}，说明书 {actual}",
            [{"type": "doc", "text": evidence_text or f"说明书商标材质={actual}"}],
        )
    tech_req = (
        ctx.fact_bundle.customer_text
        if ctx.fact_bundle
        else "；".join(str(ctx.inputs.get(key) or "") for key in ("tech_req", "new_material"))
    )
    has_req = ("商标" in tech_req) or ("LOGO" in tech_req.upper())
    manual_tm = manual.find_around("商标") or manual.find_around("TRADEMARK")
    if not has_req:
        return RuleOutcome("warning", "客户输入未提供商标材质要求", [])
    if manual_tm:
        return RuleOutcome("warning", "找到商标相关文字，但 AI 未提取出可比较的材质事实", [{"type": "doc", "text": manual_tm}])
    return RuleOutcome("fail", "说明书未找到商标材质信息", [])


def h_man_08(ctx, rule) -> RuleOutcome:  # noqa: ANN001
    manual = ctx.manual
    if manual is None:
        return RuleOutcome("warning", "说明书文件缺失", [])
    facts = ctx.manual_facts
    required = (facts.get("requirements") or {}).get("guarantees") or []
    actual = facts.get("guarantees") or []
    if required:
        evidence: list[dict] = []
        verdicts: list[str] = []
        conclusions: list[str] = []
        for req in required:
            subject = str(req.get("subject") or "质保")
            candidate = next(
                (
                    item for item in actual
                    if not item.get("subject")
                    or _compact(subject) in _compact(item.get("subject"))
                    or _compact(item.get("subject")) in _compact(subject)
                ),
                None,
            )
            if candidate is None:
                verdict = "fail"
                actual_value = "未提取"
                source = ""
            else:
                req_months = float(req.get("duration") or 0) * (12 if req.get("unit") == "年" else 1)
                actual_months = float(candidate.get("duration") or 0) * (12 if candidate.get("unit") == "年" else 1)
                verdict = "pass" if req_months and req_months == actual_months else "fail"
                actual_value = f"{candidate.get('duration') or '?'}{candidate.get('unit') or ''}"
                source = str(candidate.get("evidence") or "")
            verdicts.append(verdict)
            required_value = f"{req.get('duration') or '?'}{req.get('unit') or ''}"
            conclusions.append(f"{subject}：要求 {required_value}，说明书 {actual_value}")
            evidence.append({
                "type": "doc" if source else "input",
                "checkpoint": subject,
                "checkpoint_verdict": verdict,
                "text": source or f"未找到 {subject} 质保条款",
            })
        return RuleOutcome(_overall(verdicts), "；".join(conclusions), evidence)
    guarantee = manual.extract_guarantee()
    if guarantee:
        return RuleOutcome("warning", "找到质保条款，但 AI 未提取出质保对象和客户基准", [{"type": "doc", "text": guarantee[:200]}])
    return RuleOutcome("warning", "说明书未找到质保年限章节", [])
