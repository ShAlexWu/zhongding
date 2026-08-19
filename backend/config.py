# -*- coding: utf-8 -*-
"""后端应用配置：MO 连接、模型、向量维度、AI 匹配权重、片段目录等。

敏感信息（API Key / 数据库连接信息）一律从环境变量读取，实际取值维护在
项目根目录的 .env 文件中（不提交到 Git，由 start.sh 加载 / 交互录入）。
"""

import os

# 项目根目录（backend 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 片段（新需求局部零件）目录
FRAGMENTS_DIR = os.path.join(PROJECT_ROOT, "片段")
# 图纸知识库目录（用于前端展示命中视图图片）
# 注意：现有 MO 向量库是用 图纸_old/ 下的解析产物建的（图纸/ 目前只是其中 21 张
# 的部分重复、且全部在 图纸_old/ 也有，没有任何独有内容），指向 图纸/ 会导致
# 绝大多数匹配结果（约 220/241 张）在 /static/diagrams 下 404、原始图片显示不出来。
DIAGRAMS_DIR = os.path.join(PROJECT_ROOT, "图纸_old")
# 原始图纸 PDF 目录（用于「排名列表悬浮预览：上传图片 vs 原始 PDF」）
PDFS_DIR = os.path.join(PROJECT_ROOT, "212份图纸")
# 用户上传原图目录
UPLOAD_DIR = os.path.join(PROJECT_ROOT, "upload")

# ---------------------------------------------------------------------------
# DashScope（百炼）模型配置
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

IMAGE_EMBEDDING_MODEL = "qwen3-vl-embedding"
TEXT_EMBEDDING_MODEL = "text-embedding-v4"

# ---------------------------------------------------------------------------
# 「基础解读」通道：PaddleOCR-VL 云 API（裁切视图）+ QWEN（逐视图解读）
# ---------------------------------------------------------------------------
PADDLE_OCR_URL = os.getenv(
    "PADDLE_OCR_URL", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
)
PADDLE_OCR_TOKEN = os.getenv("PADDLEOCR_KEY", "")
PADDLE_OCR_MODEL = os.getenv("PADDLE_OCR_MODEL", "PaddleOCR-VL-1.6")

# 统一向量维度，与知识库建表 vecf64(1024) 保持一致
VECTOR_DIM = 1024

# ---------------------------------------------------------------------------
# MatrixOne（MO）数据库连接配置
# ---------------------------------------------------------------------------
MO_CONFIG = {
    "host": os.getenv("MO_HOST", ""),
    "port": int(os.getenv("MO_PORT", "6001")),
    "user": os.getenv("MO_USER", ""),
    "password": os.getenv("MO_PASSWORD", ""),
    "db": os.getenv("MO_DB", ""),
    "charset": os.getenv("MO_CHARSET", "utf8mb4"),
}

PIC_VEC_TABLE = "view_pic_vec"
TEXT_VEC_TABLE = "view_text_vec"

# 支持向量化的图片扩展名
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp")

# 调用 DashScope 时每次请求之间的间隔（秒），用于规避限流
REQUEST_INTERVAL_SECONDS = float(os.getenv("EMBED_REQUEST_INTERVAL", "0.2"))

# ---------------------------------------------------------------------------
# AI 匹配权重（页面可配置；此处为默认值）
# ---------------------------------------------------------------------------
# 图片向量相似度权重 50%、文本语义相似度 50%
IMAGE_WEIGHT = float(os.getenv("MATCH_IMAGE_WEIGHT", "0.5"))
TEXT_WEIGHT = float(os.getenv("MATCH_TEXT_WEIGHT", "0.5"))

# AI 匹配并行比对的线程数（每线程一条独立 MO 连接，= 同时占用的 DB 连接数）
MATCH_WORKERS = int(os.getenv("MATCH_WORKERS", "8"))
