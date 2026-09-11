"""VLM client tests: dry-run mock, PNG rendering, JSON payload parsing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.prompts import build_prompt, extract_json_payload
from app.services.vlm_client import RenderedPage, VLMClient, render_pdf_pages


def _settings(dry_run: bool, key: str | None = None) -> Settings:
    return Settings(
        data_dir=Path("/tmp"),
        db_path=Path("/tmp/t.db"),
        rules_path=Path("/tmp/rules.json"),
        vlm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        vlm_model="qwen-vl-max-latest",
        dashscope_api_key=key if key is not None else ("" if dry_run else "sk-test"),
        vlm_dry_run=dry_run,
        png_cache_dir=Path("/tmp/png"),
    )


def test_dry_run_returns_mock(real_pdf_path) -> None:  # noqa: ANN001
    client = VLMClient(_settings(dry_run=True))
    outcome = asyncio.run(
        client.call(
            prompt_id="gen_sealant_notes",
            rule_title="打胶注释完整性检查",
            pdf_path=str(real_pdf_path),
            pages=[1],
            file_id="f1",
        )
    )
    assert outcome.status == "done"
    assert outcome.verdict == "warning"
    assert outcome.vlm_raw and outcome.vlm_raw.get("mock") is True
    assert outcome.evidence and outcome.evidence[0]["type"] == "pdf"


def test_missing_key_finishes_as_manual_review(real_pdf_path) -> None:  # noqa: ANN001
    client = VLMClient(_settings(dry_run=False, key=""))
    outcome = asyncio.run(
        client.call(
            prompt_id="gen_weld_notes",
            rule_title="焊接注释完整性",
            pdf_path=str(real_pdf_path),
            pages=[1],
        )
    )
    assert outcome.status == "done"
    assert outcome.verdict == "warning"
    assert "人工复核" in outcome.conclusion


def test_render_pdf_pages(real_pdf_path) -> None:  # noqa: ANN001
    rendered = render_pdf_pages(str(real_pdf_path), pages=[1], dpi=150)
    assert len(rendered) == 1
    assert rendered[0].page == 1
    assert rendered[0].png_b64


def test_render_pdf_page_out_of_range(real_pdf_path) -> None:  # noqa: ANN001
    rendered = render_pdf_pages(str(real_pdf_path), pages=[999], dpi=150)
    assert rendered == []


def test_qwen_1000_bbox_maps_to_rotated_pdf_user_space() -> None:
    page = RenderedPage(
        page=1,
        png_b64="",
        pdf_width=1190.52,
        pdf_height=841.92,
        media_height=1190.52,
        derotation=(0.0, 1.0, -1.0, 0.0, 841.92, 0.0),
    )

    rect = VLMClient._evidence_rect(  # noqa: SLF001
        {"x": 156, "y": 326, "w": 114, "h": 156},
        page,
    )

    assert rect == pytest.approx({
        "x": 436.11456,
        "y": 869.07962,
        "w": 131.33952,
        "h": 135.71928,
    })


def test_extract_json_payload() -> None:
    raw = '```json\n{"verdict": "pass", "conclusion": "ok", "evidence": []}\n```'
    parsed = extract_json_payload(raw)
    assert parsed["verdict"] == "pass"


def test_extract_json_payload_no_fence() -> None:
    raw = '前导文字 {"verdict": "warning", "evidence": [{"page": 1, "text": "x"}]} 尾随'
    parsed = extract_json_payload(raw)
    assert parsed["verdict"] == "warning"


def test_extract_json_payload_invalid() -> None:
    with pytest.raises(ValueError):
        extract_json_payload("no json here")


def test_build_prompt_contains_anchor() -> None:
    prompt = build_prompt("tm_marking_color", "检查颜色", "外面漆颜色: 白")
    assert "外面漆颜色: 白" in prompt
    assert "json" in prompt.lower()


def test_failed_vlm_call_reports_internal_attempt_count(real_pdf_path) -> None:  # noqa: ANN001
    client = VLMClient(replace(
        _settings(dry_run=False),
        vlm_max_attempts=2,
        vlm_retry_backoff_s=[],
    ))

    async def invalid_json(prompt, images):  # noqa: ANN001
        return "not json", {}

    client._chat_once = invalid_json  # type: ignore[method-assign]  # noqa: SLF001
    outcome = asyncio.run(
        client.call("gen_weld_notes", "焊接", str(real_pdf_path), [1])
    )

    assert outcome.vlm_attempts == 2


def test_openai_sdk_retries_are_disabled() -> None:
    client = VLMClient(_settings(dry_run=False))._get_client()  # noqa: SLF001
    assert client.max_retries == 0
