# -*- coding: utf-8 -*-
"""性能测试：单个片段（局部图片）里每张局部图/每段局部文本，检索 MO 向量表的耗时。

只统计「对 MO 的向量查询」本身的耗时；不包含图片/文本的解析、DashScope 向量化、
建立数据库连接、拉取图纸列表等准备工作（全部在计时开始前完成）。

两种模式（--mode）：
  global      （默认）每张局部图 / 每段局部文本，各对「整张表」发一次查询
                （不按 diagram_name 过滤），即：
                    SELECT ..., cosine_similarity(vec_value, %s) AS sim
                    FROM <表> ORDER BY sim DESC LIMIT <top_k>
                —— 回答“检索整个图片向量表 / 整个文本向量表要多久”。
  per_diagram 线上匹配逻辑（matcher.score_diagram）的真实查询方式：对知识库
                每张图纸单独查一次「该图纸内 top1」，再跨图纸比较；查询次数
                = 局部图/分段数 × 图纸数（而不是表的总行数——每次查询只在
                该图纸自己的几行里算相似度）。

用法（在项目根目录下）：
    uv run python backend/perf_mo_search.py                          # 默认 20C114257-Y，global 模式
    uv run python backend/perf_mo_search.py 20C114257-Y
    uv run python backend/perf_mo_search.py 20C114257-Y --repeat 5   # 每张局部图/文本重复查询 5 次取统计
    uv run python backend/perf_mo_search.py 20C114257-Y --mode per_diagram
"""

import argparse
import os
import statistics
import sys
import time
from typing import List


def _load_env_file(path: str) -> None:
    """极简 .env 加载（与 start.sh 行为一致），不引入 python-dotenv 依赖。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_load_env_file(os.path.join(_PROJECT_ROOT, ".env"))

sys.path.insert(0, _THIS_DIR)  # backend 目录为模块根，与 match_cli.py 等脚本一致

import config  # noqa: E402
import matcher  # noqa: E402
import mo_db  # noqa: E402


def _find_fragment(xxx: str):
    """定位片段目录下 XXX 的 images 子目录与 _extracted.md（同 upload_pipeline 的做法）。"""
    images_dir = None
    subdir = None
    if os.path.isdir(config.FRAGMENTS_DIR):
        for name in os.listdir(config.FRAGMENTS_DIR):
            full = os.path.join(config.FRAGMENTS_DIR, name)
            if os.path.isdir(full) and name.startswith(xxx):
                subdir = full
                break
    if subdir:
        for root, _dirs, _files in os.walk(subdir):
            if os.path.basename(root) == "images":
                images_dir = root
                break

    md_path = os.path.join(config.FRAGMENTS_DIR, f"{xxx}_extracted.md")
    md_path = md_path if os.path.isfile(md_path) else None
    return images_dir, md_path


def _query_global(conn, table: str, name_col: str, vec, top_k: int = 1):
    """对整张表发一次向量检索（不按 diagram_name 过滤），取相似度最高的 top_k 行。"""
    cur = conn.cursor()
    lit = mo_db.vec_to_literal(vec)
    cur.execute(
        f"SELECT {name_col}, diagram_name, cosine_similarity(vec_value, %s) AS sim "
        f"FROM {table} ORDER BY sim DESC LIMIT %s",
        (lit, top_k),
    )
    return cur.fetchall()


def _table_row_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _report(label: str, samples: List[float]) -> float:
    total = sum(samples)
    n = len(samples)
    print(
        f"    {label}: n={n}  合计={total * 1000:.1f}ms  "
        f"均值={statistics.mean(samples) * 1000:.2f}ms  "
        f"中位数={statistics.median(samples) * 1000:.2f}ms  "
        f"最小={min(samples) * 1000:.2f}ms  最大={max(samples) * 1000:.2f}ms"
    )
    return total


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "xxx", nargs="?", default="20C114257-Y",
        help="片段名（不含扩展名），默认 20C114257-Y",
    )
    ap.add_argument(
        "--repeat", type=int, default=1,
        help="每张图纸/每次查询重复次数（用于摊平抖动），默认 1",
    )
    ap.add_argument(
        "--mode", choices=["global", "per_diagram"], default="global",
        help="global=对整张表查一次（默认）；per_diagram=复刻线上逐图纸查询",
    )
    ap.add_argument(
        "--top-k", type=int, default=1,
        help="global 模式下每次查询取相似度最高的前 K 行，默认 1",
    )
    args = ap.parse_args(argv[1:])

    images_dir, md_path = _find_fragment(args.xxx)
    if not images_dir and not md_path:
        raise SystemExit(
            f"片段目录下找不到 {args.xxx} 的产物（{args.xxx}_assets/images 或 "
            f"{args.xxx}_extracted.md），请先完成解析。"
        )

    print("=" * 70)
    print(f"MO 向量检索性能测试 | 片段：{args.xxx}")
    print(f"images_dir = {images_dir}")
    print(f"md_path    = {md_path}")
    print("=" * 70)

    # ---- 准备阶段（不计时）：解析结果的向量化 ----
    print("\n[准备阶段·不计入统计] 向量化片段图片与文本分段（DashScope embedding）…")
    t_prep = time.perf_counter()
    frag = matcher.embed_fragment(
        config.FRAGMENTS_DIR, images_dir=images_dir, md_path=md_path
    )
    print(
        f"  完成，耗时 {time.perf_counter() - t_prep:.2f}s（图片 {len(frag.image_vecs)} 张，"
        f"文本分段 {len(frag.chunk_vecs)} 段）— 此耗时不计入 MO 查询统计"
    )

    print("[准备阶段·不计入统计] 连接 MO…")
    conn = mo_db.connect()
    try:
        pic_rows = _table_row_count(conn, config.PIC_VEC_TABLE)
        text_rows = _table_row_count(conn, config.TEXT_VEC_TABLE)
        print(f"  view_pic_vec 总行数：{pic_rows}   view_text_vec 总行数：{text_rows}")

        grand_total = 0.0

        if args.mode == "global":
            # ---- 每张局部图 / 每段局部文本，各对整张表发一次查询 ----
            print(f"\n【图片相似度检索】view_pic_vec 全表（{pic_rows} 行）× top_k="
                  f"{args.top_k}" + (f" × repeat={args.repeat}" if args.repeat > 1 else ""))
            for src_name, _ref, vec in frag.image_vecs:
                samples = []
                for _ in range(args.repeat):
                    t0 = time.perf_counter()
                    _query_global(conn, config.PIC_VEC_TABLE, "pic_name", vec, args.top_k)
                    samples.append(time.perf_counter() - t0)
                grand_total += _report(src_name, samples)

            print(f"\n【文本相似度检索】view_text_vec 全表（{text_rows} 行）× top_k="
                  f"{args.top_k}" + (f" × repeat={args.repeat}" if args.repeat > 1 else ""))
            for src_name, _text, vec in frag.chunk_vecs:
                samples = []
                for _ in range(args.repeat):
                    t0 = time.perf_counter()
                    _query_global(conn, config.TEXT_VEC_TABLE, "chunk_name", vec, args.top_k)
                    samples.append(time.perf_counter() - t0)
                grand_total += _report(src_name, samples)

        else:  # per_diagram：复刻线上逐图纸查询（matcher._best_in_table）
            diagrams = matcher.list_diagrams(conn)
            print(f"  知识库图纸数：{len(diagrams)}")
            if not diagrams:
                raise SystemExit("知识库为空（view_pic_vec / view_text_vec 均无数据），无法测试。")

            print("\n【图片相似度检索】view_pic_vec，cosine_similarity，每张局部图片 × "
                  f"{len(diagrams)} 张图纸" + (f" × repeat={args.repeat}" if args.repeat > 1 else ""))
            for src_name, _ref, vec in frag.image_vecs:
                samples = []
                for dn in diagrams:
                    for _ in range(args.repeat):
                        t0 = time.perf_counter()
                        matcher._best_in_table(conn, config.PIC_VEC_TABLE, "pic_name", dn, vec)
                        samples.append(time.perf_counter() - t0)
                grand_total += _report(src_name, samples)

            print("\n【文本相似度检索】view_text_vec，cosine_similarity，每个文本分段 × "
                  f"{len(diagrams)} 张图纸" + (f" × repeat={args.repeat}" if args.repeat > 1 else ""))
            for src_name, _text, vec in frag.chunk_vecs:
                samples = []
                for dn in diagrams:
                    for _ in range(args.repeat):
                        t0 = time.perf_counter()
                        matcher._best_in_table(
                            conn, config.TEXT_VEC_TABLE, "chunk_name", dn, vec, with_text=True
                        )
                        samples.append(time.perf_counter() - t0)
                grand_total += _report(src_name, samples)
    finally:
        conn.close()

    print("\n" + "=" * 70)
    print(f"MO 向量查询总耗时（图片 + 文本，mode={args.mode}）：{grand_total * 1000:.1f} ms")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
