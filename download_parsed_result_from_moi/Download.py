# -*- coding: utf-8 -*-
"""从 MOI 平台下载指定工作区/卷下的解析结果，并编排到 ../图纸/ 目录。

交互输入（来自 MOI 平台）：Authorization、x-workspace-id、volume_id。
（page_size 固定为 100，与服务端单页返回上限一致，不需用户输入。）

流程：
  1. 调用 catalog/file/list 拉取该卷下的文件列表（每项是一个解析结果 zip）；
  2. 逐个调用 catalog/file/download 下载 zip；
  3. 解压后（含 <name>.pdf.md、<name>.pdf_parse.json、images/）编排成图纸库结构：
        图纸/<stem>/
            <stem>_extracted.md          # 由 <name>.pdf.md 原样改名（链接保持 images/ 不变）
            <stem>_assets/images/...      # 由解压出的 images/ 目录搬运而来
     （<name>.pdf_parse.json 不纳入最终结构，丢弃。）

用法：
    python Download.py                 # 处理列表中全部文件
    python Download.py --limit 1       # 只下载并编排第 1 个文件（先看效果）
    python Download.py --insecure      # 跳过 TLS 证书校验（内网/自签名证书时）
"""

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile

import requests

# 接口地址
_BASE = (
    "https://backend-zhongding.moi.shanghai.idc.matrixorigin.cn:30443"
    "/newmoi/catalog/file"
)
LIST_URL = f"{_BASE}/list"
DOWNLOAD_URL = f"{_BASE}/download"

# 列表接口单页返回上限（实测约 100）。固定按此翻页，offset 才能连续对齐，
# 既稳又无需用户输入 page_size。
PAGE_SIZE = 100

# 目录定位：脚本在 DownloadParseResultFromMOI/ 下，图纸库在项目根 图纸/ 下
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DIAGRAMS_DIR = os.path.join(PROJECT_ROOT, "图纸")

try:  # Windows 终端中文输出更稳
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------- 交互输入 ---------------------------------------

def _prompt(label: str) -> str:
    while True:
        val = input(f"{label}: ").strip()
        if val:
            return val
        print(f"  {label} 不能为空，请重新输入。")


# --------------------------- 接口调用 ---------------------------------------

def fetch_file_list(session, auth, workspace_id, volume_id):
    """拉取卷下文件列表，返回 list 数组（自动翻页累计全部）。

    page_size 固定为 PAGE_SIZE(100)，与服务端单页返回上限一致；按 data.total
    循环翻页，offset =(page-1)*PAGE_SIZE 连续对齐，直到收齐全部或某页为空。
    """
    headers = {
        "Authorization": auth,
        "x-workspace-id": workspace_id,
        "Content-Type": "application/json",
    }
    collected = []
    seen_ids = set()
    total = None
    page = 1
    step = PAGE_SIZE
    while True:
        body = {
            "page": page,
            "page_size": step,
            "order": "desc",
            "filters": [
                {"name": "volume_id", "values": [str(volume_id)]},
                {"name": "parent_id", "values": [""]},
            ],
        }
        resp = session.post(LIST_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "OK":
            raise RuntimeError(f"列表接口返回异常：{data}")
        d = data.get("data", {})
        batch = d.get("list", [])
        if total is None:
            total = d.get("total")
        # 用第 1 页实际返回条数校正步长（服务端单页封顶）
        if page == 1 and batch and len(batch) < step:
            step = len(batch)
        # 去重累计（防止服务端忽略 page 时把同一页重复计入）
        new = [x for x in batch if x.get("id") not in seen_ids]
        for x in new:
            seen_ids.add(x.get("id"))
        collected.extend(new)
        print(f"    第 {page} 页：返回 {len(batch)} 条，新增 {len(new)} 条，"
              f"累计 {len(collected)} 条，total={total}，步长={step}")
        # 终止：本页为空、或本页无新增（服务端忽略 page/已到尾）、或已收齐 total
        if not batch or not new:
            break
        if total is not None and len(collected) >= total:
            break
        page += 1
    return collected


def download_zip(session, auth, workspace_id, volume_id, file_id) -> bytes:
    """下载单个文件，返回 zip 字节。"""
    headers = {"Authorization": auth, "x-workspace-id": workspace_id}
    body = {"volume_id": str(volume_id), "file_id": file_id}
    resp = session.post(DOWNLOAD_URL, headers=headers, json=body, timeout=300)
    resp.raise_for_status()
    content = resp.content

    # 正常应直接返回 zip（以 'PK' 开头）。若不是，尝试当作 JSON 里携带下载 URL 处理。
    if content[:2] != b"PK":
        try:
            j = resp.json()
        except Exception:
            text = content[:500].decode("utf-8", "replace")
            raise RuntimeError(f"下载返回的不是 zip：{text}")
        data = j.get("data") or {}
        url = data.get("url") or data.get("download_url")
        if url:
            r2 = session.get(url, timeout=300)
            r2.raise_for_status()
            content = r2.content
        if content[:2] != b"PK":
            raise RuntimeError(f"下载返回的不是 zip：{str(j)[:500]}")
    return content


# --------------------------- 结构编排 ---------------------------------------

def _find_md_and_images(root: str):
    """在解压目录中递归定位 .md 文件与 images 目录。"""
    md_path = None
    images_dir = None
    for dirpath, dirnames, filenames in os.walk(root):
        if images_dir is None and "images" in dirnames:
            images_dir = os.path.join(dirpath, "images")
        for f in filenames:
            if f.lower().endswith(".md"):
                md_path = os.path.join(dirpath, f)
    return md_path, images_dir


def _stem_from_md(md_name: str) -> str:
    """由 md 文件名推出图纸短名：20C114319.pdf.md -> 20C114319。"""
    stem = md_name[:-3] if md_name.lower().endswith(".md") else md_name
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return stem


# markdown 图片链接： ![alt](path)
_IMG_LINK_RE = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")


def _rewrite_image_links(md_text: str, stem: str) -> str:
    """把 md 中图片链接的路径统一改写为 <stem>_assets/images/<文件名>。

    解析结果原始链接形如 ![alt](images/xxx.png)，缺少 <stem>_assets 一级目录；
    改写后与图纸库的物理结构一致。按文件名重建，已带前缀者保持等价（幂等）。
    """
    def repl(m: "re.Match") -> str:
        base = os.path.basename(m.group(2).strip())
        return f"{m.group(1)}{stem}_assets/images/{base}{m.group(3)}"

    return _IMG_LINK_RE.sub(repl, md_text)


def arrange(zip_bytes: bytes, list_name: str):
    """解压 zip 并编排到 图纸/<stem>/，返回 (stem, target_dir)。"""
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp)

        md_path, images_dir = _find_md_and_images(tmp)
        if not md_path:
            raise RuntimeError(f"zip 内未找到 .md 文件（{list_name}）")
        stem = _stem_from_md(os.path.basename(md_path))

        target_dir = os.path.join(DIAGRAMS_DIR, stem)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        assets_dir = os.path.join(target_dir, f"{stem}_assets")
        os.makedirs(assets_dir, exist_ok=True)

        # <name>.pdf.md -> <stem>_extracted.md
        # 改写图片链接，补上 <stem>_assets 一级目录，使其与物理结构一致
        with open(md_path, "r", encoding="utf-8") as fp:
            md_text = fp.read()
        md_text = _rewrite_image_links(md_text, stem)
        with open(os.path.join(target_dir, f"{stem}_extracted.md"), "w", encoding="utf-8") as fp:
            fp.write(md_text)

        # images 目录 -> <stem>_assets/images
        dst_images = os.path.join(assets_dir, "images")
        if images_dir and os.path.isdir(images_dir):
            shutil.copytree(images_dir, dst_images)
        else:
            os.makedirs(dst_images, exist_ok=True)
            print(f"      [提示] zip 内未找到 images 目录，已创建空目录（{list_name}）")

        return stem, target_dir


# --------------------------- 主流程 -----------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="下载 MOI 解析结果并编排到 图纸/")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只处理列表中前 N 个文件（<=0 表示全部）。先看效果时用 --limit 1。",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="跳过 TLS 证书校验（内网/自签名证书时使用）。",
    )
    args = parser.parse_args()

    print("请输入下列参数（来自 MOI 平台）：")
    auth = _prompt("Authorization")
    workspace_id = _prompt("x-workspace-id")
    volume_id = _prompt("volume_id")

    session = requests.Session()
    if args.insecure:
        session.verify = False
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass

    print("\n[1/2] 拉取文件列表…")
    files = fetch_file_list(session, auth, workspace_id, volume_id)
    files = [
        f for f in files
        if f.get("file_ext") == "zip" or str(f.get("name", "")).lower().endswith(".zip")
    ]
    print(f"  列表共 {len(files)} 个 zip 文件。")
    if args.limit and args.limit > 0:
        files = files[:args.limit]
        print(f"  --limit={args.limit}，本次只处理前 {len(files)} 个。")

    if not files:
        print("没有可处理的文件，结束。")
        return 0

    os.makedirs(DIAGRAMS_DIR, exist_ok=True)
    print("\n[2/2] 逐个下载并编排…")
    ok = 0
    for i, f in enumerate(files, 1):
        file_id = f.get("id")
        name = f.get("name", "")
        print(f"  ({i}/{len(files)}) 下载 {name} …")
        try:
            zip_bytes = download_zip(session, auth, workspace_id, volume_id, file_id)
            stem, _ = arrange(zip_bytes, name)
            print(f"      已编排 -> 图纸/{stem}/")
            ok += 1
        except Exception as e:  # noqa: BLE001  单个失败不影响其余
            print(f"      [失败] {name}: {e}")

    print(f"\n完成：成功 {ok}/{len(files)}。输出目录：{DIAGRAMS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
