"""Hybrid fact extraction: AI parses text; project data supplies auditable facts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.core.logging import get_logger

logger = get_logger(__name__)

_SPLIT_RE = re.compile(r"[\n；;。]+|(?<!\d)[,，]|[,，](?!\d)")
_MM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
_RAL_RE = re.compile(r"RAL\s*(\d{4})", re.IGNORECASE)
_INSTANCE_RE = re.compile(r"-\d+$")
_PART_RE = re.compile(r"^([A-Z]\d{6})[_-]?(.*)$", re.IGNORECASE)


@dataclass
class RequirementFact:
    id: str
    source: str
    source_text: str
    object: str = ""
    attribute: str = ""
    expected_value: str | float | None = None
    unit: str = ""
    rule_keys: list[str] = field(default_factory=lambda: ["MAN-03"])


@dataclass
class EntityResolution:
    requirement_id: str
    status: str = "unresolved"
    entity_id: str = ""
    label: str = ""
    score: float = 0.0
    candidate_labels: list[str] = field(default_factory=list)


@dataclass
class ObservedFact:
    requirement_id: str
    entity_id: str
    attribute: str
    value: float | str
    unit: str = ""
    source: str = "spatial"


@dataclass
class FactIssue:
    requirement_id: str
    kind: str
    message: str


@dataclass
class FactBundle:
    requirements: list[RequirementFact] = field(default_factory=list)
    resolutions: list[EntityResolution] = field(default_factory=list)
    observations: list[ObservedFact] = field(default_factory=list)
    issues: list[FactIssue] = field(default_factory=list)
    manual_facts: dict = field(default_factory=dict)

    def for_rule(self, rule_key: str) -> list[RequirementFact]:
        return [item for item in self.requirements if rule_key in item.rule_keys]

    def resolution_for(self, requirement_id: str) -> EntityResolution | None:
        return next(
            (item for item in self.resolutions if item.requirement_id == requirement_id),
            None,
        )

    def observations_for(self, requirement_id: str, attribute: str) -> list[ObservedFact]:
        return [
            item
            for item in self.observations
            if item.requirement_id == requirement_id and item.attribute == attribute
        ]

    @property
    def customer_text(self) -> str:
        return "；".join(item.source_text for item in self.requirements)


class FactLayer:
    """The single extraction entry point. It never returns an audit verdict."""

    def __init__(self, manual_extractor) -> None:  # noqa: ANN001
        self.manual_extractor = manual_extractor

    async def extract(self, ctx) -> FactBundle:  # noqa: ANN001
        requirements = _input_requirements(ctx.inputs)
        bundle = FactBundle(requirements=requirements)
        if ctx.manual:
            try:
                bundle.manual_facts = await self.manual_extractor.extract(ctx.manual, ctx.inputs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("manual fact extraction failed: %s", exc)
                bundle.issues.append(FactIssue("manual", "extraction_failed", str(exc)[:200]))

        _merge_ai_requirements(bundle)
        _complete_change_check_coverage(bundle)
        catalog = _entity_catalog(ctx.spatial)
        for requirement in bundle.requirements:
            if not requirement.object:
                if requirement.attribute not in {"color", "guarantee", "trademark", "contact"}:
                    bundle.issues.append(
                        FactIssue(requirement.id, "unmapped", "未提取到可映射的审核对象")
                    )
                continue
            resolution, components = _resolve_entity(requirement, catalog)
            bundle.resolutions.append(resolution)
            if resolution.status != "resolved":
                bundle.issues.append(
                    FactIssue(requirement.id, resolution.status, "对象无法唯一映射到项目组件")
                )
                continue
            if requirement.attribute == "thickness":
                for value in _thicknesses(components):
                    bundle.observations.append(
                        ObservedFact(requirement.id, resolution.entity_id, "thickness", value, "mm")
                    )
        return bundle


def _input_requirements(inputs: dict) -> list[RequirementFact]:
    requirements: list[RequirementFact] = []
    for source in ("tech_req", "new_material"):
        for text in _split(str(inputs.get(source) or "")):
            requirement = RequirementFact(
                id=f"{source}:{len(requirements) + 1}",
                source=source,
                source_text=text,
            )
            _fallback_parse(requirement)
            requirements.append(requirement)
    return requirements


def _split(text: str) -> list[str]:
    return [part.strip() for part in _SPLIT_RE.split(text) if part.strip()]


def _fallback_parse(requirement: RequirementFact) -> None:
    text = requirement.source_text
    mm = _MM_RE.search(text)
    if mm and re.search(r"板厚|厚度|厚\b|thickness", text, re.IGNORECASE):
        requirement.attribute = "thickness"
        requirement.expected_value = float(mm.group(1))
        requirement.unit = "mm"
        requirement.object = _object_before(text, r"板厚|厚度|厚\b|thickness")
        requirement.rule_keys.append("TOT-04")
    if _RAL_RE.search(text) or re.search(r"外(?:面|部)?漆|内(?:面|部)?漆|颜色|color", text, re.IGNORECASE):
        requirement.attribute = requirement.attribute or "color"
        requirement.rule_keys.extend(["MAN-06", "DOOR-03", "TM-02"])
    if re.search(r"商标|logo", text, re.IGNORECASE):
        requirement.attribute = requirement.attribute or "trademark"
        requirement.rule_keys.extend(["MAN-07", "TM-03"])
    if re.search(r"公司|客户名称|地址|联系人|电话|邮箱", text, re.IGNORECASE):
        requirement.attribute = requirement.attribute or "contact"
        requirement.rule_keys.extend(["MAN-05", "TM-05"])
    if re.search(r"质保|保修|保证期|guarantee|warranty", text, re.IGNORECASE):
        requirement.attribute = requirement.attribute or "guarantee"
        requirement.rule_keys.append("MAN-08")
    requirement.rule_keys = list(dict.fromkeys(requirement.rule_keys))


def _object_before(text: str, marker: str) -> str:
    value = re.split(marker, text, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.sub(r"^(?:我想要|要求|客户要求|请将|把)", "", value).strip(" ：:，,的")
    return value


def _merge_ai_requirements(bundle: FactBundle) -> None:
    ai_items = bundle.manual_facts.get("requirement_facts") or []
    for requirement in bundle.requirements:
        match = next(
            (
                item
                for item in ai_items
                if _same_fragment(requirement.source_text, str(item.get("source_text") or ""))
            ),
            None,
        )
        if not match:
            continue
        requirement.object = str(match.get("object") or requirement.object).strip()
        requirement.attribute = str(match.get("attribute") or requirement.attribute).strip()
        requirement.expected_value = match.get("expected_value", requirement.expected_value)
        requirement.unit = str(match.get("unit") or requirement.unit).strip()
        requirement.rule_keys = list(
            dict.fromkeys(requirement.rule_keys + _route(requirement.attribute, requirement.source_text))
        )


def _complete_change_check_coverage(bundle: FactBundle) -> None:
    checks = list(bundle.manual_facts.get("change_checks") or [])
    completed: list[dict] = []
    for requirement in bundle.requirements:
        match = next(
            (
                item
                for item in checks
                if _same_fragment(
                    requirement.source_text,
                    str(item.get("source_text") or item.get("requirement") or ""),
                )
            ),
            None,
        )
        if match:
            completed.append({**match, "source_text": requirement.source_text})
        else:
            completed.append(
                {
                    "source_text": requirement.source_text,
                    "requirement": requirement.source_text,
                    "status": "uncertain",
                    "evidence": "",
                }
            )
            bundle.issues.append(
                FactIssue(requirement.id, "extraction_gap", "AI 未返回该条客户要求")
            )
    bundle.manual_facts["change_checks"] = completed


def _route(attribute: str, text: str) -> list[str]:
    probe = f"{attribute} {text}".lower()
    rules = ["MAN-03"]
    if attribute == "thickness" or "板厚" in probe or "厚度" in probe:
        rules.append("TOT-04")
    if attribute == "color" or "颜色" in probe or "paint" in probe:
        rules.extend(["MAN-06", "DOOR-03", "TM-02"])
    if attribute == "trademark" or "商标" in probe or "logo" in probe:
        rules.extend(["MAN-07", "TM-03"])
    if attribute == "contact" or any(word in probe for word in ("客户名称", "地址", "联系人")):
        rules.extend(["MAN-05", "TM-05"])
    if attribute == "guarantee" or "质保" in probe:
        rules.append("MAN-08")
    return rules


def _same_fragment(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    return bool(a and a == b)


def _norm(value: str) -> str:
    return re.sub(r"[\W_]", "", value, flags=re.UNICODE).lower()


def _entity_catalog(spatial: dict[str, list[dict]]) -> list[tuple[str, str, list[dict]]]:
    grouped: dict[str, tuple[str, list[dict]]] = {}
    for code, components in spatial.items():
        for component in components:
            if not component.get("leaf"):
                continue
            base = _INSTANCE_RE.sub("", str(component.get("name") or "").rsplit("/", 1)[-1])
            match = _PART_RE.match(base)
            part_code, label = (match.group(1).upper(), match.group(2)) if match else (base, base)
            # The same part is repeated in assembly and detail JSON.  Its drawing
            # number is the stable identity; only nameless/code-less items remain
            # scoped to their source drawing.
            entity_id = part_code if match else f"{code}:{base}"
            display = f"{part_code}_{label}" if label else part_code
            grouped.setdefault(entity_id, (display, []))[1].append(component)
    return [(entity_id, label, components) for entity_id, (label, components) in grouped.items()]


def _resolve_entity(
    requirement: RequirementFact,
    catalog: list[tuple[str, str, list[dict]]],
) -> tuple[EntityResolution, list[dict]]:
    target = _norm(requirement.object)
    scored: list[tuple[float, str, str, list[dict]]] = []
    for entity_id, label, components in catalog:
        label_match = _PART_RE.match(label)
        candidate_label = label_match.group(2) if label_match else label
        candidate = _norm(re.sub(r"^\d+(?=[^\d])", "", candidate_label))
        if not candidate:
            continue
        if target == candidate:
            score = 1.0
        elif target in candidate or candidate in target:
            score = 0.96
        else:
            score = SequenceMatcher(None, target, candidate).ratio()
        if score >= 0.55:
            scored.append((score, entity_id, label, components))
    scored.sort(reverse=True, key=lambda item: item[0])
    labels = [item[2] for item in scored[:5]]
    if not scored or scored[0][0] < 0.65:
        return EntityResolution(requirement.id, candidate_labels=labels), []
    top = scored[0]
    if top[0] < 1.0 and len(scored) > 1 and top[0] - scored[1][0] < 0.12:
        return EntityResolution(
            requirement.id,
            status="ambiguous",
            score=round(top[0], 3),
            candidate_labels=labels,
        ), []
    return EntityResolution(
        requirement.id,
        status="resolved",
        entity_id=top[1],
        label=top[2],
        score=round(top[0], 3),
        candidate_labels=labels,
    ), top[3]


def _thicknesses(components: list[dict]) -> list[float]:
    values: set[float] = set()
    for component in components:
        for item in component.get("thicknesses") or []:
            value = item.get("thicknessMm") if isinstance(item, dict) else item
            try:
                values.add(round(float(value), 3))
            except (TypeError, ValueError):
                continue
    return sorted(values)
