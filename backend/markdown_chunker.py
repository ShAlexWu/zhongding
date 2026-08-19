# -*- coding: utf-8 -*-
"""Markdown 分段工具（匹配侧 / 片段）。

对应 Prompt.md「AI 匹配」第 3 条的「图片锚定分段」规则：
- 依次找到 md 中每一个图片链接；
- 对每个链接：向上找到最靠近的标题（任意层级）；
- 该分段 = 从该标题到「下一个同级（或更高级）标题」之前的全部内容，
  其中不包含图片链接文本；分段名称取该锚定标题的文字。

要点：
- 不含图片链接的小节不会成为分段（因为它没有可锚定的图片链接）。
- 若多个图片链接锚定到同一个标题，只产出一个分段（同一标题范围内容相同），
  避免该段在文本维度被重复计权。
"""

import re
from typing import List, NamedTuple

# 形如 "## 标题" 的 ATX 标题
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# 整行只有一个图片链接： ![alt](path)
IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# 行内的图片链接（用于剔除夹在文字中的图片）
INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


class Chunk(NamedTuple):
    name: str   # 分段名称（锚定标题文本）
    text: str   # 分段正文（已去除图片链接）


def _parse_headings(lines: List[str]):
    """返回 [(行号, 层级, 标题文本)]，按行号升序。"""
    result = []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            result.append((i, len(m.group(1)), m.group(2).strip()))
    return result


def _strip_images(text_lines: List[str]) -> str:
    """去掉图片链接后拼接为正文文本。"""
    kept = []
    for line in text_lines:
        if IMAGE_LINE_RE.match(line):
            continue
        # 去掉行内可能存在的图片链接
        line = INLINE_IMAGE_RE.sub("", line)
        kept.append(line)
    text = "\n".join(kept).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _nearest_heading_above(headings, img_line: int):
    """返回图片链接所在行之上、最靠近的标题；没有则返回 None。"""
    anchor = None
    for h in headings:               # headings 已按行号升序
        if h[0] < img_line:
            anchor = h
        else:
            break
    return anchor


def _section_end(headings, anchor_line: int, level: int) -> int:
    """锚定标题之后、第一个层级 <= 锚定层级的标题行号（即分段结束边界）。"""
    for h in headings:
        if h[0] > anchor_line and h[1] <= level:
            return h[0]
    return None


def _doc_title_line(headings):
    """唯一的最高一级标题视为文件标题（对应整页渲染图），不作为分段锚点。

    仅当最浅层级在全文只出现一次时，认定其为文件标题；否则返回 None。
    """
    levels = [lvl for _, lvl, _ in headings]
    top = min(levels)
    if levels.count(top) != 1:
        return None
    return next(ln for ln, lvl, _ in headings if lvl == top)


def chunk_by_image_anchor(md_text: str) -> List[Chunk]:
    """按「图片锚定」规则分段。"""
    lines = md_text.splitlines()
    headings = _parse_headings(lines)
    if not headings:
        return []

    title_line = _doc_title_line(headings)
    image_lines = [i for i, line in enumerate(lines) if IMAGE_LINE_RE.match(line)]

    chunks: List[Chunk] = []
    seen_anchor = set()
    for img_line in image_lines:
        anchor = _nearest_heading_above(headings, img_line)
        if anchor is None:
            continue
        anchor_line, level, title = anchor
        # 文件标题（整页渲染图所在小节）不分段
        if anchor_line == title_line:
            continue
        # 多个图片锚定同一标题时，只产出一次
        if anchor_line in seen_anchor:
            continue
        seen_anchor.add(anchor_line)

        end = _section_end(headings, anchor_line, level)
        if end is None:
            end = len(lines)

        body = _strip_images(lines[anchor_line + 1:end])
        if not body:
            continue
        # 锚定标题文本并入正文，提供语义上下文
        chunk_text = f"{title}\n{body}".strip()
        chunks.append(Chunk(name=title, text=chunk_text))

    return chunks


# ---------------------------------------------------------------------------
# 图纸侧分段：还原图纸某个分段的文本内容，供前端对比展示。
# 与知识库构建侧（knowledge_construct/markdown_chunker.chunk_markdown）以及
# 片段查询侧（chunk_by_image_anchor）必须使用同一套「图片锚定」规则，
# 文本相似度才在同一尺度上可比，故此处直接复用图片锚定分段。
# ---------------------------------------------------------------------------

def chunk_markdown(md_text: str) -> List[Chunk]:
    """图纸分段还原：与知识库构建一致，按「图片锚定」规则分段。"""
    return chunk_by_image_anchor(md_text)


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    result = chunk_by_image_anchor(content)
    print(f"分段数：{len(result)}\n")
    for c in result:
        print("=" * 60)
        print("CHUNK:", c.name)
        print(c.text)
