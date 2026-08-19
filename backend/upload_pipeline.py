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
import shutil
import subprocess
import time
from typing import Iterator, Tuple

from PIL import Image

import basic_parser
import config
import matcher
import qwen_client

TOP_N = 3

# 用户在 Prompt.md 第 24 行给出的 codex 解析 prompt（逐字使用，仅替换 XXX）
CODEX_PROMPT_TEMPLATE = (
    "对文件 upload\\{xxx}.png 进行内容识别，"
    "1、基于零件的每一个加工视图做裁切（如果只有一个视图，则不需要截取，取整个视图即可），"
    "并对每个视图中信息(尺寸、弧度)进行提取和对含义进行说明和解读，"
    "要求把裁切的图片也嵌入到 Markdown 文件中（采用 UTF-8 编码），并放在对应的位置，"
    "即裁切图片要和其被提取的内容在同一个文档区域；"
    "请先将 PDF 渲染为整页 PNG，再按每个加工视图、剖面视图、局部视图、表格、NOTES、标题栏分别裁切。"
    "裁切后必须检查每张图片是否包含完整尺寸线和文字；如果有边缘文字被裁掉，必须重裁，"
    "如果带入了其他视图中的片段（如：尺寸），要严格区分归属，"
    "只能提取当前视图中的内容到对应的文档区域，禁止混杂；"
    "如果裁切后的内容没有图形，只有文字，直接丢弃；"
    "2、表格、文本都要精准还原和提取；3、需要严格还原原图的结构，不做主观整理；"
    "3、最终生成 '片段\\{xxx}_extracted.md' 和 '片段\\{xxx}_assets\\images\\';"
    "4、禁止把整页 PNG 放在 Markdown 文件中；"
    "5、禁止把整页 PNG 放在 images 子目录中；"
    "6、不要探测或使用 ImageMagick/magick；"
    "7、不要探测或使用 tesseract/OCR 命令行工具；"
    "8、图像尺寸读取、裁切、保存固定使用 Python + Pillow(PIL)，不要调用 magick identify/convert"
)


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


def _codex_executable() -> str:
    return shutil.which("codex") or "codex"


# AI 思考过程里会带出底层工具的特征信息，推送到前端前先过滤：
# - "model:" 之后的内容统一改为 Auto
# - "provider:" 之后的内容统一改为 Auto
# - 文案中所有 Codex（不区分大小写）一律去除
# - 文案中所有 chatgpt（不区分大小写）替换为 innerrouter
# - 含 OpenAI 或 SandBox（不区分大小写）的行，整行屏蔽不输出
_MODEL_LINE_RE = re.compile(r"(model\s*:\s*).*", re.IGNORECASE)
_PROVIDER_LINE_RE = re.compile(r"(provider\s*:\s*).*", re.IGNORECASE)
_CODEX_WORD_RE = re.compile(r"codex", re.IGNORECASE)
_CHATGPT_WORD_RE = re.compile(r"chatgpt", re.IGNORECASE)
_BLOCKED_LINE_RE = re.compile(r"openai|sandbox", re.IGNORECASE)


def _sanitize_codex_line(line: str) -> str:
    """过滤掉 codex 的特征属性后，返回可安全展示的文案。"""
    line = _MODEL_LINE_RE.sub(r"\1Auto", line)
    line = _PROVIDER_LINE_RE.sub(r"\1Auto", line)
    line = _CODEX_WORD_RE.sub("", line)
    line = _CHATGPT_WORD_RE.sub("innerrouter", line)
    return line


def run_codex_stream(xxx: str) -> Iterator[str]:
    """以项目根为 cwd 运行 codex 解析命令，逐行产出 stdout。

    生成器结束时 return 一个计时字典 {"parse": 秒}（Codex 是「深度解读」通道，
    切图与解读一体完成，不像 PaddleOCR+QWEN 那样能拆成两段，故只汇总一个耗时，
    上游 process() 会把它计入「VLM 解读内容」阶段）。
    """
    prompt = CODEX_PROMPT_TEMPLATE.format(xxx=xxx)
    cmd = [
        _codex_executable(),
        "--dangerously-bypass-approvals-and-sandbox",
        "exec",
        "--skip-git-repo-check",
        prompt,
    ]
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=config.PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            # 含 OpenAI / SandBox 的行整行屏蔽，不推送到前端
            if _BLOCKED_LINE_RE.search(line):
                continue
            line = _sanitize_codex_line(line)
            if line.strip():
                yield line
    finally:
        proc.stdout.close()
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"codex 退出码 {code}")
    return {"parse": time.perf_counter() - t0}


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
    """驱动「逐行 yield 解析进度文案」的生成器（basic_parser / codex 解析通道）：
    把每行转成 progress 事件；生成器结束时通过 StopIteration.value 取回其
    计时字典（如 {"paddle_crop": 秒, "vlm_interpret": 秒} 或 {"parse": 秒}）。
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
    - {"worker": k, "msg": ...} → 带 worker 的 progress（路由到第 k 个窗口）。
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
        else:
            yield {"type": "progress", "worker": item["worker"], "msg": item["msg"]}
    return scores, timings


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
        "stage_timings": stage_timings,
    }


def _run_text_match(md_text: str, extra_timings: dict = None) -> Iterator[dict]:
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
        "stage_timings": stage_timings,
    }


def process(
    file_bytes: bytes, orig_name: str, iw: float, tw: float,
    deep_parse: bool = False,
) -> Iterator[dict]:
    """完整处理流程，逐阶段产出事件。

    先看产物、再定模式（命中复用时连 QWEN 一起跳过，更快更稳）：
    - 片段目录已有 XXX「子目录 + md」 → 图形模式复用（跳过 QWEN 与解析）；
    - 片段目录已有 XXX「md」（无子目录）→ 文本模式复用（跳过 QWEN 与解析）；
    - 都没有 → 调 QWEN 判模式后只生成本次 XXX 的产物：
        含图形 → 解析（默认走 PaddleOCR+QWEN；deep_parse=True「深度解读」走 Codex）；
        纯文本 → 写 md（不受 deep_parse 影响）。
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
            yield from _run_text_match(md_text)
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
            if deep_parse:
                yield {"type": "progress", "msg": "判定：含图形 → 图形模式（深度解读）。运行 【图文双模式】 解析（可能耗时数分钟）…"}
                parse_stream = run_codex_stream(xxx)
            else:
                yield {"type": "progress", "msg": "判定：含图形 → 图形模式，进行解析…"}
                parse_stream = basic_parser.run_basic_parse_stream(xxx)
            parse_timings = yield from _pump_parse(parse_stream)
            extra_timings = {
                "vlm_interpret": vlm_secs
                + parse_timings.get("vlm_interpret", parse_timings.get("parse", 0.0)),
            }
            if "paddle_crop" in parse_timings:
                # Codex（深度解读）切图与解读一体完成，没有独立的 paddle_crop 阶段
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
            yield from _run_text_match(md_text, extra_timings={"vlm_interpret": vlm_secs})
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "msg": str(e)}
