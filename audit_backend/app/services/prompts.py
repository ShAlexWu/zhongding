"""VLM prompt templates, keyed by rules_40.json params.prompt_id.

Every prompt forces a strict JSON schema output (SPEC section 11 pit 4):
  {"verdict": "pass|fail|warning", "confidence": 0-1,
   "evidence": [{"page": 1-based, "rect": {"x","y","w","h"} or null, "text": "原文"}],
   "facts": {},
   "conclusion": "一句话结论"}
{rule_title} and {doc_anchor} placeholders are injected by the rule engine.
"""

from __future__ import annotations

import json

_JSON_SCHEMA = """请严格按以下 JSON 结构输出（不要输出 JSON 之外任何文字）：
{
  "verdict": "pass" 或 "fail" 或 "warning",
  "confidence": 0.0 到 1.0 的小数,
  "evidence": [{"page": 页码(整数, 1 起), "rect": {"x": 0, "y": 0, "w": 0, "h": 0} 或 null, "text": "图面证据原文(必填,尽量完整)"}],
  "facts": {},
  "conclusion": "一句话判定结论(中文)"
}
rect 必须使用整张输入图左上角为原点的 0..1 归一化坐标，框住最小充分证据。
"""

_BASE = "你是资深集装箱审图工程师。请审阅附带的所有工程图纸页面。判定规则：{rule_title}。\n"


def _tpl(instruction: str) -> str:
    return _BASE + instruction + "\n" + _JSON_SCHEMA


TEMPLATES: dict[str, str] = {
    "door_sealant_color": _tpl(
        "检查门端图密封胶颜色是否与外面漆颜色相符。外面漆颜色基准：{doc_anchor}（如无基准值请说明）。"
    ),
    "door_hinge_sealant_note": _tpl(
        "检查门铰链区域的打胶注释是否指向正确位置（注释引线应对准铰链/焊缝特征）。"
    ),
    "weld_symbol_offset": _tpl("检查焊接符号是否相对其指向的焊缝特征位置发生偏移。"),
    "vent_sealant_marked": _tpl("检查侧板通风器区域是否标注了打胶（sealant）注释。"),
    "vent_rivet_sealant_marked": _tpl("检查通风器铆钉是否标注打胶注释。"),
    "front_beam_weld_direction": _tpl(
        "检查 40HC 前端下梁是否标注了焊缝朝向。21A 数据集按图纸实际内容判定标注存在性与正确性。"
    ),
    "floor_support_sealant_note": _tpl(
        "检查塑料地板支撑是否有打胶注释，焊接区域是否有多余注释。"
    ),
    "gooseneck_sponge_needed": _tpl(
        "检查鹅颈槽地板角钢区域结构，判断该处是否需要配置海绵条，并检查是否已配置。"
    ),
    "roof_one_piece_formed": _tpl("核实顶板图是否为一次成型顶板图（标题栏/图纸内容判定）。"),
    "tm_four_views_vs_ga": _tpl(
        "输入是从两份原始矢量 PDF 以 4 倍分辨率直接渲染的局部裁切图。图片顺序固定："
        "1=总图门端，2=商标图门端，3=总图侧板，4=商标图侧板，"
        "5=总图前端，6=商标图前端，7=总图 SECTION D-D，8=商标图顶视图，"
        "9=商标图标题栏。必须只比较同一对图片，禁止跨视图补全。\n"
        "门端：必须在两份 PDF 中逐个清点左右门扇的长条把手，以把手数作为锁杆根数，"
        "并用顶部和底部凸轮托架交叉核对；比较锁杆水平位置、门板波形和门框结构。"
        "看不清时必须 warning，禁止按常见门型猜测。\n"
        "侧板：分别清点两张对应图中的通风器数量和横向位置，并比较波形数量、疏密和分组。"
        "贴花、箱号、铆钉、引出序号不是通风器。\n"
        "前端：分别清点通风器数量和位置，比较波形方向、分布、角柱及上下梁外轮廓。\n"
        "顶板：比较波形方向、重复分布、端部关系和整体外轮廓。\n"
        "按门端、侧板、前端、顶板四个视图分别判定；每个视图同时比较波形和通风器数量及位置。"
        "任一明确差异即该视图 fail；任一切片看不清即该视图 warning；两项均可读且一致才 pass。\n"
        "evidence 必须恰好 8 条，分别覆盖图片 1 至 8，每张切片只输出一条独立描述。"
        "page 填图片序号 1..8；rect 必须填 null，红框由后端直接使用裁切边界生成，不需要模型计算坐标。"
        "依次使用以下完整前缀，禁止输出字符 `|`："
        "`TM-01 VIEW general door_end:`、`TM-01 VIEW marking door_end:`、"
        "`TM-01 VIEW general side:`、`TM-01 VIEW marking side:`、"
        "`TM-01 VIEW general front_end:`、`TM-01 VIEW marking front_end:`、"
        "`TM-01 VIEW general roof:`、`TM-01 VIEW marking roof:`。"
        "每条只描述当前图片，必须同时写明：①波形方向、数量、疏密、分组和外轮廓；"
        "②通风器实际数量和位置。禁止复制对应切片或其他视图的描述。\n"
        "另外读取图片 9 商标图标题栏中的图号。facts 必须使用："
        '"door_end_verdict":"pass|fail|warning","door_end_conclusion":"门端波形和通风器对比结论",'
        '"side_verdict":"pass|fail|warning","side_conclusion":"侧板波形和通风器对比结论",'
        '"front_end_verdict":"pass|fail|warning","front_end_conclusion":"前端波形和通风器对比结论",'
        '"roof_verdict":"pass|fail|warning","roof_conclusion":"顶板波形和通风器对比结论",'
        '"marking_drawing_number":"标题栏图号原文", '
        '"marking_drawing_number_evidence":{"image_index":9,"rect":{"x":0,"y":0,"w":0,"h":0},'
        '"text":"标题栏图号原文"}。rect 使用 0..1 归一化最小框。'
        "无法可靠识别时图号填空字符串且 rect 填 null，不得根据文件名或总图 BOM 补写。"
    ),
    "tm_marking_color": _tpl(
        "检查商标图 ISO 标、重量标颜色是否与外面漆颜色匹配。外面漆颜色基准：{doc_anchor}（如无基准值请说明）。"
    ),
    "tm_customer_logo": _tpl(
        "重点检查客户 LOGO 是否与客户技术文件/信息要求一致。客户要求基准：{doc_anchor}（如无基准值请说明）。"
    ),
    "tm_weight_plate_value": _tpl(
        "附带的图片按顺序：第 1 张为商标图，第 2 张为总图（000A22G1G 总装配）。"
        "只提取两张图中 MAX GROSS、TARE、PAYLOAD/NET 的 kg 和 lb 数值，不直接作最终裁决。"
        "说明书基准值：{doc_anchor}。"
        "evidence 必须恰好 6 条，每个字段在两张图中各一条；page 填图片序号 1 或 2。"
        "text 依次以 `TM-04 marking max_gross:`、`TM-04 general max_gross:`、"
        "`TM-04 marking tare:`、`TM-04 general tare:`、`TM-04 marking payload:`、"
        "`TM-04 general payload:` 开头。"
        "facts 必须使用：{"
        "\"marking\":{\"max_gross_kg\":数值或null,\"max_gross_lb\":数值或null,"
        "\"tare_kg\":数值或null,\"tare_lb\":数值或null,"
        "\"payload_kg\":数值或null,\"payload_lb\":数值或null},"
        "\"general\":{\"max_gross_kg\":数值或null,\"max_gross_lb\":数值或null,"
        "\"tare_kg\":数值或null,\"tare_lb\":数值或null,"
        "\"payload_kg\":数值或null,\"payload_lb\":数值或null}}。"
    ),
    "tm_nameplate_customer": _tpl(
        "只检查铭牌上的客户公司名称和地址是否与基准准确、完整地一致。"
        "不得检查或评价板材厚度、油漆及供应商、质保期、重量等其他客户技术要求。"
        "客户信息基准：{doc_anchor}（缺少名称或地址基准时只能判 warning）。"
        "facts 必须使用：{\"customer_name\":{\"expected\":\"基准值\",\"actual\":\"图中值\","
        "\"verdict\":\"pass|fail|warning\",\"conclusion\":\"仅说明名称核对结果\"},"
        "\"customer_address\":{\"expected\":\"基准值\",\"actual\":\"图中值\","
        "\"verdict\":\"pass|fail|warning\",\"conclusion\":\"仅说明地址核对结果\"}}。"
        "名称和地址的 evidence.text 分别以 `TM-05 customer_name:` 和 `TM-05 customer_address:` 开头。"
    ),
    "tm_nameplate_test_values": _tpl(
        "只检查标题为 CSC SAFETY APPROVAL 的同一块铭牌，不能使用外部重量标或其他文字补全。"
        "说明书试验数值基准：{doc_anchor}。逐项读取并比较："
        "ALLOWABLE STACKING LOAD FOR 1.8G 的 kg 数值，以及 TRANSVERSE RACKING TEST FORCE 的 N 数值。"
        "两项都清晰且与基准一致才 pass；任一明确不一致则 fail；铭牌、字段或数值不清则 warning。"
        "evidence 必须恰好两条，rect 均为 0..1 归一化的最小字段框，text 分别以 "
        "`TM-06 CSC allowable_stacking_load_1_8g:` 和 "
        "`TM-06 CSC transverse_racking_test_force:` 开头。"
        "facts 必须使用：{"
        "\"csc_plate_title\":\"CSC SAFETY APPROVAL\","
        "\"csc_plate_rect\":{\"x\":0,\"y\":0,\"w\":0,\"h\":0},"
        "\"allowable_stacking_load_1_8g_kg\":数值或null,"
        "\"transverse_racking_test_force_n\":数值或null}。"
    ),
    "gen_sheet_metal_thickness": _tpl(
        "检查各视图钣金件厚度标注的完整性。spatial 抽取的厚度集合（应覆盖）：{doc_anchor}，请核对图面标注覆盖。"
    ),
    "gen_sealant_notes": _tpl("检查总装配图面打胶注释的完整性（是否有应标注而未标注的打胶部位）。"),
    "gen_weld_notes": _tpl("检查总装配图面焊接符号/注释的完整性。"),
}


def build_prompt(prompt_id: str, rule_title: str, doc_anchor: str = "") -> str:
    template = TEMPLATES.get(prompt_id)
    if template is None:
        template = _tpl("请按图纸实际内容进行审图判定。")
    return (
        template.replace("{rule_title}", rule_title)
        .replace("{doc_anchor}", doc_anchor or "未提供")
    )


def extract_json_payload(text: str) -> dict:
    """Parse model output; tolerate markdown fences and trailing prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("model output contains no JSON object")
    return json.loads(cleaned[start : end + 1])
