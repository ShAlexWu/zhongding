# -*- coding: utf-8 -*-
"""QWEN 多模态图片解读（用于上传后判模式 + 文本模式产出 md）。

参考 SampleCode/ParsePicBase64ViaQwen.py：
一次 qwen3.7-plus 调用，prompt 让模型——
- 若图片「全是文字、没有机械零件设计的图形」：返回保留文本/表格格式的 markdown；
- 否则：返回数值 0。

因此返回 "0" => 图形模式；否则返回内容即文本模式的 md。
"""

import base64
import os
from typing import Tuple

from dashscope import MultiModalConversation

import config

# 与样例一致的判别 + 产出 prompt
INTERPRET_PROMPT = (
    "仔细观察图片中的内容，如果全是文字而没有机械零件设计的图形，"
    "返回 markdown 格式，保留文本和表格的格式，否则返回数值 0。"
    "不要输出推理和思考过程，直接输出结果。"
)

QWEN_MODEL = "qwen3.7-plus"

# 「基础解读」逐视图解读 prompt（取自 PaddleOCR 参考实现）：
# 对单张裁切视图，输出「## 尺寸、标注提取」markdown 表格。
VIEW_PROMPT = (
    "这是一个机械制造图纸中的视图，请解读其中的内容（视图含义、尺寸、角度、弧度等和设计有关的"
    "参数和说明），返回 markdown 表格，样例如下：\n"
    "### 尺寸、标注提取\n"
    "| 提取项 | 原图数值或文本 | 含义解读 |\n"
    "|---|---|---|\n"
    "| 直径尺寸；关键特性标识 | `Ø21±0.3☆` | 中心圆/孔相关直径为 21，公差 ±0.3；`☆` 表示关键特性。 |\n"
    "| 圆弧半径尺寸 | `R23.25±0.5` | 外轮廓圆弧半径为 23.25，公差 ±0.5。 |\n"
    "| 竖向尺寸 | `22.5±0.5` | 右侧竖向高度/边界尺寸为 22.5，公差 ±0.5。 |"
)


def _call_qwen(image_path: str, prompt: str) -> str:
    """对单张图片调用 QWEN 多模态模型，返回去除首尾空白后的文本结果。"""
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用 QWEN。")

    ext = (os.path.splitext(image_path)[1].lstrip(".").lower() or "png")
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    image_uri = f"data:image/{ext};base64,{image_data}"

    messages = [
        {
            "role": "user",
            "content": [
                {"image": image_uri},
                {"text": prompt},
            ],
        }
    ]

    resp = MultiModalConversation.call(
        api_key=config.DASHSCOPE_API_KEY,
        model=QWEN_MODEL,
        messages=messages,
        enable_thinking=False,  # 关闭思考模式，直接输出结果
    )
    if resp.status_code != 200:
        raise RuntimeError(f"QWEN 调用失败: {resp.code}, {resp.message}")

    content = resp.output.choices[0].message.content
    text = ""
    for item in content:
        if isinstance(item, dict) and "text" in item:
            text += item["text"]
        elif isinstance(item, str):
            text += item
    return text.strip()


def interpret_view(image_path: str) -> str:
    """「基础解读」用：对单张裁切视图做逐视图解读，返回 markdown 表格文本。"""
    return _call_qwen(image_path, VIEW_PROMPT)


def interpret_image(image_path: str) -> str:
    """调用 QWEN 解读图片，返回去除首尾空白后的文本结果。"""
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用 QWEN。")

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")
    # DashScope 多模态接口需要 data URI（带 mime 前缀），而非裸 base64
    image_uri = f"data:image/png;base64,{image_data}"

    messages = [
        {
            "role": "user",
            "content": [
                {"image": image_uri},
                {"text": INTERPRET_PROMPT},
            ],
        }
    ]

    resp = MultiModalConversation.call(
        api_key=config.DASHSCOPE_API_KEY,
        model=QWEN_MODEL,
        messages=messages,
        enable_thinking=False,  # 关闭思考模式，直接输出结果
    )
    if resp.status_code != 200:
        raise RuntimeError(f"QWEN 调用失败: {resp.code}, {resp.message}")

    content = resp.output.choices[0].message.content
    # content 是一个列表，取其中的文本片段拼接
    text = ""
    for item in content:
        if isinstance(item, dict) and "text" in item:
            text += item["text"]
        elif isinstance(item, str):
            text += item
    return text.strip()


def interpret_and_decide(image_path: str) -> Tuple[bool, str]:
    """返回 (is_graphic, raw_text)。

    is_graphic=True 表示图片含机械零件设计图形（QWEN 返回 "0"），走图形模式；
    否则 raw_text 即文本模式所用的 markdown。
    """
    raw = interpret_image(image_path)
    is_graphic = raw.strip().strip("`").strip() == "0"
    return is_graphic, raw


# 「文档生成」用：从图纸片段里抽取/回答 NL 问题（纯文本 LLM）
_EXTRACT_SYSTEM = (
    "你是图纸信息抽取助手。只依据用户提供的图纸文本片段回答问题，"
    "不得编造片段中没有的信息。直接给出问题所要的结果本身（可用简洁 markdown），"
    "不要输出推理过程或多余解释；若片段中找不到，回答“未找到”。"
)


def _call_text(prompt: str) -> str:
    """纯文本 LLM 调用，复用与图像解读一致的 MultiModalConversation 范式。"""
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用 QWEN。")

    resp = MultiModalConversation.call(
        api_key=config.DASHSCOPE_API_KEY,
        model=QWEN_MODEL,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        enable_thinking=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"QWEN 调用失败: {resp.code}, {resp.message}")

    content = resp.output.choices[0].message.content
    text = ""
    for item in content:
        if isinstance(item, dict) and "text" in item:
            text += item["text"]
        elif isinstance(item, str):
            text += item
    return text.strip()


def extract_field(query: str, context: str) -> str:
    """根据 NL 问题 query，从图纸片段 context 中抽取/回答，返回结果文本。"""
    prompt = f"{_EXTRACT_SYSTEM}\n\n图纸片段：\n{context}\n\n问题：{query}"
    return _call_text(prompt)


def extract_doc_fields(prompt: str) -> str:
    """「文档生成」用：传入已完善的字段抽取 prompt（含规则 + JSON 要求 + 图纸上下文），
    返回 LLM 原始文本（期望是一个 JSON）。"""
    return _call_text(prompt)
