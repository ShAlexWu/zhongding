"""VLM adapter: DashScope OpenAI-compatible mode.

- Semaphore(3) concurrency (SPEC section 4)
- temperature 0, forced JSON output, parse-fail retry <=2 (backoff 3s/9s)
- 429 rate-limit exponential backoff
- PDF pages and configured crops are rendered to images before VLM calls
- VLM_DRY_RUN=1 returns a deterministic mock; missing key returns skipped
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field

import pymupdf as fitz
from openai import AsyncOpenAI, RateLimitError

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.prompts import build_prompt, extract_json_payload

logger = get_logger(__name__)

VLM_PRICE_IN_PER_M = 2.0  # CNY per 1M input tokens (qwen-vl-max estimate)
VLM_PRICE_OUT_PER_M = 6.0  # CNY per 1M output tokens

ALLOWED_VERDICTS = {"pass", "fail", "warning"}


@dataclass
class VLMOutcome:
    verdict: str = "error"  # pass/fail/warning/skipped/error
    conclusion: str = ""
    evidence: list[dict] = field(default_factory=list)
    vlm_raw: dict | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cny: float | None = None
    latency_ms: int = 0
    error: str | None = None
    status: str = "done"
    vlm_attempts: int = 0


@dataclass
class RenderedPage:
    page: int  # 1-based page number within the source PDF
    png_b64: str
    file_id: str = ""  # owning project file (evidence anchoring)
    file_label: str = ""  # e.g. "商标图" / "总图 000A22G1G"
    seq: int = 0  # 1-based global image order sent to the model (multi-file)
    pdf_width: float = 0.0
    pdf_height: float = 0.0
    media_height: float = 0.0
    derotation: tuple[float, float, float, float, float, float] = (
        1.0, 0.0, 0.0, 1.0, 0.0, 0.0,
    )
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)


def render_pdf_pages(
    path: str,
    pages: list[int],
    dpi: int = 150,
    max_long_edge: int = 2200,
    crop: dict | None = None,
    zoom: float | None = None,
) -> list[RenderedPage]:
    """Render selected PDF pages to PNG (base64). Runs in threadpool by caller."""
    rendered: list[RenderedPage] = []
    with fitz.open(path) as pdf:
        for page_no in pages:
            if page_no < 1 or page_no > pdf.page_count:
                continue
            page = pdf[page_no - 1]
            rect = page.rect
            clip = None
            crop_box = (0.0, 0.0, 1.0, 1.0)
            if crop:
                x = max(0.0, min(float(crop.get("x", 0)), 1.0))
                y = max(0.0, min(float(crop.get("y", 0)), 1.0))
                w = max(0.0, min(float(crop.get("w", 1)), 1.0 - x))
                h = max(0.0, min(float(crop.get("h", 1)), 1.0 - y))
                if w <= 0 or h <= 0:
                    continue
                crop_box = (x, y, w, h)
                clip = fitz.Rect(
                    rect.x0 + x * rect.width,
                    rect.y0 + y * rect.height,
                    rect.x0 + (x + w) * rect.width,
                    rect.y0 + (y + h) * rect.height,
                )
            render_zoom = zoom
            if render_zoom is None:
                scale = min(1.0, max_long_edge / max(rect.width, rect.height))
                render_zoom = (dpi / 72.0) * scale
            pix = page.get_pixmap(matrix=fitz.Matrix(render_zoom, render_zoom), clip=clip)
            rendered.append(
                RenderedPage(
                    page=page_no,
                    png_b64=base64.b64encode(pix.tobytes("png")).decode(),
                    pdf_width=float(rect.width),
                    pdf_height=float(rect.height),
                    media_height=float(page.mediabox.height),
                    derotation=tuple(page.derotation_matrix),
                    crop=crop_box,
                )
            )
    return rendered


class VLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.vlm_concurrency)
        self._client: AsyncOpenAI | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.dashscope_api_key)

    def set_api_key(self, api_key: str) -> None:
        self.settings.dashscope_api_key = api_key
        self.settings.vlm_dry_run = False
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.settings.vlm_base_url,
                api_key=self.settings.dashscope_api_key,
                timeout=self.settings.vlm_timeout_s,
                max_retries=0,
            )
        return self._client

    async def _chat_once(
        self,
        prompt: str,
        images: list[RenderedPage],
        file_parts: list[dict] | None = None,
    ) -> tuple[str, dict]:
        """One chat completion. Returns (content, api_usage) — usage comes from the
        API response, not the model's JSON payload."""
        content: list[dict] = list(file_parts or [])
        content.append({"type": "text", "text": prompt})
        if not file_parts:
            for img in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img.png_b64}"},
                    }
                )
        response = await self._get_client().chat.completions.create(
            model=self.settings.vlm_model,
            messages=[{"role": "user", "content": content}],
            temperature=self.settings.vlm_temperature,
            response_format={"type": "json_object"},
        )
        usage = response.usage
        api_usage = {
            "prompt_tokens": int(usage.prompt_tokens or 0),
            "completion_tokens": int(usage.completion_tokens or 0),
        } if usage else {}
        return response.choices[0].message.content or "", api_usage

    async def chat_json(self, prompt: str) -> dict:
        """Run one text-only structured extraction through the configured model."""
        if not self.configured or self.settings.vlm_dry_run:
            return {}
        async with self._semaphore:
            for attempt in range(self.settings.vlm_max_attempts):
                try:
                    text, _ = await self._chat_once(prompt, [])
                    return extract_json_payload(text)
                except Exception as exc:  # noqa: BLE001 - extraction safely falls back to rules
                    logger.warning("text extraction failed attempt=%s: %s", attempt + 1, exc)
                    if attempt < len(self.settings.vlm_retry_backoff_s):
                        await asyncio.sleep(self.settings.vlm_retry_backoff_s[attempt])
        return {}

    async def call(
        self,
        prompt_id: str,
        rule_title: str,
        pdf_path: str,
        pages: list[int],
        doc_anchor: str = "",
        file_id: str = "",
    ) -> VLMOutcome:
        """Legacy single-file entry point; adapted onto the multi-file path."""
        files = [{"label": "", "path": pdf_path, "pages": pages, "file_id": file_id}]
        return await self.call_files(prompt_id, rule_title, files, doc_anchor)

    async def call_files(
        self,
        prompt_id: str,
        rule_title: str,
        files: list[dict],
        doc_anchor: str = "",
    ) -> VLMOutcome:
        """Multi-file VLM call; entries may include normalized ``crop`` and ``zoom``.

        Images are rendered in list order (trademark first, reference drawing after);
        the render order is the order the model sees them.
        """
        if not self.configured and not self.settings.vlm_dry_run:
            return VLMOutcome(
                verdict="warning",
                status="done",
                error="VLM API key 未配置，该项已跳过（需人工复核）",
                conclusion="VLM API key 未配置，需人工复核",
            )
        prompt = build_prompt(prompt_id, rule_title, doc_anchor)
        if self.settings.vlm_dry_run:
            await asyncio.sleep(0.05)  # simulate latency
            return self._mock(prompt_id, files)
        return await self._call_real(prompt, files, prompt_id)

    def _mock(self, prompt_id: str, files: list[dict]) -> VLMOutcome:
        """Deterministic dry-run result for tests and key-less demos."""
        raw = {
            "mock": True,
            "prompt_id": prompt_id,
            "model": self.settings.vlm_model,
            "files": [f.get("label") or str(f.get("path", "")).split("/")[-1] for f in files],
        }
        evidence: list[dict] = []
        for f in files:
            path = str(f.get("path", ""))
            page = (f.get("pages") or [1])[0]
            label = f.get("label") or ""
            name = path.split("/")[-1]
            desc = f"{label} {name}" if label else name
            evidence.append(
                {
                    "type": "pdf",
                    "file_id": f.get("file_id") or None,
                    "file_label": label or None,
                    "page": page,
                    "rect": None,
                    "text": (
                        f"[VLM dry-run 模拟输出] prompt_id={prompt_id}，"
                        f"未真实读图（{desc} 第 {page} 页）"
                    ),
                }
            )
        return VLMOutcome(
            verdict="warning",
            conclusion="VLM dry-run 模拟结果：未调用真实模型，需人工复核",
            evidence=evidence,
            vlm_raw=raw,
            status="done",
            latency_ms=50,
        )

    def _render_files(self, files: list[dict]) -> list[RenderedPage]:
        """Render pages of all files in order; seq is the global image index."""
        rendered: list[RenderedPage] = []
        seq = 0
        for f in files:
            pages = f.get("pages") or [1]
            for rp in render_pdf_pages(
                str(f["path"]),
                pages,
                self.settings.page_render_dpi,
                crop=f.get("crop"),
                zoom=f.get("zoom"),
            ):
                seq += 1
                rendered.append(
                    RenderedPage(
                        page=rp.page,
                        png_b64=rp.png_b64,
                        file_id=str(f.get("file_id") or ""),
                        file_label=str(f.get("label") or ""),
                        seq=seq,
                        pdf_width=rp.pdf_width,
                        pdf_height=rp.pdf_height,
                        media_height=rp.media_height,
                        derotation=rp.derotation,
                        crop=rp.crop,
                    )
                )
        return rendered

    async def _call_real(self, prompt: str, files: list[dict], prompt_id: str = "") -> VLMOutcome:
        rendered = await asyncio.to_thread(self._render_files, files)
        if not rendered:
            return VLMOutcome(
                verdict="warning",
                status="done",
                error="PDF 页渲染失败",
                conclusion="PDF 页渲染失败，需人工复核",
            )
        async with self._semaphore:
            start = time.monotonic()
            backoff = self.settings.vlm_retry_backoff_s
            last_error: str | None = None
            model_calls = 0
            for attempt in range(self.settings.vlm_max_attempts):
                try:
                    model_calls += 1
                    text, api_usage = await self._chat_once(prompt, rendered)
                    raw = extract_json_payload(text)
                    return self._finalize(raw, rendered, start, model_calls, api_usage, prompt_id)
                except RateLimitError:
                    last_error = "VLM 限速(429)"
                    delay = min(60.0, 5.0 * (2**attempt))
                    logger.warning("vlm 429 backoff %ss (attempt %s)", delay, attempt + 1)
                    await asyncio.sleep(delay)
                except ValueError as exc:  # JSON parse failure -> retry with backoff
                    last_error = f"VLM 返回解析失败: {exc}"
                    if attempt < len(backoff):
                        logger.warning("vlm parse retry in %ss (attempt %s)", backoff[attempt], attempt + 1)
                        await asyncio.sleep(backoff[attempt])
                except Exception as exc:  # noqa: BLE001
                    last_error = f"VLM 调用失败: {type(exc).__name__}: {exc}"
                    if attempt < len(backoff):
                        await asyncio.sleep(backoff[attempt])
            latency = int((time.monotonic() - start) * 1000)
            return VLMOutcome(
                verdict="warning",
                status="done",
                error=last_error,
                conclusion="VLM 调用失败，需人工复核",
                latency_ms=latency,
                vlm_attempts=model_calls,
            )

    def _finalize(
        self,
        raw: dict,
        rendered: list[RenderedPage],
        start: float,
        attempts: int,
        api_usage: dict | None = None,
        prompt_id: str = "",
    ) -> VLMOutcome:
        verdict = str(raw.get("verdict", "warning")).lower()
        if verdict not in ALLOWED_VERDICTS:
            verdict = "warning"
        evidence: list[dict] = []
        invalid_image_index = False
        for item in raw.get("evidence", []) or []:
            # The model reports the 1-based image index it saw; map that back to
            # the owning file (file_id) and the page inside that file.
            try:
                image_index = int(item.get("page"))
            except (TypeError, ValueError):
                invalid_image_index = True
                continue
            if image_index < 1 or image_index > len(rendered):
                invalid_image_index = True
                continue
            entry = rendered[image_index - 1]
            evidence_text = str(item.get("text", ""))[:400]
            if prompt_id == "tm_four_views_vs_ga" and entry.seq <= 8:
                role_view = {
                    1: ("general", "door_end"), 2: ("marking", "door_end"),
                    3: ("general", "side"), 4: ("marking", "side"),
                    5: ("general", "front_end"), 6: ("marking", "front_end"),
                    7: ("general", "roof"), 8: ("marking", "roof"),
                }.get(entry.seq)
                if role_view:
                    detail = evidence_text.partition(":")[2].strip() or evidence_text
                    source = "总图切片" if role_view[0] == "general" else "商标图切片"
                    evidence_text = f"TM-01 VIEW {' '.join(role_view)}: {source}：{detail}"
            rect = self._evidence_rect(
                {"x": 0, "y": 0, "w": 1, "h": 1}
                if prompt_id == "tm_four_views_vs_ga" and entry.seq <= 8
                else item.get("rect"),
                entry,
            )
            evidence.append(
                {
                    "type": "pdf",
                    "file_id": (entry.file_id if entry else "") or None,
                    "file_label": (entry.file_label if entry else "") or None,
                    "image_index": entry.seq,
                    "page": entry.page,
                    "rect": rect,
                    "text": evidence_text,
                }
            )
        if prompt_id == "tm_four_views_vs_ga":
            facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
            fact_evidence = facts.get("marking_drawing_number_evidence")
            fact_evidence = fact_evidence if isinstance(fact_evidence, dict) else {}
            drawing_number = str(facts.get("marking_drawing_number") or "").strip()
            try:
                fact_index = int(fact_evidence.get("image_index"))
            except (TypeError, ValueError):
                fact_index = 0
            entry = rendered[fact_index - 1] if 1 <= fact_index <= len(rendered) else None
            rect = self._evidence_rect(fact_evidence.get("rect"), entry) if entry else None
            if drawing_number and entry and entry.file_label == "商标图PDF" and rect:
                facts["marking_drawing_number_evidence"] = {
                    "type": "pdf",
                    "file_id": entry.file_id or None,
                    "file_label": entry.file_label,
                    "page": entry.page,
                    "rect": rect,
                    "text": str(fact_evidence.get("text") or drawing_number)[:400],
                }
            else:
                facts["marking_drawing_number"] = ""
                facts["marking_drawing_number_evidence"] = None
            raw["facts"] = facts
        problems: list[str] = []
        if invalid_image_index:
            problems.append("证据图片序号无效")
        if prompt_id == "tm_four_views_vs_ga":
            closure_problem = self._tm01_evidence_problem(evidence, rendered)
            if closure_problem:
                problems.append(closure_problem)
        conclusion = str(raw.get("conclusion", ""))[:400]
        if problems:
            verdict = "warning"
            note = "；".join(problems) + "，需人工复核"
            conclusion = f"{note}。原判定：{conclusion}" if conclusion else note
        usage = api_usage or raw.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = None
        if self.settings.vlm_model.startswith("qwen-vl-max"):
            cost = round(
                (tokens_in * VLM_PRICE_IN_PER_M + tokens_out * VLM_PRICE_OUT_PER_M) / 1_000_000,
                6,
            )
        raw["_meta"] = {**(raw.get("_meta") or {}), "vlm_attempts": attempts}
        return VLMOutcome(
            verdict=verdict,
            conclusion=conclusion,
            evidence=evidence,
            vlm_raw=raw,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_cny=cost,
            latency_ms=int((time.monotonic() - start) * 1000),
            status="done",
            vlm_attempts=attempts,
        )

    @staticmethod
    def _evidence_rect(value, entry: RenderedPage) -> dict | None:  # noqa: ANN001
        if not isinstance(value, dict):
            return None
        try:
            x, y, w, h = (float(value[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return None
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            return None
        unit = 1.0 if x + w <= 1.001 and y + h <= 1.001 else 1000.0
        if x + w > unit * 1.001 or y + h > unit * 1.001:
            return None
        crop_x, crop_y, crop_w, crop_h = entry.crop
        x0 = crop_x + x / unit * crop_w
        y0 = crop_y + y / unit * crop_h
        x1 = crop_x + (x + w) / unit * crop_w
        y1 = crop_y + (y + h) / unit * crop_h
        rendered_rect = fitz.Rect(
            x0 * entry.pdf_width,
            y0 * entry.pdf_height,
            x1 * entry.pdf_width,
            y1 * entry.pdf_height,
        )
        native = rendered_rect * fitz.Matrix(*entry.derotation)
        media_height = entry.media_height or entry.pdf_height
        return {
            "x": native.x0,
            "y": media_height - native.y1,
            "w": native.width,
            "h": native.height,
        }

    @staticmethod
    def _tm01_evidence_problem(evidence: list[dict], rendered: list[RenderedPage]) -> str:
        views = ("door_end", "side", "front_end", "roof")
        expected = {(role, view) for role in ("general", "marking") for view in views}
        expected_index = {
            ("general", "door_end"): 1,
            ("marking", "door_end"): 2,
            ("general", "side"): 3,
            ("marking", "side"): 4,
            ("general", "front_end"): 5,
            ("marking", "front_end"): 6,
            ("general", "roof"): 7,
            ("marking", "roof"): 8,
        }
        found: list[tuple[str, str]] = []
        rects: set[tuple] = set()
        dimensions = {item.file_label: (item.pdf_width, item.pdf_height) for item in rendered}
        invalid = 0
        for item in evidence:
            text = str(item.get("text", "")).strip()
            matched = None
            for role, view in expected:
                if text.startswith(f"TM-01 VIEW {role} {view}:"):
                    matched = (role, view)
                    break
            expected_label = "总图PDF" if matched and matched[0] == "general" else "商标图PDF"
            rect = item.get("rect")
            size = dimensions.get(expected_label, (0.0, 0.0))
            if (
                matched is None
                or item.get("file_label") != expected_label
                or item.get("image_index") != expected_index.get(matched)
                or not isinstance(rect, dict)
                or float(rect.get("w") or 0) <= 0
                or float(rect.get("h") or 0) <= 0
                or (size[0] and float(rect["w"]) >= size[0] * 0.9 and float(rect["h"]) >= size[1] * 0.9)
            ):
                invalid += 1
                continue
            found.append(matched)
            rects.add((item.get("file_id"), *(round(float(rect[k]), 3) for k in ("x", "y", "w", "h"))))
        missing = expected - set(found)
        duplicates = len(found) - len(set(found))
        duplicate_rects = len(found) - len(rects)
        if len(evidence) == 8 and not missing and not duplicates and not duplicate_rects and not invalid:
            return ""
        details = []
        if missing:
            details.append("缺少" + "、".join("-".join(item) for item in sorted(missing)))
        if duplicates:
            details.append(f"重复视图证据 {duplicates} 条")
        if duplicate_rects:
            details.append(f"重复定位框 {duplicate_rects} 个")
        if invalid:
            details.append(f"无效证据 {invalid} 条")
        if len(evidence) != 8:
            details.append(f"证据共 {len(evidence)} 条")
        return "TM-01 四视图八切片证据闭环不完整（" + "；".join(details) + "）"
