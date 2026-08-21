# -*- coding: utf-8 -*-
"""解析通道：PaddleOCR-VL（云 API 裁切视图）+ QWEN（逐视图解读）。

generator，逐行 yield 进度文案；产物结构：
    片段/XXX_extracted.md           （# 文件标题 + 每个 "## 视图 N" 分段）
    片段/XXX_assets/images/<裁切图>

md 必须满足 chunk_by_image_anchor 的要求：图片链接独占一行、二级标题作分段锚点。
"""

import json
import os
import re
import shutil
import time
from typing import Iterator, List, Tuple

import requests

import config
import qwen_client

# 行首 ATX 标题（# 标题）
_HEADING_LINE_RE = re.compile(r"^(#{1,6})(\s)")

# PaddleOCR-VL 作业可选参数：关闭方向分类/去扭曲/图表识别（与参考实现一致）
_OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
# 轮询间隔（秒）
_POLL_INTERVAL = 5


def _headers() -> dict:
    return {"Authorization": f"bearer {config.PADDLE_OCR_TOKEN}"}


def _submit_job(png_path: str) -> str:
    """提交本地图片到 PaddleOCR-VL，返回 jobId。"""
    data = {
        "model": config.PADDLE_OCR_MODEL,
        "optionalPayload": json.dumps(_OPTIONAL_PAYLOAD),
    }
    with open(png_path, "rb") as f:
        files = {"file": f}
        resp = requests.post(
            config.PADDLE_OCR_URL, headers=_headers(), data=data, files=files,
            timeout=120,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"PaddleOCR 提交失败（{resp.status_code}）：{resp.text[:300]}")
    return resp.json()["data"]["jobId"]


def _poll_job(job_id: str) -> Iterator:
    """轮询作业状态，yield 进度文案（str）；完成时 yield ('__done__', jsonl_url)。"""
    url = f"{config.PADDLE_OCR_URL}/{job_id}"
    while True:
        resp = requests.get(url, headers=_headers(), timeout=60)
        resp.raise_for_status()
        d = resp.json()["data"]
        state = d.get("state")
        if state == "done":
            yield ("__done__", d["resultUrl"]["jsonUrl"])
            return
        if state == "failed":
            raise RuntimeError(f"PaddleOCR 解析失败：{d.get('errorMsg')}")
        prog = d.get("extractProgress") or {}
        if prog.get("totalPages"):
            yield f"处理中…（{prog.get('extractedPages', 0)}/{prog['totalPages']} 页）"
        else:
            yield "OCR 处理中…"
        time.sleep(_POLL_INTERVAL)


def _download_crops(jsonl_url: str, dst_dir: str) -> List[Tuple[str, str]]:
    """下载作业结果里各视图裁切图到 dst_dir，返回 [(文件名, 本地路径)]（按名排序）。"""
    os.makedirs(dst_dir, exist_ok=True)
    resp = requests.get(jsonl_url, timeout=120)
    resp.raise_for_status()
    crops: List[Tuple[str, str]] = []
    seen = set()
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        result = json.loads(line)["result"]
        for res in result.get("layoutParsingResults", []):
            images = (res.get("markdown") or {}).get("images") or {}
            for img_path, img_url in images.items():
                base = os.path.basename(img_path)
                if base in seen:
                    continue
                seen.add(base)
                local = os.path.join(dst_dir, base)
                with open(local, "wb") as fp:
                    fp.write(requests.get(img_url, timeout=120).content)
                crops.append((base, local))
    crops.sort(key=lambda x: x[0])
    return crops


def _demote_headings(text: str, by: int = 2) -> str:
    """把 QWEN 解读内容里「会与视图锚点冲突」的标题降级。

    视图锚点用二级标题（## 视图 N）。只有当解读内容里的标题是一级(#)或二级(##)时，
    才会与锚点同级或更浅、把视图分段截断成空，需要降级（# → ###，## → ####）；
    三级(###)及更深本就深于锚点，原样保留。提示词已要求 QWEN 输出三级标题，正常
    不会触发降级，此处仅作兜底。
    """
    out = []
    for line in text.splitlines():
        m = _HEADING_LINE_RE.match(line)
        if m:
            cur = len(m.group(1))
            if cur <= 2:  # 仅一级/二级标题需要降级，三级及更深保留
                new_level = min(6, cur + by)
                line = "#" * new_level + line[cur:]
        out.append(line)
    return "\n".join(out)


def _reset_fragment(xxx: str) -> str:
    """清掉本次 XXX 的旧产物（md + assets），返回 images 目标目录路径。"""
    md_path = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_extracted.md")
    if os.path.isfile(md_path):
        os.remove(md_path)
    assets_dir = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_assets")
    if os.path.isdir(assets_dir):
        shutil.rmtree(assets_dir, ignore_errors=True)
    images_dir = os.path.join(assets_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    return images_dir


def run_basic_parse_stream(xxx: str) -> Iterator[str]:
    """以 PaddleOCR-VL + QWEN 解析 upload/XXX.png，产出片段产物。

    生成器结束时 return 一个计时字典 {"paddle_crop": 秒, "vlm_interpret": 秒}，
    供 upload_pipeline 汇总「各阶段耗时」展示给前端（调用方用 StopIteration.value 取回）。
    """
    if not config.PADDLE_OCR_TOKEN:
        raise RuntimeError("未配置 PADDLEOCR_KEY 环境变量，无法调用 PaddleOCR-VL。")

    png_path = os.path.join(config.PROJECT_ROOT, "upload", f"{xxx}.png")
    if not os.path.isfile(png_path):
        raise RuntimeError(f"未找到上传文件：upload/{xxx}.png")

    t_crop0 = time.perf_counter()
    yield "提交解析…"
    job_id = _submit_job(png_path)
    yield f"已提交解析作业（job {job_id[:8]}…），等待结果…"

    jsonl_url = None
    for item in _poll_job(job_id):
        if isinstance(item, tuple) and item[0] == "__done__":
            jsonl_url = item[1]
        else:
            yield item
    yield "解析完成，下载裁切视图…"

    tmp_dir = os.path.join(config.PROJECT_ROOT, "upload", f"{xxx}_paddle")
    try:
        crops = _download_crops(jsonl_url, os.path.join(tmp_dir, "imgs"))
        if not crops:
            raise RuntimeError("未提取到任何视图图片，无法解析该图。")
        paddle_crop_secs = time.perf_counter() - t_crop0
        yield f"共获得 {len(crops)} 个视图，开始逐视图解读…"

        images_dir = _reset_fragment(xxx)
        md_parts = [f"# {xxx} 内容识别\n"]
        t_vlm0 = time.perf_counter()
        for i, (base, local) in enumerate(crops, 1):
            shutil.copy2(local, os.path.join(images_dir, base))
            yield f"解读视图 {i}/{len(crops)}…"
            # 降级解读内容里的标题，避免与「## 视图 N」锚点同级把分段截断
            content = _demote_headings(qwen_client.interpret_view(local))
            name = f"视图 {i}"
            md_parts.append(
                f"## {name}\n![{name}]({xxx}_assets/images/{base})\n{content}\n"
            )
        vlm_secs = time.perf_counter() - t_vlm0

        md_path = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_extracted.md")
        with open(md_path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(md_parts))
        yield f"生成 {xxx}_extracted.md，共 {len(crops)} 个视图"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"paddle_crop": paddle_crop_secs, "vlm_interpret": vlm_secs}
