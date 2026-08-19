# -*- coding: utf-8 -*-
"""集中配置：MO 数据库连接、模型名称、向量维度、图纸目录等。

敏感信息（API Key / 数据库连接信息）一律从环境变量读取，实际取值维护在
项目根目录的 .env 文件中（不提交到 Git，由 start.sh 加载 / 交互录入）。
"""

import os

# ---------------------------------------------------------------------------
# 图纸数据目录
# ---------------------------------------------------------------------------
# 项目根目录（knowledge_construct 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 每张图纸解析结果所在的根目录
# 注意：与 backend/config.py 保持一致——现有 MO 向量库是用 图纸_old/ 建的，
# 增量重建/续跑知识库时也要从这里读取解析产物，否则找不到 images 目录。
DIAGRAMS_DIR = os.path.join(PROJECT_ROOT, "图纸_old")

# ---------------------------------------------------------------------------
# DashScope（百炼）模型配置
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

# 图片向量化模型
IMAGE_EMBEDDING_MODEL = "qwen3-vl-embedding"
# 文本向量化模型
TEXT_EMBEDDING_MODEL = "text-embedding-v4"

# 统一向量维度。
# text-embedding-v4 与 qwen3-vl-embedding 均支持 1024 维，
# 与 MO 指引示例中的 vecf64(1024) 保持一致，便于两表结构统一。
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

# 表名
PIC_VEC_TABLE = "view_pic_vec"
TEXT_VEC_TABLE = "view_text_vec"

# 支持向量化的图片扩展名
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp")

# 调用 DashScope 时每次请求之间的间隔（秒），用于规避限流
REQUEST_INTERVAL_SECONDS = float(os.getenv("EMBED_REQUEST_INTERVAL", "0.2"))
