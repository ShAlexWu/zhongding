"""Codex CLI adapter used only by trademark VLM rules."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import time
from pathlib import Path

from app.services.prompts import build_prompt, extract_json_payload
from app.services.vlm_client import VLMClient, VLMOutcome

_RECT = {
    "type": "object",
    "properties": {key: {"type": "number"} for key in ("x", "y", "w", "h")},
    "required": ["x", "y", "w", "h"],
    "additionalProperties": False,
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "warning"]},
        "confidence": {"type": "number"},
        "conclusion": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "rect": {"anyOf": [_RECT, {"type": "null"}]},
                    "text": {"type": "string"},
                },
                "required": ["page", "rect", "text"],
                "additionalProperties": False,
            },
        },
        "facts": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "door_end_verdict": {
                            "type": "string", "enum": ["pass", "fail", "warning"]
                        },
                        "door_end_conclusion": {"type": "string"},
                        "side_verdict": {
                            "type": "string", "enum": ["pass", "fail", "warning"]
                        },
                        "side_conclusion": {"type": "string"},
                        "front_end_verdict": {
                            "type": "string", "enum": ["pass", "fail", "warning"]
                        },
                        "front_end_conclusion": {"type": "string"},
                        "roof_verdict": {
                            "type": "string", "enum": ["pass", "fail", "warning"]
                        },
                        "roof_conclusion": {"type": "string"},
                        "marking_drawing_number": {"type": "string"},
                        "marking_drawing_number_evidence": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "image_index": {"type": "integer"},
                                        "rect": {"anyOf": [_RECT, {"type": "null"}]},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["image_index", "rect", "text"],
                                    "additionalProperties": False,
                                },
                                {"type": "null"},
                            ],
                        },
                    },
                    "required": [
                        "door_end_verdict",
                        "door_end_conclusion",
                        "side_verdict",
                        "side_conclusion",
                        "front_end_verdict",
                        "front_end_conclusion",
                        "roof_verdict",
                        "roof_conclusion",
                        "marking_drawing_number",
                        "marking_drawing_number_evidence",
                    ],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "csc_plate_title": {"type": "string"},
                        "csc_plate_rect": {"anyOf": [_RECT, {"type": "null"}]},
                        "allowable_stacking_load_1_8g_kg": {
                            "anyOf": [{"type": "number"}, {"type": "null"}]
                        },
                        "transverse_racking_test_force_n": {
                            "anyOf": [{"type": "number"}, {"type": "null"}]
                        },
                    },
                    "required": [
                        "csc_plate_title",
                        "csc_plate_rect",
                        "allowable_stacking_load_1_8g_kg",
                        "transverse_racking_test_force_n",
                    ],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["verdict", "confidence", "conclusion", "evidence", "facts"],
    "additionalProperties": False,
}


class CodexCLIClient(VLMClient):
    async def call_files(
        self,
        prompt_id: str,
        rule_title: str,
        files: list[dict],
        doc_anchor: str = "",
    ) -> VLMOutcome:
        if self.settings.vlm_dry_run:
            await asyncio.sleep(0.05)
            return self._mock(prompt_id, files)
        if not shutil.which(self.settings.codex_cli_path):
            return VLMOutcome(
                verdict="warning",
                conclusion="Codex CLI 未安装，需人工复核",
                error="Codex CLI executable not found",
            )

        rendered = await asyncio.to_thread(self._render_files, files)
        if not rendered:
            return VLMOutcome(verdict="warning", conclusion="PDF 页渲染失败，需人工复核")

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="zhongji-codex-vlm-") as temp_dir:
            root = Path(temp_dir)
            images: list[Path] = []
            for item in rendered:
                image = root / f"image-{item.seq}.png"
                image.write_bytes(base64.b64decode(item.png_b64))
                images.append(image)
            schema_path = root / "output-schema.json"
            output_path = root / "result.json"
            schema_path.write_text(json.dumps(_OUTPUT_SCHEMA), encoding="utf-8")
            command = [
                self.settings.codex_cli_path,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(root),
                "--model",
                self.settings.codex_model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--color",
                "never",
            ]
            for image in images:
                command.extend(("--image", str(image)))
            command.append("-")
            returncode, stderr = await self._run_codex(
                command,
                build_prompt(prompt_id, rule_title, doc_anchor),
                output_path,
            )
            if returncode != 0 or not output_path.is_file():
                detail = stderr.strip()[-300:]
                return VLMOutcome(
                    verdict="warning",
                    conclusion="Codex CLI 调用失败，需人工复核",
                    error=detail or f"Codex CLI exited with {returncode}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    vlm_attempts=1,
                )
            raw = extract_json_payload(output_path.read_text(encoding="utf-8"))
            raw["_meta"] = {
                **(raw.get("_meta") or {}),
                "provider": "codex_cli",
                "model": self.settings.codex_model,
            }
            outcome = self._finalize(raw, rendered, started, 1, prompt_id=prompt_id)
            outcome.cost_cny = None
            return outcome

    async def _run_codex(
        self,
        command: list[str],
        prompt: str,
        output_path: Path,
    ) -> tuple[int, str]:
        del output_path
        async with self._semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode()),
                    timeout=self.settings.vlm_timeout_s,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return 124, "Codex CLI timed out"
            return process.returncode or 0, stderr.decode(errors="replace")
