# -*- coding: utf-8 -*-
"""AI 匹配命令行入口（先以「片段」目录为输入跑通，后续再接上传/Web）。

用法：
    python match_cli.py                       # 匹配 片段/ 下唯一片段
    python match_cli.py ../片段               # 指定片段目录
    python match_cli.py ../片段 --iw 0.6 --tw 0.4   # 覆盖权重

输出包含「AI 思考过程」（对应规则第 1~5 步）与最终排名、排名第一图纸的明细。
"""

import io
import os
import sys

import config
import matcher

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass


def _resolve_fragment_dir(arg: str) -> str:
    """允许传入 片段 根目录或具体某个片段目录。"""
    if not os.path.isdir(arg):
        raise SystemExit(f"目录不存在：{arg}")
    # 若目录下直接含 *_assets/images，则它本身就是片段目录
    if matcher._find_images_dir(arg) and matcher._find_markdown(arg):
        return arg
    # 否则视为根目录，取其中第一个子片段
    subs = [
        os.path.join(arg, d)
        for d in sorted(os.listdir(arg))
        if os.path.isdir(os.path.join(arg, d))
    ]
    for s in subs:
        if matcher._find_markdown(os.path.dirname(s)) or matcher._find_markdown(s):
            return s
    # 退化：把根目录当片段目录（其下 *_assets 在同级）
    return arg


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    iw = tw = None
    for i, a in enumerate(argv):
        if a == "--iw":
            iw = float(argv[i + 1])
        if a == "--tw":
            tw = float(argv[i + 1])

    fragment_dir = args[0] if args else config.FRAGMENTS_DIR
    fragment_dir = _resolve_fragment_dir(fragment_dir)

    iw = iw if iw is not None else config.IMAGE_WEIGHT
    tw = tw if tw is not None else config.TEXT_WEIGHT

    print("=" * 70)
    print(f"AI 匹配 | 片段目录：{fragment_dir}")
    print(f"权重：图片 {iw:.0%}，文本 {tw:.0%}")
    print("=" * 70)

    scores = matcher.match(fragment_dir, image_weight=iw, text_weight=tw)

    # -------- AI 思考过程（规则第 1~5 步）--------
    print("\n【AI 思考过程】")
    print(f"步骤1 维度与权重：图片相似度 × {iw:.0%} + 文本相似度 × {tw:.0%} = 综合得分")
    print(f"        相似度函数：cosine_similarity（越大越相似）")

    for s in scores:
        print(f"\n  —— 候选图纸 {s.diagram_name} ——")
        print(f"  步骤2 图片维度：各片段图片在该图纸视图中的最相似度")
        for m in s.image_matches:
            print(f"        {m.source_name}  →  {m.target_name}  sim={m.similarity:.4f}")
        print(f"        图片原始得分(各图平分)= {s.image_raw:.4f}，× {iw:.0%} = {s.image_component:.4f}")
        print(f"  步骤3-4 文本维度：各片段分段在该图纸分段中的最相似度")
        for m in s.text_matches:
            print(f"        {m.source_name}  →  {m.target_name}  sim={m.similarity:.4f}")
        print(f"        文本原始得分(各段平分)= {s.text_raw:.4f}，× {tw:.0%} = {s.text_component:.4f}")
        print(f"  步骤5 综合得分 = {s.image_component:.4f} + {s.text_component:.4f} = {s.composite:.4f}")

    # -------- 步骤6 排名 --------
    print("\n【最终排名】（综合得分降序）")
    for rank, s in enumerate(scores, 1):
        print(f"  第{rank}名  {s.diagram_name}  综合得分={s.composite:.4f}")

    if not scores:
        print("\n知识库为空，无可比对图纸。")
        return 0

    top = scores[0]
    print("\n" + "=" * 70)
    print(f"最相似图纸（排名第一）：{top.diagram_name}  综合得分={top.composite:.4f}")
    print("=" * 70)
    print("各片段图片 ↔ 最相似图纸视图：")
    for m in top.image_matches:
        print(f"  {m.source_name}  ↔  {m.target_name}   (sim={m.similarity:.4f})")
    print("各片段分段 ↔ 最相似图纸文本：")
    for m in top.text_matches:
        print(f"  {m.source_name}  ↔  {m.target_name}   (sim={m.similarity:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
