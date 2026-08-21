# -*- coding: utf-8 -*-
"""上传图片处理编排：存 upload → QWEN 判模式 → 图形/文本模式 → 匹配。

以生成器形式按阶段产出事件（dict），由 app.py 包装成 SSE 推送给前端：
    {"type": "progress"|"mode"|"result"|"error", ...}

注意：不会删除「片段」目录下任何已有解析结果；每次只新增/覆盖本次上传 XXX 的产物。
"""

import dataclasses
import io
import os
import re
import time
from typing import Iterator, Tuple

from PIL import Image

import basic_parser
import config
import matcher
import qwen_client

TOP_N = 3


def _sanitize_basename(orig_name: str) -> str:
    base = os.path.splitext(os.path.basename(orig_name))[0]
    base = re.sub(r"[^0-9A-Za-z_\-]+", "_", base).strip("_")
    return base or "upload"


# QWEN 文本解析常把 ±、℃、≥ 等符号输出成 LaTeX（如 $60\pm3\text{shA}$），
# 前端 markdown 未启用数学插件会原样显示，且与知识库的普通 unicode 写法不一致、
# 影响文本匹配。写出片段 md 前先把这些 LaTeX 片段归一化为普通 unicode。
_LATEX_TEXT_RE = re.compile(r"\\text\s*\{([^{}]*)\}")     # \text{shA} -> shA
_LATEX_DEG_RE = re.compile(r"\^\s*\{?\s*\\circ\s*\}?")    # ^{\circ} -> °
_LATEX_SUP_RE = re.compile(r"\^\s*\{?\s*(\d)\s*\}?")      # ^2 / ^{2} -> ²
_SUP_DIGITS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
}
# 命令 -> unicode；按键长降序替换，避免 \le 抢先匹配 \leq、\left 等
_LATEX_CMDS = {
    r"\left": "", r"\right": "",
    r"\leqslant": "≤", r"\geqslant": "≥",
    r"\approx": "≈", r"\bigcirc": "○", r"\rightarrow": "→", r"\leftarrow": "←",
    r"\times": "×", r"\cdots": "…", r"\ldots": "…", r"\infty": "∞",
    r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\sim": "~",
    r"\cdot": "·", r"\circ": "°", r"\div": "÷", r"\phi": "φ",
    r"\pm": "±", r"\mp": "∓", r"\le": "≤", r"\ge": "≥", r"\%": "%",
}


def normalize_latex_math(text: str) -> str:
    """把文本中的 LaTeX 数学片段转成普通 unicode 符号。"""
    text = _LATEX_TEXT_RE.sub(r"\1", text)
    text = _LATEX_DEG_RE.sub("°", text)
    text = _LATEX_SUP_RE.sub(lambda m: _SUP_DIGITS[m.group(1)], text)
    for cmd in sorted(_LATEX_CMDS, key=len, reverse=True):
        text = text.replace(cmd, _LATEX_CMDS[cmd])
    # 去掉行内/行间公式定界符与残留的 LaTeX 间距控制
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\[,;:!> ]", "", text)
    return text


def save_upload(file_bytes: bytes, orig_name: str) -> Tuple[str, str]:
    """把上传图片转存为 upload/XXX.png（统一 PNG），返回 (XXX, png_path)。"""
    xxx = _sanitize_basename(orig_name)
    upload_dir = os.path.join(config.PROJECT_ROOT, "upload")
    os.makedirs(upload_dir, exist_ok=True)
    png_path = os.path.join(upload_dir, f"{xxx}.png")
    img = Image.open(io.BytesIO(file_bytes))
    img.save(png_path, "PNG")
    return xxx, png_path


def _existing_md(xxx: str):
    """片段目录下是否已有以 XXX 为前缀的 md 文件；有则返回其路径。"""
    if not os.path.isdir(config.FRAGMENTS_DIR):
        return None
    # 优先精确匹配 XXX_extracted.md
    exact = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_extracted.md")
    if os.path.isfile(exact):
        return exact
    for name in os.listdir(config.FRAGMENTS_DIR):
        full = os.path.join(config.FRAGMENTS_DIR, name)
        if os.path.isfile(full) and name.startswith(xxx) and name.lower().endswith(".md"):
            return full
    return None


def _existing_subdir(xxx: str):
    """片段目录下是否已有以 XXX 为前缀的子目录；有则返回其路径。"""
    if not os.path.isdir(config.FRAGMENTS_DIR):
        return None
    for name in os.listdir(config.FRAGMENTS_DIR):
        full = os.path.join(config.FRAGMENTS_DIR, name)
        if os.path.isdir(full) and name.startswith(xxx):
            return full
    return None


def _xxx_images_dir(xxx: str):
    """定位片段中 XXX 子目录下的 images 目录。"""
    subdir = _existing_subdir(xxx)
    if not subdir:
        return None
    for root, dirs, _files in os.walk(subdir):
        if os.path.basename(root) == "images":
            return root
    return None


def _ranked(scores) -> list:
    ranked = []
    for i, s in enumerate(scores[:TOP_N], 1):
        d = dataclasses.asdict(s)
        d["rank"] = i
        ranked.append(d)
    return ranked


def _pump_parse(gen):
    """驱动「逐行 yield 解析进度文案」的生成器（basic_parser 解析通道）：
    把每行转成 progress 事件；生成器结束时通过 StopIteration.value 取回其
    计时字典（{"paddle_crop": 秒, "vlm_interpret": 秒}）。
    """
    timings = {}
    while True:
        try:
            line = next(gen)
        except StopIteration as e:
            timings = e.value or {}
            break
        yield {"type": "progress", "msg": line}
    return timings


def _pump_scores(gen):
    """驱动 matcher 的流式生成器：把其产出的 dict 翻译成前端事件。

    - {"timing": {...}} → 不下发给前端，累积进本函数汇总返回的计时字典；
    - {"start": n, "total": t} → match_start（前端据此创建 n 个线程窗口）；
    - {"worker": k, "msg": ...} → 带 worker 的 progress（路由到第 k 个窗口）；
    - {"msg": ...}（无 worker，如向量化阶段逐项进度）→ 不带 worker 的 progress，
      前端走通用日志区展示（复用 App.jsx 里本来就支持的「无 worker」分支）。
    生成器结束时通过 StopIteration.value 取回 (排序后的 scores 列表, 计时字典)。"""
    scores = []
    timings = {}
    while True:
        try:
            item = next(gen)
        except StopIteration as e:
            scores = e.value or []
            break
        if "timing" in item:
            timings.update(item["timing"])
        elif "start" in item:
            yield {"type": "match_start", "workers": item["start"], "total": item["total"]}
        elif "worker" in item:
            yield {"type": "progress", "worker": item["worker"], "msg": item["msg"]}
        else:
            yield {"type": "progress", "msg": item["msg"]}
    return scores, timings


def _upload_image_name(xxx: str):
    """本次 XXX 对应的原始上传图片文件名（相对 upload/ 目录）；
    不存在则返回 None（复用旧片段但从未真正走过上传接口的边缘情况）。"""
    path = os.path.join(config.UPLOAD_DIR, f"{xxx}.png")
    return f"{xxx}.png" if os.path.isfile(path) else None


def _run_graphic_match(
    iw: float, tw: float, xxx: str, extra_timings: dict = None
) -> Iterator[dict]:
    """图形模式：流式产出逐图纸比对/打分进度，最后产出 result 事件（含各阶段耗时）。"""
    gen = matcher.iter_match(
        config.FRAGMENTS_DIR,
        image_weight=iw,
        text_weight=tw,
        # 精确锁定本次上传 XXX 的片段产物，避免片段目录含多个零件时取错
        images_dir=_xxx_images_dir(xxx),
        md_path=_existing_md(xxx),
    )
    scores, timings = yield from _pump_scores(gen)
    stage_timings = {**(extra_timings or {}), **timings}
    yield {"type": "progress", "msg": "所有文件匹配完成"}
    yield {
        "type": "result",
        "mode": "graphic",
        "weights": {"image": iw, "text": tw},
        "similarity_func": "cosine_similarity",
        "ranking": _ranked(scores),
        "top": _ranked(scores)[0] if scores else None,
        "upload_image": _upload_image_name(xxx),
        "stage_timings": stage_timings,
    }


def _run_text_match(md_text: str, xxx: str, extra_timings: dict = None) -> Iterator[dict]:
    """文本模式：流式产出逐图纸比对进度，最后产出 result 事件（含各阶段耗时）。"""
    scores, timings = yield from _pump_scores(matcher.iter_match_text(md_text))
    stage_timings = {**(extra_timings or {}), **timings}
    yield {"type": "progress", "msg": "所有文件匹配完成"}
    yield {
        "type": "result",
        "mode": "text",
        "similarity_func": "cosine_similarity",
        "source_text": md_text,
        "ranking": _ranked(scores),
        "top": _ranked(scores)[0] if scores else None,
        "upload_image": _upload_image_name(xxx),
        "stage_timings": stage_timings,
    }


def process(
    file_bytes: bytes, orig_name: str, iw: float, tw: float,
) -> Iterator[dict]:
    """完整处理流程，逐阶段产出事件。

    先看产物、再定模式（命中复用时连 QWEN 一起跳过，更快更稳）：
    - 片段目录已有 XXX「子目录 + md」 → 图形模式复用（跳过 QWEN 与解析）；
    - 片段目录已有 XXX「md」（无子目录）→ 文本模式复用（跳过 QWEN 与解析）；
    - 都没有 → 调 QWEN 判模式后只生成本次 XXX 的产物：
        含图形 → 走 PaddleOCR+QWEN 解析；
        纯文本 → 写 md。
    全程不删除片段目录下任何已有解析结果（匹配按 XXX 精确锁定产物）。
    """
    try:
        xxx = _sanitize_basename(orig_name)
        existing_md = _existing_md(xxx)
        existing_dir = _existing_subdir(xxx)

        # ---- 复用：图形产物（子目录 + md）----
        if existing_md and existing_dir:
            yield {"type": "mode", "mode": "graphic"}
            yield {"type": "progress", "msg": f"检测到已有图形片段（{os.path.basename(existing_dir)} 与 {os.path.basename(existing_md)}）。"}
            yield {"type": "progress", "msg": "开始图片+文本双维相似度匹配…"}
            yield from _run_graphic_match(iw, tw, xxx)
            return

        # ---- 复用：文本产物（仅 md）----
        if existing_md:
            yield {"type": "mode", "mode": "text"}
            yield {"type": "progress", "msg": f"检测到已有 md（{os.path.basename(existing_md)}）。"}
            with open(existing_md, "r", encoding="utf-8") as f:
                md_text = normalize_latex_math(f.read())
            yield {"type": "progress", "msg": "在 view_text_vec 中检索最相似图纸…"}
            yield from _run_text_match(md_text, xxx)
            return

        # ---- 无可复用产物：保存上传、QWEN 判模式、重新生成 ----
        yield {"type": "progress", "msg": "无可复用片段，保存上传文件并转存为 PNG…"}
        _, png_path = save_upload(file_bytes, orig_name)
        yield {"type": "progress", "msg": f"已保存：upload/{xxx}.png"}

        yield {"type": "progress", "msg": "解读图片内容…"}
        t_decide0 = time.perf_counter()
        is_graphic, raw = qwen_client.interpret_and_decide(png_path)
        # 判模式这一步本身也是 QWEN 对图片内容的解读，计入「VLM 解读内容」阶段
        vlm_secs = time.perf_counter() - t_decide0

        if is_graphic:
            yield {"type": "mode", "mode": "graphic"}
            yield {"type": "progress", "msg": "判定：含图形 → 图形模式，进行解析…"}
            parse_stream = basic_parser.run_basic_parse_stream(xxx)
            parse_timings = yield from _pump_parse(parse_stream)
            extra_timings = {
                "vlm_interpret": vlm_secs + parse_timings.get("vlm_interpret", 0.0),
            }
            if "paddle_crop" in parse_timings:
                extra_timings["paddle_crop"] = parse_timings["paddle_crop"]
            yield {"type": "progress", "msg": "开始图片+文本双维相似度匹配…"}
            yield from _run_graphic_match(iw, tw, xxx, extra_timings=extra_timings)
        else:
            yield {"type": "mode", "mode": "text"}
            yield {"type": "progress", "msg": "判定：纯文本 → 文本模式。写出片段 md…"}
            os.makedirs(config.FRAGMENTS_DIR, exist_ok=True)
            md_path = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_extracted.md")
            md_text = normalize_latex_math(raw)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
            yield {"type": "progress", "msg": "在 view_text_vec 中检索最相似图纸…"}
            yield from _run_text_match(md_text, xxx, extra_timings={"vlm_interpret": vlm_secs})
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "msg": str(e)}
