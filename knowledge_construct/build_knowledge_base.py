# -*- coding: utf-8 -*-
"""图纸知识库构建主程序。

对 图纸/ 目录下每张图纸执行：
1. 对 images 目录下各图片用 qwen3-vl-embedding 向量化 -> 写入 view_pic_vec
2. 对 md 文件按最高一级标题分段（去除图片链接）
3. 对每个分段用 text-embedding-v4 向量化 -> 写入 view_text_vec

用法：
    # 先安装依赖
    pip install -r requirements.txt
    # 配置密钥（PowerShell 示例）
    $env:DASHSCOPE_API_KEY = "sk-xxxx"
    # 全量运行
    python build_knowledge_base.py
    # 只处理指定的某张/某几张图纸（按文件夹名）
    python build_knowledge_base.py 20C114220
    # 从某张图纸开始，按字典序一直处理到最后（断点续跑）
    python build_knowledge_base.py --start 20C114361
"""

import argparse
import io
import os
import sys
import time
from typing import List, Optional

import config
import mo_db
import embedding_client
from markdown_chunker import chunk_markdown

# 确保在 Windows 终端能正确输出中文
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
except Exception:
    pass


def _find_images_dir(diagram_dir: str) -> Optional[str]:
    """在某张图纸目录下定位 images 目录（位于 *_assets/images 下）。"""
    for root, dirs, _files in os.walk(diagram_dir):
        for d in dirs:
            if d == "images":
                return os.path.join(root, d)
    return None


def _find_markdown(diagram_dir: str) -> Optional[str]:
    """定位图纸目录下的 *_extracted.md 文件。"""
    for name in os.listdir(diagram_dir):
        if name.lower().endswith(".md"):
            return os.path.join(diagram_dir, name)
    return None


def _list_images(images_dir: str) -> List[str]:
    """列出 images 目录下所有受支持的图片文件（忽略 base64 文本等）。"""
    result = []
    for name in sorted(os.listdir(images_dir)):
        if name.lower().endswith(config.IMAGE_EXTENSIONS):
            result.append(os.path.join(images_dir, name))
    return result


def process_diagram(conn, diagram_dir: str) -> None:
    """处理单张图纸目录。"""
    folder = os.path.basename(diagram_dir.rstrip(os.sep))
    diagram_name = f"{folder}.pdf"
    print(f"\n=== 处理图纸 {diagram_name} ===")

    # ---- 1. 图片向量化 ----
    images_dir = _find_images_dir(diagram_dir)
    if not images_dir:
        print(f"  [警告] 未找到 images 目录，跳过图片向量化")
    else:
        images = _list_images(images_dir)
        print(f"  共发现 {len(images)} 张图片")
        # 重新生成前先清理该图纸旧数据，避免重复
        mo_db.delete_diagram_rows(conn, config.PIC_VEC_TABLE, diagram_name)
        for img_path in images:
            pic_name = os.path.basename(img_path)
            try:
                vec = embedding_client.embed_image(img_path)
                mo_db.insert_pic_vec(conn, diagram_name, pic_name, vec)
                print(f"    [图片] {pic_name} -> {len(vec)} 维, 已入库")
            except Exception as e:  # noqa: BLE001
                print(f"    [失败] 图片 {pic_name}: {e}")
            time.sleep(config.REQUEST_INTERVAL_SECONDS)

    # ---- 2 & 3. md 分段 + 文本向量化 ----
    md_path = _find_markdown(diagram_dir)
    if not md_path:
        print(f"  [警告] 未找到 md 文件，跳过文本向量化")
        return

    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    chunks = chunk_markdown(md_text)
    print(f"  md 分段数：{len(chunks)}")

    mo_db.delete_diagram_rows(conn, config.TEXT_VEC_TABLE, diagram_name)
    for chunk in chunks:
        try:
            vec = embedding_client.embed_text(chunk.text)
            mo_db.insert_text_vec(conn, diagram_name, chunk.name, chunk.text, vec)
            print(f"    [分段] {chunk.name} -> {len(vec)} 维, 已入库")
        except Exception as e:  # noqa: BLE001
            print(f"    [失败] 分段 {chunk.name}: {e}")
        time.sleep(config.REQUEST_INTERVAL_SECONDS)


def _select_folders(start: Optional[str], names: List[str]) -> List[str]:
    """按字典序返回待处理的图纸文件夹名列表。

    - names 非空：只处理这些文件夹（原有行为）；
    - start 非空：从该文件夹起，按字典序处理到最后（断点续跑）；
    - 两者皆空：全部。
    start 容错：允许带 .pdf 后缀；若该名不存在，则从字典序 >= 它的第一个开始。
    """
    folders = [
        name for name in sorted(os.listdir(config.DIAGRAMS_DIR))
        if os.path.isdir(os.path.join(config.DIAGRAMS_DIR, name))
    ]
    if start:
        if start.lower().endswith(".pdf"):
            start = start[:-4]
        start_idx = next(
            (i for i, n in enumerate(folders) if n >= start), len(folders)
        )
        return folders[start_idx:]
    if names:
        sel = set(names)
        return [n for n in folders if n in sel]
    return folders


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="构建图纸知识库（向量入库）")
    parser.add_argument(
        "names", nargs="*",
        help="只处理这些图纸文件夹名（不传则全部）",
    )
    parser.add_argument(
        "--start",
        help="从该图纸文件夹名开始，按字典序处理到最后（断点续跑）。与位置参数互斥，优先生效。",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not os.path.isdir(config.DIAGRAMS_DIR):
        print(f"图纸目录不存在：{config.DIAGRAMS_DIR}")
        return 1

    targets = _select_folders(args.start, args.names)
    if not targets:
        print("没有匹配到要处理的图纸，结束。")
        return 0
    if args.start:
        print(f"从 {targets[0]} 开始，共处理 {len(targets)} 张图纸（到末尾）。")
    else:
        print(f"本次将处理 {len(targets)} 张图纸。")

    conn = mo_db.connect()
    try:
        mo_db.create_tables(conn)
        print("已确保向量表存在：", config.PIC_VEC_TABLE, ",", config.TEXT_VEC_TABLE)

        for name in targets:
            process_diagram(conn, os.path.join(config.DIAGRAMS_DIR, name))
    finally:
        conn.close()

    print("\n知识库构建完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
