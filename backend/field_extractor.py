# -*- coding: utf-8 -*-
"""「文档生成 - 生产检验指导书」字段抽取：基于 view_text_vec 的 RAG。

针对指定图纸，对用户的自然语言问题做：
  1. 用 text-embedding-v4 向量化问题；
  2. 在该图纸的分段向量里 cosine_similarity 取 top-K 候选分段；
  3. 把候选分段交给 QWEN 文本模型抽取/回答精确结果。

为什么取 top-K 而非 top-1：实测纯 top-1 会被语义相近但不含值的分段抢走
（如「产品号」被「产品信息表/代用品表」抢走），含值的「标题栏」分段稳定在 top-2，
故召回 top-K 进候选，再由 LLM 从中抽取。
"""

import json
import re
from typing import List, Optional, Tuple

import config
import embedding_client
import mo_db
import qwen_client


def _top_chunks(conn, diagram_name: str, vec, k: int) -> List[Tuple[str, str, float]]:
    """在某图纸范围内，按 cosine_similarity 取前 k 个分段。

    返回 [(分段名, 分段原文, 相似度)]，按相似度降序。
    """
    cur = conn.cursor()
    lit = mo_db.vec_to_literal(vec)
    cur.execute(
        f"SELECT chunk_name, chunk_text, cosine_similarity(vec_value, %s) AS sim "
        f"FROM {config.TEXT_VEC_TABLE} WHERE diagram_name = %s "
        f"ORDER BY sim DESC LIMIT %s",
        (lit, diagram_name, k),
    )
    return [(r[0], (r[1] or ""), float(r[2])) for r in cur.fetchall()]


def extract_field(diagram_name: str, query: str, top_k: int = 4) -> str:
    """针对 diagram_name，按 NL 问题 query 做 RAG 抽取，返回结果文本。"""
    query = (query or "").strip()
    if not query:
        return ""

    vec = embedding_client.embed_text(query)
    conn = mo_db.connect()
    try:
        chunks = _top_chunks(conn, diagram_name, vec, top_k)
    finally:
        conn.close()

    if not chunks:
        return "未找到（该图纸暂无可检索的文本分段）"

    context = "\n\n".join(f"【{name}】\n{text}" for name, text, _ in chunks)
    return qwen_client.extract_field(query, context)


# ---------------------------------------------------------------------------
# 「文档生成 - 生产检验指导书」：整图全分段为上下文，LLM 抽成结构化 JSON
# ---------------------------------------------------------------------------

# 默认抽取提示词（完整发送给 QWEN 的提示词模板，前端可编辑、不写死在代码里）。
# 其中 {图纸内容} 是占位符，后端在调用时填入该图纸的全分段文本。
# 注意：输出格式（JSON key、geometric_dims 为对象数组）与后端组装逻辑耦合，
# 用户可改措辞/规则，但若改坏了 key/格式，后端组装可能失败。
DEFAULT_DOC_PROMPT = (
    "你是图纸信息抽取助手。请仅依据下面给出的图纸内容，按抽取规则提取信息，"
    "不得编造图纸中没有的内容。\n\n"
    "抽取规则：\n"
    "文件编号：取该图纸中的“产品号”中横线左侧的部分，再拼接“ D01”\n"
    "过程名称：半成品检验\n"
    "版本编号：取“版本号”的个数，如果是 1，则返回 S01，如果是 2，则返回 S02，以此类推\n"
    "工序流程：外协检验→工序检验→修边→外观100%检验→成品检验→涂胶→二次外观检→包装→出厂检验→入库\n"
    "产品图号：取该图纸中的“客户图号”\n"
    "产品名称：取该图纸中的“产品名称”\n"
    "产品规格：取该图纸中的“产品规格”，取不到则设置斜杠\\\n"
    "发布/修订日期：取图纸中的“发布/修改日期”\n"
    "材料：取该图纸中的含“材料”列的表中的所有“名称”列内容\n"
    "产品颜色：取图纸中的“产品颜色”，取不到则设置为“黑色”\n"
    "产品净重：取该图纸中的“产品质量”\n"
    "顾客代码：取该图纸中的“图号”中括号内的内容\n"
    "编制：取“设计”一栏中的人名\n"
    "日期：取“设计”一栏中的日期\n"
    "几何尺寸：取该图纸中的所有尺寸数值（必须是数字加正负误差），仅取数值、不要尺寸名称。"
    "对每个尺寸，以一行的形式分别给出以下各列（同一尺寸的各列对应同一行）：\n"
    "  · 检验项目：该尺寸数值（若含五角星符号 ★/☆，不要放在此列）；\n"
    "  · 特性标识：若该尺寸标注了五角星 ★/☆ 则填该符号，否则留空；\n"
    "  · 检验设施/器具：取图纸中的“画法“相关信息，例如：投影对应投影仪，取不到留空；\n"
    "  · 检验频次：该尺寸对应的检验频次，取不到则设置为“首末模/批”\n"
    "外观：取该图纸中“技术要求/参数/规则”中和外观有关的内容（如：飞边要求、产品表面做标识等要求）。对每项内容，以一行的形式分别给出以下各列（同一项的各列对应同一行）："
    "  · 检验项目：即项内容；\n"
    "  · 特性标识：留空；\n"
    "  · 检验设施/器具：由两部分拼接，首先是“目测“，这个始终存在，其次，取图纸中的“画法“相关信息，例如：投影对应投影仪，取不到留空，样例：目测 + 投影仪；\n"
    "  · 检验频次：该尺寸对应的检验频次，取不到则设置为“首模/每班、末模/每批” \n\n"

    "输出要求：请严格只输出一个 JSON 对象（不要任何解释、不要 markdown 代码围栏），"
    "key 用下列英文标识：\n"
    "file_no（文件编号），process_name（过程名称），version_no（版本编号），"
    "process_flow（工序流程），product_drawing_no（产品图号），product_name（产品名称），"
    "product_spec（产品规格），publish_date（发布日期），material（材料），product_color（产品颜色），"
    "product_weight（产品净重），customer_code（顾客代码），geometric_dims（几何尺寸），"
    "outline_dims（外观）、编制（author）、日期（author_date），\n"
    "除 geometric_dims 外，每个字段的 value 是字符串。\n"
    "geometric_dims 的 value 必须是一个【对象数组】，每个对象代表一个尺寸（即检验项目表里的一行），"
    "同一尺寸的各列须放在同一个对象里、不得错位。每个对象固定含这 4 个 key："
    "“检验项目”、“特性标识”、“检验设施/器具”、“检验频次”。\n"
    "取不到的字符串字段用空字符串，geometric_dims 用 []。\n"
    "outline_dims 的 value 必须是一个【对象数组】，每个对象代表一个外观项（即检验项目表里的一行），"
    "同一外观项的各列须放在同一个对象里、不得错位。每个对象固定含这 4 个 key："
    "“检验项目”、“特性标识”、“检验设施/器具”、“检验频次”。"
    "取不到用 []。\n\n"
    "图纸内容：\n{图纸内容}"
)


def _all_chunks_text(conn, diagram_name: str) -> str:
    """读取该图纸在 view_text_vec 的全部分段正文，拼成完整上下文。"""
    cur = conn.cursor()
    cur.execute(
        f"SELECT chunk_name, chunk_text FROM {config.TEXT_VEC_TABLE} "
        f"WHERE diagram_name = %s",
        (diagram_name,),
    )
    return "\n\n".join(f"【{n}】\n{t or ''}" for n, t in cur.fetchall())


def _build_doc_prompt(prompt_template: str, context: str) -> str:
    """把图纸内容填入提示词模板（{图纸内容} 占位；缺占位符时追加到末尾）。"""
    if "{图纸内容}" in prompt_template:
        return prompt_template.replace("{图纸内容}", context)
    return f"{prompt_template}\n\n图纸内容：\n{context}"


def _parse_json_obj(text: str) -> dict:
    """从 LLM 文本里稳健解析出一个 JSON 对象（容错围栏/多余文字）。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 统一大 JSON（对照模版）：顶层字段 + 检验项目嵌套数组
# ---------------------------------------------------------------------------

_STARS = "★☆⭐✦✪✩⭑"  # 五角星类符号（特性标识）

# 检验项目行的统一列 key（4 类一致；不含「备注」——备注是跨类合并注释）
INSPECTION_COLS = ["序号", "检验项目", "特性标识", "检验设施/器具", "检验频次"]

# 顶层字段：LLM 英文 key -> 统一 JSON 中文 key（不含 geometric_dims，单独转行）
_FLAT_MAP = [
    ("file_no", "文件编号"),
    ("process_name", "过程名称"),
    ("version_no", "版本编号"),
    ("process_flow", "工序流程"),
    ("product_drawing_no", "产品图号"),
    ("product_name", "产品名称"),
    ("product_spec", "产品规格"),
    ("publish_date", "发布日期"),
    ("material", "材料"),
    ("product_color", "产品颜色"),
    ("product_weight", "产品净重"),
    ("customer_code", "顾客代码"),
    ("author", "编制"),
    ("author_date", "日期"),
]


def _row(seq="", item="", char="", facility="", freq=""):
    return {
        "序号": seq, "检验项目": item, "特性标识": char,
        "检验设施/器具": facility, "检验频次": freq,
    }


# 外观固定首/末项（中间项由 RAG+LLM 抽取，见 outline_dims）；排版顺序：首项→中间→末项
_APPEARANCE_FIRST = _row(
    item="产品表面无有害的气泡、欠硫、缺料、硫痕、裂口等缺陷",
    facility="目测",
    freq="首模/每班、末模/每批",
)
_APPEARANCE_LAST = _row(
    item="检查衬套是否缺骨架",
    facility="目测+按压",
    freq="首模/每班、末模/每批",
)

# 外观/性能/其他 默认明细（镜像模版实际默认行）
# 性能固化两行；外观仅作兜底（实际由 _outline_to_rows 动态组装）
DEFAULT_INSPECTION = {
    "外观": [dict(_APPEARANCE_FIRST), dict(_APPEARANCE_LAST)],
    "性能": [
        _row(1, "径向静刚度，扭转刚度", facility="MTS831", freq="1件/批"),
        _row(2, "产品粘接实验", facility="MTS831", freq="1件/批"),
    ],
    "其他": [_row("", "")],
}


def _dims_to_rows(dims) -> List[dict]:
    """把几何尺寸转成统一明细行（每尺寸一行、补序号、规范化 ★、补齐 5 列）。

    - 优先：dims 为【行对象数组】，每个对象含 检验项目/特性标识/检验设施·器具/检验频次
      （由 QWEN 逐尺寸产出，保证行内多列对齐）；
    - 兜底：dims 为字符串数组 或 逗号/顿号串时，按"每个尺寸只有检验项目"处理。
    规范化：把残留在「检验项目」里的星号移到「特性标识」。
    """
    raw: List[dict] = []
    if isinstance(dims, list):
        for d in dims:
            if isinstance(d, dict):
                raw.append(d)
            elif str(d).strip():
                raw.append({"检验项目": str(d).strip()})
    elif isinstance(dims, str):
        for d in dims.replace("，", ",").split(","):
            if d.strip():
                raw.append({"检验项目": d.strip()})

    rows = []
    for i, obj in enumerate(raw, 1):
        item = str(obj.get("检验项目", "") or "").strip()
        char = str(obj.get("特性标识", "") or "").strip()
        stars = "".join(ch for ch in item if ch in _STARS)
        if stars:  # 规范化：星号只应在特性标识列
            item = "".join(ch for ch in item if ch not in _STARS).strip()
            if not char:
                char = stars
        rows.append(_row(
            i, item, char,
            str(obj.get("检验设施/器具", "") or "").strip(),
            str(obj.get("检验频次", "") or "").strip(),
        ))
    return rows


def _outline_to_rows(dims) -> List[dict]:
    """组装外观明细：固定首项 + RAG/LLM 抽取的中间项 + 固定末项，统一补序号。

    中间项来自 LLM 的 outline_dims（行对象数组，含 4 列）；空检验项目的行丢弃。
    """
    middle: List[dict] = []
    if isinstance(dims, list):
        for d in dims:
            if isinstance(d, dict):
                item = str(d.get("检验项目", "") or "").strip()
                if not item:
                    continue
                middle.append(_row(
                    "", item,
                    str(d.get("特性标识", "") or "").strip(),
                    str(d.get("检验设施/器具", "") or "").strip(),
                    str(d.get("检验频次", "") or "").strip(),
                ))
            elif str(d).strip():
                middle.append(_row("", str(d).strip()))

    rows = [dict(_APPEARANCE_FIRST)] + middle + [dict(_APPEARANCE_LAST)]
    for i, r in enumerate(rows, 1):
        r["序号"] = i
    return rows


def extract_doc_struct(diagram_name: str, prompt: Optional[str] = None) -> dict:
    """针对图纸抽取，组装对照模版的统一大 JSON（两阶段：LLM 扁平 → 后端组装）。

    prompt 为完整提示词模板（前端可编辑，含 {图纸内容} 占位符）；不传则用默认模板。
    """
    prompt = (prompt or DEFAULT_DOC_PROMPT)
    conn = mo_db.connect()
    try:
        context = _all_chunks_text(conn, diagram_name)
    finally:
        conn.close()

    data = _parse_json_obj(qwen_client.extract_doc_fields(_build_doc_prompt(prompt, context)))

    result = {ck: str(data.get(ek, "") or "").strip() for ek, ck in _FLAT_MAP}
    result["检验项目"] = [
        {"项目": "几何尺寸", "明细": _dims_to_rows(data.get("geometric_dims", []))},
        {"项目": "外观", "明细": _outline_to_rows(data.get("outline_dims", []))},
        {"项目": "性能", "明细": [dict(r) for r in DEFAULT_INSPECTION["性能"]]},
        {"项目": "其他", "明细": [dict(r) for r in DEFAULT_INSPECTION["其他"]]},
    ]
    return result
