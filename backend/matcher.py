# -*- coding: utf-8 -*-
"""AI 匹配引擎。

严格按照 Prompt.md「AI 匹配」段落的规则实现，唯一区别：
相似度函数采用 MO 的 cosine_similarity（返回值越大越相似，
与「取最大值即最相似」「综合得分越大排名第一」的权重规则自洽），
而非 l2_distance（其越小越相似，与上述规则方向相反）。

每张候选图纸的综合得分计算：
- 图片维度原始得分 image_raw =
    片段各图片「在该图纸所有视图图片中的最大相似度」的平均值（各图片平分权重）
- 文本维度原始得分 text_raw =
    片段各分段「在该图纸所有分段中的最大相似度」的平均值（各分段平分权重）
- 综合得分 = image_raw * 图片权重 + text_raw * 文本权重
按综合得分降序排名，排名第一即最相似图纸。
"""

import os
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import config
import mo_db
import embedding_client
from markdown_chunker import chunk_by_image_anchor


# --------------------------- 数据结构 ---------------------------------------

@dataclass
class BestMatch:
    """片段某个图片/分段，在某图纸中的最相似命中。"""
    source_name: str          # 片段图片名 或 片段分段名
    target_name: str          # 命中的图纸视图图片名 或 图纸分段名
    similarity: float         # cosine_similarity 得分
    # 图片维度用于前端展示缩略图的相对路径；文本维度留空
    source_ref: str = ""      # 片段图片相对 片段目录 的路径
    target_ref: str = ""      # 图纸视图图片相对 图纸目录 的路径
    # 文本维度用于前端展示的分段全文；图片维度留空
    source_text: str = ""     # 片段分段全文
    target_text: str = ""     # 命中的图纸分段全文


@dataclass
class DiagramScore:
    diagram_name: str
    image_raw: float                       # 图片维度原始得分（未乘权重）
    text_raw: float                        # 文本维度原始得分（未乘权重）
    image_component: float                 # image_raw * 图片权重
    text_component: float                  # text_raw * 文本权重
    composite: float                       # 综合得分
    image_matches: List[BestMatch] = field(default_factory=list)
    text_matches: List[BestMatch] = field(default_factory=list)


@dataclass
class FragmentInput:
    """片段经向量化后的输入。"""
    image_vecs: List[Tuple[str, str, List[float]]]  # (片段图片名, 相对路径, 向量)
    chunk_vecs: List[Tuple[str, str, List[float]]]  # (片段分段名, 分段全文, 向量)


# --------------------------- 片段向量化 -------------------------------------

def _find_images_dir(fragment_dir: str) -> Optional[str]:
    for root, dirs, _files in os.walk(fragment_dir):
        for d in dirs:
            if d == "images":
                return os.path.join(root, d)
    return None


def _find_markdown(fragment_dir: str) -> Optional[str]:
    for name in os.listdir(fragment_dir):
        if name.lower().endswith(".md"):
            return os.path.join(fragment_dir, name)
    return None


def embed_fragment(
    fragment_dir: str,
    images_dir: Optional[str] = None,
    md_path: Optional[str] = None,
) -> FragmentInput:
    """对片段的图片和 md 分段进行向量化。

    可显式指定 images_dir / md_path 以精确锁定某个片段（片段目录可能同时
    存在多个零件的产物）；未指定时回退为在 fragment_dir 下自动查找。
    """
    image_vecs: List[Tuple[str, str, List[float]]] = []
    images_dir = images_dir or _find_images_dir(fragment_dir)
    if images_dir:
        for name in sorted(os.listdir(images_dir)):
            if name.lower().endswith(config.IMAGE_EXTENSIONS):
                path = os.path.join(images_dir, name)
                rel = os.path.relpath(path, config.FRAGMENTS_DIR).replace("\\", "/")
                image_vecs.append((name, rel, embedding_client.embed_image(path)))

    chunk_vecs: List[Tuple[str, str, List[float]]] = []
    md_path = md_path or _find_markdown(fragment_dir)
    if md_path:
        with open(md_path, "r", encoding="utf-8") as f:
            chunks = chunk_by_image_anchor(f.read())
        for chunk in chunks:
            chunk_vecs.append(
                (chunk.name, chunk.text, embedding_client.embed_text(chunk.text))
            )

    return FragmentInput(image_vecs=image_vecs, chunk_vecs=chunk_vecs)


# --------------------------- MO 相似度检索 ----------------------------------

def list_diagrams(conn) -> List[str]:
    """知识库中所有图纸名称（图片表与文本表的并集）。"""
    cur = conn.cursor()
    cur.execute(f"SELECT DISTINCT diagram_name FROM {config.PIC_VEC_TABLE}")
    names = {r[0] for r in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT diagram_name FROM {config.TEXT_VEC_TABLE}")
    names |= {r[0] for r in cur.fetchall()}
    return sorted(names)


def _best_in_table(
    conn, table: str, name_col: str, diagram_name: str, vec: List[float],
    with_text: bool = False,
):
    """在某图纸范围内，找与给定向量 cosine_similarity 最大的一行。

    返回 (命中行名称, 相似度)；该图纸无数据时返回 None。
    with_text=True 时（仅文本表）额外取出该行的 chunk_text，
    返回 (命中行名称, 分段原文, 相似度)。
    """
    cur = conn.cursor()
    lit = mo_db.vec_to_literal(vec)
    select_cols = f"{name_col}, chunk_text" if with_text else name_col
    cur.execute(
        f"SELECT {select_cols}, cosine_similarity(vec_value, %s) AS sim "
        f"FROM {table} WHERE diagram_name = %s "
        f"ORDER BY sim DESC LIMIT 1",
        (lit, diagram_name),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if with_text:
        return row[0], (row[1] or ""), float(row[2])
    return row[0], float(row[1])


def score_diagram(conn, diagram_name: str, frag: FragmentInput) -> DiagramScore:
    """计算片段对单张图纸的综合得分及各维度最相似命中。"""
    # ---- 图片维度 ----
    image_matches: List[BestMatch] = []
    image_sims: List[float] = []
    folder = diagram_name[:-4] if diagram_name.lower().endswith(".pdf") else diagram_name
    for src_name, src_ref, vec in frag.image_vecs:
        hit = _best_in_table(
            conn, config.PIC_VEC_TABLE, "pic_name", diagram_name, vec
        )
        if hit is None:
            continue
        tgt, sim = hit
        # 图纸视图图片在 图纸目录 下的相对路径：<folder>/<folder>_assets/images/<pic>
        tgt_ref = f"{folder}/{folder}_assets/images/{tgt}"
        image_matches.append(
            BestMatch(src_name, tgt, sim, source_ref=src_ref, target_ref=tgt_ref)
        )
        image_sims.append(sim)
    image_raw = sum(image_sims) / len(image_sims) if image_sims else 0.0

    # ---- 文本维度 ----
    text_matches: List[BestMatch] = []
    text_sims: List[float] = []
    for src_name, src_text, vec in frag.chunk_vecs:
        hit = _best_in_table(
            conn, config.TEXT_VEC_TABLE, "chunk_name", diagram_name, vec,
            with_text=True,
        )
        if hit is None:
            continue
        tgt, tgt_text, sim = hit
        text_matches.append(
            BestMatch(
                src_name, tgt, sim,
                source_text=src_text,
                target_text=tgt_text,
            )
        )
        text_sims.append(sim)
    text_raw = sum(text_sims) / len(text_sims) if text_sims else 0.0

    image_component = image_raw * config.IMAGE_WEIGHT
    text_component = text_raw * config.TEXT_WEIGHT
    composite = image_component + text_component

    return DiagramScore(
        diagram_name=diagram_name,
        image_raw=image_raw,
        text_raw=text_raw,
        image_component=image_component,
        text_component=text_component,
        composite=composite,
        image_matches=image_matches,
        text_matches=text_matches,
    )


def _fmt_pct(w: float) -> str:
    return f"{w * 100:.0f}%"


def _describe_graphic(s: DiagramScore) -> Iterator[str]:
    """把单张图纸的比对明细（各片段相似度 + 加权过程）转成进度文案。"""
    yield f"正在和 {s.diagram_name} 比对："
    if s.image_matches:
        parts = "，".join(f"{m.source_name}={m.similarity:.3f}" for m in s.image_matches)
        yield (f"  图片：{parts} → 均值 {s.image_raw:.3f} × "
               f"{_fmt_pct(config.IMAGE_WEIGHT)} = {s.image_component:.3f}")
    if s.text_matches:
        parts = "，".join(f"{m.source_name}={m.similarity:.3f}" for m in s.text_matches)
        yield (f"  文本：{parts} → 均值 {s.text_raw:.3f} × "
               f"{_fmt_pct(config.TEXT_WEIGHT)} = {s.text_component:.3f}")
    yield f"  → 综合得分 {s.composite:.4f}"


def _parallel_iter(
    diagrams: List[str],
    work_fn: Callable,
    describe_fn: Callable[[object], Iterator[str]],
) -> Iterator[dict]:
    """并行对每张图纸执行 work_fn(conn, dn) -> score，按完成顺序产出进度。

    - 线程数 n = min(config.MATCH_WORKERS, 图纸数)；预先在主线程顺序建好 n 条独立
      MO 连接（任一失败立刻清晰报错，避免在线程池 initializer 里建连失败导致
      「线程池损坏」的隐晦错误），每条连接对应一个固定窗口号（0..n-1）。
    - 任务从「车道队列」借一条 (窗口号, 连接) 用完归还；先产出 {"start": n,
      "total": T} 宣告并行阶段；每张图纸完成时把 describe_fn(score) 的每行包成
      {"worker": 窗口号, "msg": 行文本} 产出（仅主线程 yield，线程安全）。
    - 生成器结束时通过 return 返回收集到的 scores 列表（未排序）。
    """
    n = max(1, min(config.MATCH_WORKERS, len(diagrams)))

    # 预先建好 n 条连接（顺序、主线程）；任一失败则清理已建连接并清晰报错
    lanes: "queue.Queue" = queue.Queue()   # 元素：(窗口号, 连接)
    conns: List = []
    try:
        for i in range(n):
            conn = mo_db.connect()
            conns.append(conn)
            lanes.put((i, conn))
    except Exception as e:  # noqa: BLE001
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(
            f"建立数据库连接失败（已建 {len(conns)}/{n} 条，可调小 MATCH_WORKERS）：{e}"
        ) from e

    def _task(dn: str):
        wid, conn = lanes.get()
        try:
            return wid, dn, work_fn(conn, dn), None
        except Exception as e:  # noqa: BLE001  单张失败不影响整体
            return wid, dn, None, str(e)
        finally:
            lanes.put((wid, conn))

    scores: List = []
    yield {"start": n, "total": len(diagrams)}
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(_task, dn) for dn in diagrams]
            for fut in as_completed(futures):
                wid, dn, score, err = fut.result()
                if err is not None:
                    yield {"worker": wid, "msg": f"[失败] {dn}: {err}"}
                    continue
                if score is None:
                    continue
                scores.append(score)
                for line in describe_fn(score):
                    yield {"worker": wid, "msg": line}
    finally:
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    return scores


def iter_match(
    fragment_dir: str,
    image_weight: Optional[float] = None,
    text_weight: Optional[float] = None,
    images_dir: Optional[str] = None,
    md_path: Optional[str] = None,
) -> Iterator[str]:
    """流式版 AI 匹配：逐图纸 yield 比对进度文案，最终 return 排序后的图纸列表。

    进度文案含「正在和 X.pdf 比对」「各片段相似度」「均值×权重」「综合得分」，
    便于前端实时展示比对/打分过程；生成器结束时通过 return 返回 scores 列表
    （调用方用 StopIteration.value 取回）。
    """
    if image_weight is not None:
        config.IMAGE_WEIGHT = image_weight
    if text_weight is not None:
        config.TEXT_WEIGHT = text_weight

    t0 = time.perf_counter()
    frag = embed_fragment(fragment_dir, images_dir=images_dir, md_path=md_path)
    yield {"timing": {"vectorize": time.perf_counter() - t0}}

    conn = mo_db.connect()
    try:
        diagrams = list_diagrams(conn)
    finally:
        conn.close()

    t1 = time.perf_counter()
    scores = yield from _parallel_iter(
        diagrams,
        lambda c, dn: score_diagram(c, dn, frag),
        _describe_graphic,
    )
    yield {"timing": {"mo_match": time.perf_counter() - t1}}

    scores.sort(key=lambda s: s.composite, reverse=True)
    return scores


def match(
    fragment_dir: str,
    image_weight: Optional[float] = None,
    text_weight: Optional[float] = None,
    images_dir: Optional[str] = None,
    md_path: Optional[str] = None,
) -> List[DiagramScore]:
    """对一个片段执行 AI 匹配，返回按综合得分降序的图纸列表（非流式封装）。"""
    gen = iter_match(fragment_dir, image_weight, text_weight, images_dir, md_path)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value or []


# --------------------------- 文本模式匹配 -----------------------------------

@dataclass
class TextDiagramScore:
    """文本模式下，整篇 md 对单张图纸的匹配结果。"""
    diagram_name: str
    similarity: float                 # 整篇 md 向量与该图纸最相似分段的 cosine
    best_chunk_name: str              # 最相似的图纸分段名
    best_chunk_text: str              # 最相似图纸分段全文


def iter_match_text(md_text: str) -> Iterator[str]:
    """流式版文本模式匹配：片段整篇 md 取「一个整体向量」，与每张图纸的分段向量做
    cosine_similarity，取该图纸最相似的分段作为其得分；逐图纸 yield 进度文案，
    最终 return 按得分降序排序的图纸列表。

    与图纸「按图片链接整段入库」的口径一致：整体 vs 整段。不再把命中分段按行
    拆开重排——逐行匹配会被换行/排版结构差异带偏（同族零件的技术要求文字几乎
    相同，谁的换行更接近片段谁就虚高），整体向量反而更稳、更贴合分段口径。
    """
    t0 = time.perf_counter()
    whole_vec = embedding_client.embed_text(md_text)
    yield {"timing": {"vectorize": time.perf_counter() - t0}}

    conn = mo_db.connect()
    try:
        diagrams = list_diagrams(conn)
    finally:
        conn.close()

    def _work(c, dn: str):
        hit = _best_in_table(
            c, config.TEXT_VEC_TABLE, "chunk_name", dn, whole_vec, with_text=True
        )
        if hit is None:
            return None
        chunk_name, chunk_text, sim = hit
        return TextDiagramScore(
            diagram_name=dn,
            similarity=sim,
            best_chunk_name=chunk_name,
            best_chunk_text=chunk_text,
        )

    def _describe(s: TextDiagramScore) -> Iterator[str]:
        yield (f"正在和 {s.diagram_name} 比对：文本相似度 {s.similarity:.4f}"
               f"（最相似分段：{s.best_chunk_name}）")

    t1 = time.perf_counter()
    scores = yield from _parallel_iter(diagrams, _work, _describe)
    yield {"timing": {"mo_match": time.perf_counter() - t1}}

    scores.sort(key=lambda s: s.similarity, reverse=True)
    return scores


def match_text(md_text: str) -> List[TextDiagramScore]:
    """文本模式匹配（非流式封装），返回按得分降序的图纸列表。"""
    gen = iter_match_text(md_text)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value or []
