"""Rule engine: loads rules_40.json DSL, builds project context, dispatches.

- source == vlm          -> handled by workers via VLMClient
- source == doc_compare/json_field/spatial -> dispatched to handlers.py
- on_missing downgrade   -> SPEC AC-08 (warning) / AC-07 (error)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import ijson
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models import ApiProp, Project, ProjectFile, SpatialComponent
from app.services.doc_parser import (
    ManualDoc,
    extract_drawing_facts,
    extract_manual,
    extract_pdf_texts,
)
from app.services.fact_layer import FactBundle
from app.services.handlers import HANDLERS, RuleOutcome
from app.services.standards_registry import OfficialStandardsRegistry

logger = get_logger(__name__)

VALID_SOURCES = {"json_field", "spatial", "vlm", "doc_compare"}
_ON_MISSING_MAP = {"warning": "warning", "error": "error", "pass": "pass"}


@dataclass
class RuleDef:
    rule_key: str
    domain: str
    title: str
    source: str
    priority: str
    is_starred: bool
    selector: str = ""
    op: str = ""
    params: dict = field(default_factory=dict)
    evidence_path: str = ""
    on_missing: str = "warning"
    target_files: list[str] = field(default_factory=list)


@dataclass
class ProjectContext:
    project_id: str
    inputs: dict = field(default_factory=dict)
    manual: ManualDoc | None = None
    manual_path: str | None = None
    manual_facts: dict = field(default_factory=dict)
    fact_bundle: FactBundle | None = None
    trademark_facts: dict = field(default_factory=dict)
    standards_registry: OfficialStandardsRegistry | None = None
    json_props: dict[str, dict[str, str]] = field(default_factory=dict)
    json_file_ids: dict[str, str] = field(default_factory=dict)
    api_paths: dict[str, str] = field(default_factory=dict)
    pdf_file_ids: dict[str, str] = field(default_factory=dict)
    spatial_paths: dict[str, str] = field(default_factory=dict)
    spatial: dict[str, list[dict]] = field(default_factory=dict)
    _pdf_texts: dict[str, list[str]] = field(default_factory=dict)
    _drawing_facts: dict[str, list[dict]] = field(default_factory=dict)
    _pair_relations: dict[str, list[dict]] = field(default_factory=dict)

    def get_pdf_texts(self, code: str) -> list[str]:
        if code not in self._pdf_texts:
            path = self._pdf_path_for(code)
            self._pdf_texts[code] = extract_pdf_texts(path) if path else []
        return self._pdf_texts[code]

    def _pdf_path_for(self, code: str) -> str:
        # path resolution is injected via ctx.pdf_paths at build time
        return self.pdf_paths.get(code, "")

    def get_drawing_facts(self, code: str) -> list[dict]:
        if code not in self._drawing_facts:
            path = self.api_paths.get(code, "")
            self._drawing_facts[code] = extract_drawing_facts(path) if path else []
        return self._drawing_facts[code]

    def get_pair_relations(self, code: str) -> list[dict]:
        if code not in self._pair_relations:
            path = self.spatial_paths.get(code, "")
            if not path:
                self._pair_relations[code] = []
            else:
                with open(path, "rb") as handle:
                    self._pair_relations[code] = list(ijson.items(handle, "pairRelations.item"))
        return self._pair_relations[code]

    pdf_paths: dict[str, str] = field(default_factory=dict)


def load_rules(path: Path | None = None) -> list[RuleDef]:
    path = path or settings.rules_path
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rules: list[RuleDef] = []
    for raw in payload.get("rules", []):
        source = raw.get("source", "")
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source {source} in {raw.get('rule_key')}")
        rules.append(
            RuleDef(
                rule_key=raw["rule_key"],
                domain=raw["domain"],
                title=raw["title"],
                source=source,
                priority=raw.get("priority", "P1"),
                is_starred=bool(raw.get("is_starred", False)),
                selector=raw.get("selector", ""),
                op=raw.get("op", ""),
                params=raw.get("params", {}) or {},
                evidence_path=raw.get("evidence_path", ""),
                on_missing=raw.get("on_missing", "warning"),
                target_files=raw.get("target_files", []) or [],
            )
        )
    return rules


RULES: list[RuleDef] = load_rules()


def rule_by_key(rule_key: str) -> RuleDef | None:
    return next((r for r in RULES if r.rule_key == rule_key), None)


def build_context(session: Session, project: Project) -> ProjectContext:
    ctx = ProjectContext(project_id=project.id)
    ctx.inputs = project.customer_inputs or {}
    ctx.standards_registry = OfficialStandardsRegistry(
        live=settings.standards_live_lookup,
        timeout_s=settings.standards_timeout_s,
    )
    files = session.query(ProjectFile).filter_by(project_id=project.id).all()

    for row in files:
        if row.kind == "docx":
            ctx.manual_path = row.path
        elif row.kind in ("pdf", "trademark"):
            ctx.pdf_file_ids[row.code] = row.id
            ctx.pdf_paths[row.code] = row.path
        elif row.kind == "api_json":
            ctx.json_file_ids[row.code] = row.id
            ctx.api_paths[row.code] = row.path
        elif row.kind == "spatial":
            ctx.spatial_paths[row.code] = row.path
            ctx.spatial[row.code] = []

    if ctx.manual_path:
        try:
            ctx.manual = extract_manual(ctx.manual_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("manual extract failed: %s", exc)

    for prop in session.query(ApiProp).filter_by(project_id=project.id).all():
        if prop.prop_name.startswith("@"):
            continue
        ctx.json_props.setdefault(prop.code, {})[prop.prop_name] = prop.resolved_value

    for comp in session.query(SpatialComponent).filter_by(project_id=project.id).all():
        ctx.spatial.setdefault(comp.code, []).append(
            {
                "id": comp.comp_id,
                "name": comp.name,
                "parent_id": comp.parent_id,
                "leaf": comp.leaf,
                "material_name": comp.material_name,
                "thicknesses": comp.thicknesses or [],
                "transform_to_root": comp.transform_to_root,
                "bounds_mm": comp.bounds_mm,
            }
        )
    return ctx


def run_rule(ctx: ProjectContext, rule: RuleDef) -> RuleOutcome:
    """Execute one engine rule; generic fallback applies on_missing downgrade."""
    handler = HANDLERS.get(rule.rule_key)
    if handler is None:
        return _generic_engine_rule(ctx, rule)
    try:
        outcome = handler(ctx, rule)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rule %s crashed", rule.rule_key)
        return RuleOutcome("error", "规则引擎异常", [], error=str(exc)[:300])
    drawing_targets = [target for target in rule.target_files if target not in ("manual", "trademark")]
    if "manual" in rule.target_files and drawing_targets and outcome.verdict in ("pass", "fail"):
        sides = {item.get("side") for item in outcome.evidence}
        if not {"manual", "drawing"} <= sides:
            return RuleOutcome(
                "warning",
                f"缺少说明书/图纸双侧证据，无法完成对比；原判定：{outcome.conclusion}",
                outcome.evidence,
            )
    return outcome


def _generic_engine_rule(ctx: ProjectContext, rule: RuleDef) -> RuleOutcome:
    """No dedicated handler: verify selector data presence, downgrade on missing."""
    codes = re.findall(r"\b(000A22G1[A-Z]|manual|trademark)\b", rule.selector)
    present = 0
    evidence: list[dict] = []
    for code in set(codes):
        if code == "manual":
            present += 1 if ctx.manual else 0
        elif code in ctx.spatial:
            present += len(ctx.spatial[code]) > 0
        elif code in ctx.json_props:
            present += 1
    if present > 0:
        return RuleOutcome("pass", f"取数成功（命中 {present} 个数据源）", evidence)
    verdict = _ON_MISSING_MAP.get(rule.on_missing, "warning")
    return RuleOutcome(
        verdict,
        f"取数失败（on_missing={rule.on_missing}），已降级",
        evidence,
    )


def resolve_doc_anchor(ctx: ProjectContext, anchor_expr: str) -> str:
    """Inject rule-engine extracted baseline values into VLM prompts (doc_anchor)."""
    expr = anchor_expr or ""
    manual = ctx.manual
    if "doc:paint.exterior_color" in expr and manual:
        colors = manual.extract_colors()
        if colors:
            return "外面漆颜色: " + "; ".join(colors[:3])
    if "doc:section.5.5.rating" in expr and manual:
        ratings = manual.extract_ratings()
        if ratings:
            return "净重/总重基准: " + json.dumps(ratings, ensure_ascii=False)
    if "doc:section.9.testing" in expr and manual:
        values = manual.extract_testing_values()
        return "试验基准: " + json.dumps(values, ensure_ascii=False) if values else ""
    if "input:tech_req" in expr:
        customer_text = (
            ctx.fact_bundle.customer_text
            if ctx.fact_bundle
            else "；".join(
                str(ctx.inputs.get(key) or "").strip()
                for key in ("tech_req", "new_material")
                if str(ctx.inputs.get(key) or "").strip()
            )
        )
        return "客户要求: " + customer_text[:2000]
    if "spatial:" in expr and "sheetMetalThicknesses" in expr:
        match = re.search(r"spatial:(\w+)/", expr)
        if match:
            code = match.group(1)
            thickness: set[float] = set()
            for comp in ctx.spatial.get(code, []):
                for t in comp.get("thicknesses", []) or []:
                    try:
                        thickness.add(round(float(t), 2))
                    except (TypeError, ValueError):
                        continue
            if thickness:
                return f"spatial 抽取厚度集合: {sorted(thickness)}"
    return ""
