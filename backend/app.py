# -*- coding: utf-8 -*-
"""FastAPI 后端：暴露 AI 匹配接口。

当前阶段不支持上传，匹配输入固定为项目下的「片段」目录，
跑通前后端联调；后续再支持上传图片。

启动：
    uv run uvicorn app:app --reload --port 50011   （在 backend 目录下）
"""

import dataclasses
import json
import os
from typing import List, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import doc_generator
import field_extractor
import matcher
import mo_db
import upload_pipeline

app = FastAPI(title="图纸相似度匹配 API")

# 允许前端开发服务器跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态托管片段与图纸图片，供前端展示缩略图
app.mount(
    "/static/fragments",
    StaticFiles(directory=config.FRAGMENTS_DIR),
    name="fragments",
)
app.mount(
    "/static/diagrams",
    StaticFiles(directory=config.DIAGRAMS_DIR),
    name="diagrams",
)
# 原始 PDF 图纸 + 用户上传原图：供排名列表悬浮预览「上传图片 vs 原始 PDF」使用。
# check_dir=False：这两个目录属于后加的功能，部署环境里可能还没建好/挂载卷，
# 缺了不应该导致整个后端启动失败，缺目录时对应请求走 404 即可。
app.mount(
    "/static/pdfs",
    StaticFiles(directory=config.PDFS_DIR, check_dir=False),
    name="pdfs",
)
app.mount(
    "/static/upload",
    StaticFiles(directory=config.UPLOAD_DIR, check_dir=False),
    name="upload",
)


class MatchRequest(BaseModel):
    # 预留：页面可配置权重；不传则用默认 0.7 / 0.3
    image_weight: Optional[float] = None
    text_weight: Optional[float] = None


def _score_to_dict(rank: int, s: matcher.DiagramScore) -> dict:
    d = dataclasses.asdict(s)
    d["rank"] = rank
    return d


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/health")
def health_root():
    """容器 / 反向代理健康检查用（Docker 部署约定路径）；内容与 /api/health 一致。"""
    return {"status": "ok"}


@app.get("/api/diagrams")
def list_diagrams():
    """返回知识库内全部图纸名（用于「文档生成」页的图纸选择器）。"""
    conn = mo_db.connect()
    try:
        diagrams = matcher.list_diagrams(conn)
    finally:
        conn.close()
    return {"diagrams": diagrams}


@app.get("/api/pdf_diagrams")
def list_pdf_diagrams():
    """返回「212份图纸」目录下实际存在 PDF 的图纸名集合。

    知识库里的图纸不是每一张都配了原始 PDF（212/241），前端排名列表悬浮预览
    「上传图片 vs 原始 PDF」时，靠这份名单判断某一行要不要显示 PDF 预览、
    还是显示「暂无 PDF」，不用逐行发请求去试。
    """
    names = []
    if os.path.isdir(config.PDFS_DIR):
        names = sorted(n for n in os.listdir(config.PDFS_DIR) if n.lower().endswith(".pdf"))
    return {"diagrams": names}


@app.post("/api/extract_field")
def extract_field(diagram: str = Form(...), query: str = Form(...)):
    """针对指定图纸，按自然语言问题做 RAG 抽取，返回结果文本。"""
    value = field_extractor.extract_field(diagram, query)
    return {"diagram": diagram, "query": query, "value": value}


@app.get("/api/doc_prompt")
def doc_prompt():
    """返回「生产检验指导书」抽取的默认提示词模板（前端预填、可编辑）。"""
    return {"prompt": field_extractor.DEFAULT_DOC_PROMPT}


@app.post("/api/extract_doc_fields")
def extract_doc_fields(diagram: str = Form(...), prompt: str = Form("")):
    """针对指定图纸，用（可编辑的）提示词抽取并组装对照模版的统一大 JSON。"""
    data = field_extractor.extract_doc_struct(diagram, prompt or None)
    return {"diagram": diagram, "data": data}


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")


@app.get("/api/diagram_images")
def diagram_images(diagram: str):
    """列出某图纸 images 子目录下全部图片（相对 图纸/ 的 '/' 路径，供前端拼 /static/diagrams）。"""
    folder = diagram[:-4] if diagram.lower().endswith(".pdf") else diagram
    rel_dir = f"{folder}/{folder}_assets/images"
    abs_dir = os.path.join(config.DIAGRAMS_DIR, folder, f"{folder}_assets", "images")
    images = []
    if os.path.isdir(abs_dir):
        for name in sorted(os.listdir(abs_dir)):
            if name.lower().endswith(_IMAGE_EXTS):
                images.append(f"{rel_dir}/{name}")
    return {"diagram": diagram, "images": images}


@app.post("/api/generate_doc")
def generate_doc(
    diagram: str = Form(...),
    data: str = Form(...),
    images: str = Form(""),
):
    """把（用户确认后的）统一 JSON + 选中的图片填入模版，生成 .docx 并作为下载返回。"""
    try:
        doc_data = json.loads(data)
    except Exception:
        doc_data = {}
    try:
        image_relpaths = json.loads(images) if images else []
    except Exception:
        image_relpaths = []
    path = doc_generator.generate(diagram, doc_data, image_relpaths)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=os.path.basename(path),
    )


@app.post("/api/match")
def run_match(req: MatchRequest = MatchRequest()):
    """对「片段」目录执行 AI 匹配，返回排名与思考过程数据。"""
    iw = req.image_weight if req.image_weight is not None else config.IMAGE_WEIGHT
    tw = req.text_weight if req.text_weight is not None else config.TEXT_WEIGHT

    scores: List[matcher.DiagramScore] = matcher.match(
        config.FRAGMENTS_DIR, image_weight=iw, text_weight=tw
    )

    ranked = [_score_to_dict(i, s) for i, s in enumerate(scores, 1)]
    return {
        "fragment_dir": config.FRAGMENTS_DIR,
        "weights": {"image": iw, "text": tw},
        "similarity_func": "cosine_similarity",
        "ranking": ranked,
        "top": ranked[0] if ranked else None,
    }


def _sse(event: dict) -> str:
    """把事件 dict 编码成一条 SSE 消息。"""
    etype = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {etype}\ndata: {payload}\n\n"


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    image_weight: float = Form(config.IMAGE_WEIGHT),
    text_weight: float = Form(config.TEXT_WEIGHT),
    deep_parse: bool = Form(False),
):
    """上传图片 → 清空片段 → QWEN 判模式 → 图形/文本模式 → 匹配。

    含图形的解析：默认走 PaddleOCR-VL + QWEN；deep_parse=True「深度解读」走 Codex。
    以 SSE 流式返回处理进度与最终结果。
    """
    file_bytes = await file.read()
    orig_name = file.filename or "upload.png"

    def event_stream():
        for event in upload_pipeline.process(
            file_bytes, orig_name, image_weight, text_weight, deep_parse
        ):
            yield _sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
