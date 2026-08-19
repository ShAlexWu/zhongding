# -*- coding: utf-8 -*-
"""DashScope（百炼）向量化客户端封装。

- embed_image: 使用 qwen3-vl-embedding 对本地图片进行向量化
- embed_text : 使用 text-embedding-v4 对文本进行向量化
"""

import base64
import os
from http import HTTPStatus
from typing import List

import dashscope

import config


def _ensure_api_key() -> None:
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError(
            "未配置 DASHSCOPE_API_KEY，请设置环境变量后再运行。"
        )
    dashscope.api_key = config.DASHSCOPE_API_KEY


def _image_to_data_uri(image_path: str) -> str:
    """把本地图片读成 base64 data URI，供多模态向量化接口使用。"""
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{ext};base64,{b64}"


def embed_image(image_path: str) -> List[float]:
    """对单张本地图片进行向量化，返回稠密向量。

    使用 DashScope 多模态向量模型 qwen3-vl-embedding。
    """
    _ensure_api_key()

    data_uri = _image_to_data_uri(image_path)
    # 额外的关键字参数会被 DashScope SDK 放入请求的 parameters 字段，
    # 因此这里直接传 dimension=...，而不是再包一层 parameters={...}。
    resp = dashscope.MultiModalEmbedding.call(
        model=config.IMAGE_EMBEDDING_MODEL,
        input=[{"image": data_uri}],
        dimension=config.VECTOR_DIM,
    )

    if resp.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"图片向量化失败 [{image_path}]: {resp.code}, {resp.message}"
        )

    embeddings = resp.output["embeddings"]
    return embeddings[0]["embedding"]


def embed_text(text: str) -> List[float]:
    """对单段文本进行向量化，返回稠密向量。

    使用 DashScope text-embedding-v4。
    """
    _ensure_api_key()

    resp = dashscope.TextEmbedding.call(
        model=config.TEXT_EMBEDDING_MODEL,
        input=text,
        dimension=config.VECTOR_DIM,
    )

    if resp.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"文本向量化失败: {resp.code}, {resp.message}"
        )

    return resp.output["embeddings"][0]["embedding"]
