from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from app.core.config import settings
from app.services.codex_cli_vlm import CodexCLIClient
from app.services.vlm_client import VLMClient
from app.workers import worker


def test_runner_uses_dashscope_for_trademark_reviews_by_default() -> None:
    runner = worker.Runner()

    assert settings.trademark_vlm_provider == "dashscope"
    assert runner.trademark_vlm is runner.vlm


def test_codex_cli_receives_whole_page_and_returns_existing_vlm_shape(real_pdf_path) -> None:  # noqa: ANN001
    client = CodexCLIClient(
        replace(
            settings,
            vlm_dry_run=False,
            trademark_vlm_provider="codex_cli",
            codex_model="gpt-5.6-sol",
        )
    )
    captured: dict = {}

    async def fake_run(command, prompt, output_path):  # noqa: ANN001, ANN202
        captured.update(command=command, prompt=prompt)
        output_path.write_text(
            json.dumps(
                {
                    "verdict": "fail",
                    "conclusion": "颜色不一致",
                    "evidence": [
                        {
                            "page": 1,
                            "rect": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1},
                            "text": "颜色标注",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return 0, ""

    client._run_codex = fake_run  # type: ignore[method-assign]  # noqa: SLF001
    outcome = asyncio.run(
        client.call_files(
            prompt_id="tm_marking_color",
            rule_title="商标颜色检查",
            files=[{"label": "商标图", "path": str(real_pdf_path), "pages": [1], "file_id": "tm"}],
        )
    )

    command = captured["command"]
    assert command[0] == "codex"
    assert (
        "--ephemeral" in command
        and ["--sandbox", "read-only"]
        == command[command.index("--sandbox") : command.index("--sandbox") + 2]
    )
    assert "--output-schema" in command and "--image" in command
    assert outcome.verdict == "fail"
    assert outcome.evidence[0]["file_id"] == "tm"
    assert outcome.evidence[0]["rect"]["w"] > 1
    assert outcome.vlm_raw["_meta"]["provider"] == "codex_cli"


def test_runner_switches_only_trademark_reviews_to_codex_cli(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        worker,
        "settings",
        replace(settings, trademark_vlm_provider="codex_cli"),
    )

    runner = worker.Runner()

    assert isinstance(runner.vlm, VLMClient)
    assert isinstance(runner.trademark_vlm, CodexCLIClient)
