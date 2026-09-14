"""Background worker: asyncio.Queue + Semaphore(3) + SQLite-backed state.

Consumes tasks submitted by the API layer, executes the 40 rules
(34 rule-engine + 6 trademark VLM with concurrency limit), persists results and
emits job_events consumed by the SSE endpoint.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.models import Project, RuleResult
from app.services import task_service
from app.services.codex_cli_vlm import CodexCLIClient
from app.services.fact_layer import FactLayer
from app.services.manual_fact_extractor import ManualFactExtractor
from app.services.rule_engine import RULES, build_context, resolve_doc_anchor, run_rule
from app.services.vlm_client import VLMClient, VLMOutcome

logger = get_logger(__name__)

_TM01_VIEW_CROPS = {
    "door_end": {"x": 0.08, "y": 0.02, "w": 0.24, "h": 0.29},
    "side": {"x": 0.31, "y": 0.02, "w": 0.42, "h": 0.29},
    "front_end": {"x": 0.72, "y": 0.02, "w": 0.23, "h": 0.29},
    "roof": {"x": 0.30, "y": 0.25, "w": 0.44, "h": 0.30},
}
_TM01_TITLE_CROP = {"x": 0.75, "y": 0.78, "w": 0.24, "h": 0.21}
_TM01_RENDER_ZOOM = 4.0


def _missing_baseline_note(rule, missing: list[str], ctx, anchor: str) -> str:  # noqa: ANN001
    problems = [
        ("商标图" if item == "trademark" else f"总图 {item}") + "缺失"
        for item in missing
    ]
    anchor_expr = (rule.params or {}).get("doc_anchor", "")
    if "manual" in rule.target_files and (ctx.manual is None or not anchor.strip()):
        problems.append("说明书基准缺失")
    if "input:tech_req" in anchor_expr and not any(
        str(ctx.inputs.get(key) or "").strip() for key in ("tech_req", "new_material")
    ):
        problems.append("客户技术要求未提供")
    if not problems:
        return ""
    return "、".join(dict.fromkeys(problems)) + "，无法完成对比，需人工复核"


def _adjudicate_tm01(outcome: VLMOutcome) -> VLMOutcome:
    raw = outcome.vlm_raw or {}
    if not raw:
        return outcome
    original_evidence = outcome.evidence
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    checks = (
        ("门端视图", "door_end", 1, 2),
        ("侧板视图", "side", 3, 4),
        ("前端视图", "front_end", 5, 6),
        ("顶板视图", "roof", 7, 8),
    )
    evidence: list[dict] = []
    verdicts: list[str] = []
    conclusions: list[str] = []
    for checkpoint, view, general_index, marking_index in checks:
        selected = [
            item for item in outcome.evidence
            if str(item.get("text") or "").startswith("TM-01 VIEW ")
            and item.get("image_index") in (general_index, marking_index)
        ]
        verdict = str(facts.get(f"{view}_verdict") or "warning").lower()
        if verdict not in {"pass", "fail", "warning"} or len(selected) != 2:
            verdict = "warning"
        by_image = {item.get("image_index"): item for item in selected}
        general = by_image.get(general_index)
        marking = by_image.get(marking_index)
        duplicated = False
        if general and marking:
            general_detail = str(general.get("text") or "").partition(":")[2].strip()
            marking_detail = str(marking.get("text") or "").partition(":")[2].strip()
            general_detail = general_detail.removeprefix("总图切片：").strip()
            marking_detail = marking_detail.removeprefix("商标图切片：").strip()
            if general_detail and general_detail == marking_detail:
                duplicated = True
                general["text"] = (
                    f"TM-01 VIEW general {view}: 总图切片与商标图切片返回了完全相同的描述，"
                    "无法确认模型已分别识别"
                )
                marking["text"] = (
                    f"TM-01 VIEW marking {view}: 商标图切片与总图切片返回了完全相同的描述，"
                    "无法确认模型已分别识别"
                )
        conclusion = str(facts.get(f"{view}_conclusion") or "").strip()
        if duplicated:
            verdict = "warning"
            conclusion = "两张切片描述完全重复，需人工复核"
        verdicts.append(verdict)
        conclusions.append(f"{checkpoint}：{conclusion or '证据不完整，需人工复核'}")
        evidence.extend(
            {**item, "checkpoint": checkpoint, "checkpoint_verdict": verdict}
            for item in selected
        )
    outcome.verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    outcome.conclusion = "；".join(conclusions)
    outcome.evidence = evidence or original_evidence
    return outcome


def _adjudicate_tm04(outcome: VLMOutcome, manual) -> VLMOutcome:  # noqa: ANN001
    expected = manual.extract_ratings() if manual else {}
    if not expected:
        return outcome
    source_text = manual.extract_rating_evidence()
    raw = outcome.vlm_raw or {}
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}

    def number(value) -> float | None:  # noqa: ANN001
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    checks = (
        ("最大总重", "max_gross", "max_gross_kg"),
        ("皮重", "tare", "tare_weight_kg"),
        ("载重", "payload", "payload_kg"),
    )
    evidence: list[dict] = []
    verdicts: list[str] = []
    details: list[str] = []
    for checkpoint, field, manual_key in checks:
        wanted_kg = expected.get(manual_key)
        wanted_lb = round(wanted_kg * 2.20462262185 / 10) * 10 if wanted_kg is not None else None
        source_verdicts: list[str] = []
        values: dict[str, tuple[float | None, float | None]] = {}
        drawing_evidence: list[dict] = []
        for source, label in (("marking", "商标图"), ("general", "总图")):
            source_facts = facts.get(source) if isinstance(facts.get(source), dict) else {}
            kg = number(source_facts.get(f"{field}_kg"))
            lb = number(source_facts.get(f"{field}_lb"))
            drawing = next(
                (
                    item for item in outcome.evidence
                    if str(item.get("text") or "").startswith(f"TM-04 {source} {field}:")
                ),
                None,
            )
            if drawing:
                text = str(drawing.get("text") or "")
                kg_match = re.search(r"([\d,]+(?:\.\d+)?)\s*KGS?\b", text, re.IGNORECASE)
                lb_match = re.search(r"([\d,]+(?:\.\d+)?)\s*LBS?\b", text, re.IGNORECASE)
                kg = kg if kg is not None else number(kg_match.group(1) if kg_match else None)
                lb = lb if lb is not None else number(lb_match.group(1) if lb_match else None)
            verdict = (
                "warning" if wanted_kg is None or kg is None or lb is None or drawing is None
                else "pass" if abs(kg - wanted_kg) <= 1 and abs(lb - wanted_lb) <= 5
                else "fail"
            )
            source_verdicts.append(verdict)
            values[label] = (kg, lb)
            if drawing:
                drawing_evidence.append({
                    **drawing,
                    "type": drawing.get("type", "pdf"),
                    "side": "drawing",
                    "checkpoint": checkpoint,
                    "checkpoint_verdict": verdict,
                })
        verdict = (
            "fail" if "fail" in source_verdicts
            else "warning" if "warning" in source_verdicts
            else "pass"
        )
        verdicts.append(verdict)
        evidence.append({
            "type": "doc",
            "side": "manual",
            "checkpoint": checkpoint,
            "checkpoint_verdict": verdict,
            "text": source_text.get(manual_key) or f"说明书未提取到{checkpoint}",
        })
        evidence.extend(drawing_evidence)
        marking_kg, _ = values["商标图"]
        general_kg, _ = values["总图"]
        details.append(
            f"{checkpoint}：说明书 {wanted_kg:,.0f} kg，"
            f"商标图 {marking_kg:,.0f} kg，总图 {general_kg:,.0f} kg"
            if None not in (wanted_kg, marking_kg, general_kg)
            else f"{checkpoint}：三方数值或定位不完整"
        )

    outcome.verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    outcome.conclusion = "；".join(details)
    outcome.evidence = evidence
    return outcome


def _adjudicate_tm06(outcome: VLMOutcome, manual) -> VLMOutcome:  # noqa: ANN001
    expected = manual.extract_testing_values() if manual else {}
    if not expected:
        return outcome

    raw = outcome.vlm_raw or {}
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    plate = facts.get("csc_plate_rect") if isinstance(facts.get("csc_plate_rect"), dict) else {}
    raw_evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []

    def number(value) -> float | None:  # noqa: ANN001
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def inside(inner) -> bool:  # noqa: ANN001
        if not isinstance(inner, dict) or not plate:
            return False
        try:
            px, py, pw, ph = (float(plate[key]) for key in ("x", "y", "w", "h"))
            x, y, w, h = (float(inner[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return False
        margin = 0.005
        return (
            pw > 0 and ph > 0 and w > 0 and h > 0
            and x >= px - margin and y >= py - margin
            and x + w <= px + pw + margin
            and y + h <= py + ph + margin
        )

    checks = (
        (
            "允许堆码载荷（1.8g）",
            "allowable_stacking_load_1_8g_kg",
            expected["allowable_stacking_load_1_8g_kg"],
            "TM-06 CSC allowable_stacking_load_1_8g:",
            "说明书 9.3：97,200 kg/post × 4 ÷ 1.8 = 216,000 kg",
            "kg",
        ),
        (
            "横向刚性试验力",
            "transverse_racking_test_force_n",
            expected["transverse_racking_test_force_n"],
            "TM-06 CSC transverse_racking_test_force:",
            "说明书 9.3：横向刚性试验力 15,240 kg（150 kN）= 150,000 N",
            "N",
        ),
    )
    evidence: list[dict] = []
    verdicts: list[str] = []
    details: list[str] = []
    plate_ok = str(facts.get("csc_plate_title") or "").strip().upper() == "CSC SAFETY APPROVAL"
    for checkpoint, field, wanted, prefix, basis, unit in checks:
        observed = number(facts.get(field))
        raw_item = next(
            (item for item in raw_evidence if str(item.get("text") or "").startswith(prefix)),
            None,
        )
        drawing = next(
            (item for item in outcome.evidence if str(item.get("text") or "").startswith(prefix)),
            None,
        )
        located = bool(plate_ok and raw_item and drawing and inside(raw_item.get("rect")))
        verdict = (
            "warning" if observed is None or not located
            else "pass" if abs(observed - wanted) <= 1
            else "fail"
        )
        verdicts.append(verdict)
        evidence.append({
            "type": "doc",
            "side": "manual",
            "checkpoint": checkpoint,
            "checkpoint_verdict": verdict,
            "text": basis,
        })
        if drawing:
            evidence.append({
                **drawing,
                "side": "drawing",
                "checkpoint": checkpoint,
                "checkpoint_verdict": verdict,
            })
        actual = "未可靠提取" if observed is None or not located else f"{observed:,.0f}"
        details.append(f"{checkpoint}：基准 {wanted:,.0f} {unit}，铭牌 {actual} {unit}")

    outcome.verdict = "fail" if "fail" in verdicts else "warning" if "warning" in verdicts else "pass"
    outcome.conclusion = "；".join(details)
    outcome.evidence = evidence
    return outcome


@dataclass
class Job:
    project_id: str
    rule_key: str | None = None  # None => full run


class Runner:
    def __init__(self, vlm_client: VLMClient | None = None) -> None:
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.vlm = vlm_client or VLMClient(settings)
        self.trademark_vlm = (
            vlm_client
            if vlm_client is not None
            else CodexCLIClient(settings)
            if settings.trademark_vlm_provider == "codex_cli"
            else self.vlm
        )
        self.fact_layer = FactLayer(ManualFactExtractor(self.vlm))
        self._loop_task: asyncio.Task | None = None

    def set_api_key(self, api_key: str) -> None:
        self.vlm.set_api_key(api_key)
        if self.trademark_vlm is not self.vlm:
            self.trademark_vlm.set_api_key(api_key)

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job.rule_key is None:
                    await self.run_project(job.project_id)
                else:
                    await self.run_single(job.project_id, job.rule_key)
            except Exception as exc:  # noqa: BLE001
                logger.exception("job failed project=%s rule=%s", job.project_id, job.rule_key)
                self._mark_failed(job.project_id, str(exc)[:300])
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------ run

    async def run_project(self, project_id: str) -> None:
        ctx = await asyncio.to_thread(self._build_ctx, project_id)
        if ctx is None:
            self._mark_failed(project_id, "任务上下文构建失败（文件缺失）")
            return
        await self._enrich_facts(ctx)
        man01_rules = [r for r in RULES if r.rule_key == "MAN-01"]
        engine_rules = [r for r in RULES if r.source != "vlm" and r.rule_key != "MAN-01"]
        vlm_rules = [r for r in RULES if r.source == "vlm"]

        for rule in engine_rules:
            outcome = await asyncio.to_thread(run_rule, ctx, rule)
            await asyncio.to_thread(self._persist_engine, project_id, rule, outcome)

        await asyncio.gather(*(self._run_vlm_rule(project_id, rule, ctx) for rule in vlm_rules))

        for rule in man01_rules:
            outcome = await asyncio.to_thread(run_rule, ctx, rule)
            await asyncio.to_thread(self._persist_engine, project_id, rule, outcome)

        self._finish(project_id)

    async def run_single(self, project_id: str, rule_key: str) -> None:
        from app.services.rule_engine import rule_by_key

        ctx = await asyncio.to_thread(self._build_ctx, project_id)
        if ctx is None:
            self._mark_failed(project_id, "任务上下文构建失败")
            return
        rule = rule_by_key(rule_key)
        if rule is None:
            self._mark_rule_error(project_id, rule_key, f"unknown rule {rule_key}")
            return
        session = SessionLocal()
        try:
            row = session.query(RuleResult).filter_by(project_id=project_id, rule_key=rule_key).one()
            row.status = "running"
            row.started_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
        if rule.rule_key in {
            "MAN-03", "MAN-04", "MAN-05", "MAN-06", "MAN-07", "MAN-08",
            "TOT-04", "DOOR-03", "TM-02",
        }:
            await self._enrich_facts(ctx)
        if rule.rule_key == "MAN-01":
            ctx.trademark_facts = await asyncio.to_thread(
                self._load_trademark_facts, project_id
            )
        if rule.source == "vlm":
            await self._run_vlm_rule(project_id, rule, ctx)
        else:
            outcome = await asyncio.to_thread(run_rule, ctx, rule)
            await asyncio.to_thread(self._persist_engine, project_id, rule, outcome)
        if rule.rule_key == "TM-01":
            man01 = rule_by_key("MAN-01")
            if man01 is not None:
                ctx.trademark_facts = await asyncio.to_thread(
                    self._load_trademark_facts, project_id
                )
                outcome = await asyncio.to_thread(run_rule, ctx, man01)
                await asyncio.to_thread(self._persist_engine, project_id, man01, outcome)

    # --------------------------------------------------------------- vlm

    async def _enrich_facts(self, ctx) -> None:  # noqa: ANN001
        if ctx.fact_bundle is not None:
            return
        ctx.fact_bundle = await self.fact_layer.extract(ctx)
        ctx.manual_facts = ctx.fact_bundle.manual_facts

    async def _run_vlm_rule(self, project_id: str, rule, ctx) -> None:  # noqa: ANN001
        files, missing = self._collect_vlm_files(rule, ctx)
        anchor = resolve_doc_anchor(ctx, (rule.params or {}).get("doc_anchor", ""))
        missing_note = _missing_baseline_note(rule, missing, ctx, anchor)
        if not files or missing_note:
            outcome = VLMOutcome(
                verdict="warning",
                status="done",
                conclusion=missing_note or f"目标文件缺失，无法审核（{rule.rule_key}），需人工复核",
            )
            await asyncio.to_thread(self._persist_vlm, project_id, rule, outcome)
            return
        outcome = await self.trademark_vlm.call_files(
            prompt_id=(rule.params or {}).get("prompt_id", ""),
            rule_title=rule.title,
            files=files,
            doc_anchor=anchor,
        )
        if rule.rule_key == "TM-01":
            outcome = _adjudicate_tm01(outcome)
        if rule.rule_key == "TM-04":
            outcome = _adjudicate_tm04(outcome, ctx.manual)
        if rule.rule_key == "TM-06":
            outcome = _adjudicate_tm06(outcome, ctx.manual)
        if rule.rule_key == "TM-01" and outcome.vlm_raw:
            ctx.trademark_facts = dict(outcome.vlm_raw.get("facts") or {})
        await asyncio.to_thread(self._persist_vlm, project_id, rule, outcome)

    @staticmethod
    def _collect_vlm_files(rule, ctx) -> tuple[list[dict], list[str]]:  # noqa: ANN001
        """Gather every image dependency declared in rule.target_files.

        "manual" is a docx and never enters the image list (its facts are injected
        as doc_anchor text). Returns (files, missing) — missing keeps declared
        targets that are absent from the project.
        """
        if rule.rule_key == "TM-01":
            missing = [
                target for target in ("trademark", "000A22G1G")
                if not ctx.pdf_paths.get(target)
            ]
            if missing:
                return [], missing
            files: list[dict] = []
            for view, crop in _TM01_VIEW_CROPS.items():
                for target, label in (("000A22G1G", "总图PDF"), ("trademark", "商标图PDF")):
                    files.append({
                        "label": label,
                        "path": ctx.pdf_paths[target],
                        "pages": [1],
                        "file_id": ctx.pdf_file_ids.get(target, ""),
                        "crop": crop,
                        "zoom": _TM01_RENDER_ZOOM,
                        "view": view,
                    })
            files.append({
                "label": "商标图PDF",
                "path": ctx.pdf_paths["trademark"],
                "pages": [1],
                "file_id": ctx.pdf_file_ids.get("trademark", ""),
                "crop": _TM01_TITLE_CROP,
                "zoom": _TM01_RENDER_ZOOM,
                "view": "title_block",
            })
            return files, []

        files: list[dict] = []
        missing: list[str] = []
        for target in rule.target_files:
            if target == "manual":
                continue
            if target == "trademark":
                label = "商标图"
            elif target.startswith("000A22G1"):
                label = f"总图 {target}"
            else:
                continue
            path = ctx.pdf_paths.get(target, "")
            if path:
                files.append(
                    {
                        "label": label,
                        "path": path,
                        "pages": [1],
                        "file_id": ctx.pdf_file_ids.get(target, ""),
                    }
                )
            else:
                missing.append(target)
        return files, missing

    # ------------------------------------------------------------- persist

    def _build_ctx(self, project_id: str):
        session = SessionLocal()
        try:
            project = session.get(Project, project_id)
            if project is None:
                return None
            return build_context(session, project)
        finally:
            session.close()

    @staticmethod
    def _load_trademark_facts(project_id: str) -> dict:
        session = SessionLocal()
        try:
            row = (
                session.query(RuleResult)
                .filter_by(project_id=project_id, rule_key="TM-01")
                .one_or_none()
            )
            return dict((row.vlm_raw or {}).get("facts") or {}) if row else {}
        finally:
            session.close()

    def _persist_engine(self, project_id: str, rule, outcome) -> None:  # noqa: ANN001
        session = SessionLocal()
        try:
            row = (
                session.query(RuleResult)
                .filter_by(project_id=project_id, rule_key=rule.rule_key)
                .one()
            )
            row.verdict = outcome.verdict
            row.status = "done"
            row.evidence = outcome.evidence
            row.conclusion = outcome.conclusion
            row.error = outcome.error
            if rule.rule_key == "TM-01":
                row.vlm_raw = None
                row.tokens_in = None
                row.tokens_out = None
                row.cost_cny = None
                row.latency_ms = None
            row.started_at = datetime.now(timezone.utc)
            row.finished_at = datetime.now(timezone.utc)
            task_service.push_event(
                session, project_id, rule.rule_key, "rule_done",
                {"rule_key": rule.rule_key, "verdict": outcome.verdict, "engine": "rule"},
            )
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("persist engine failed %s", rule.rule_key)
        finally:
            session.close()

    def _persist_vlm(self, project_id: str, rule, outcome) -> None:  # noqa: ANN001
        session = SessionLocal()
        try:
            row = (
                session.query(RuleResult)
                .filter_by(project_id=project_id, rule_key=rule.rule_key)
                .one()
            )
            row.verdict = outcome.verdict
            row.status = "done"
            row.evidence = outcome.evidence
            raw = dict(outcome.vlm_raw or {})
            if outcome.vlm_attempts:
                raw["_meta"] = {
                    **(raw.get("_meta") or {}),
                    "vlm_attempts": outcome.vlm_attempts,
                }
            row.vlm_raw = raw or None
            row.conclusion = outcome.conclusion
            row.error = outcome.error
            row.tokens_in = outcome.tokens_in
            row.tokens_out = outcome.tokens_out
            row.cost_cny = outcome.cost_cny
            row.latency_ms = outcome.latency_ms
            row.finished_at = datetime.now(timezone.utc)
            task_service.push_event(
                session, project_id, rule.rule_key, "rule_done",
                {"rule_key": rule.rule_key, "verdict": outcome.verdict, "engine": "vlm"},
            )
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("persist vlm failed %s", rule.rule_key)
        finally:
            session.close()

    def _finish(self, project_id: str) -> None:
        session = SessionLocal()
        try:
            task_service.finish_project(session, project_id, "completed")
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("finish failed %s", project_id)
        finally:
            session.close()

    def _mark_failed(self, project_id: str, reason: str) -> None:
        session = SessionLocal()
        try:
            task_service.finish_project(session, project_id, "failed", reason)
            session.commit()
        finally:
            session.close()

    def _mark_rule_error(self, project_id: str, rule_key: str, error: str) -> None:
        session = SessionLocal()
        try:
            row = (
                session.query(RuleResult)
                .filter_by(project_id=project_id, rule_key=rule_key)
                .one_or_none()
            )
            if row:
                row.verdict = "error"
                row.status = "done"
                row.error = error
                row.finished_at = datetime.now(timezone.utc)
                task_service.push_event(
                    session, project_id, rule_key, "rule_done",
                    {"rule_key": rule_key, "verdict": "error", "engine": "rule"},
                )
            session.commit()
        finally:
            session.close()
