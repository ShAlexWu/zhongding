"""Per-rule handlers for the 34 rule-engine items (source != vlm).

Each handler receives a ProjectContext and returns a RuleOutcome.
Handlers prefer pass-with-evidence when data is consistent, warning when
evidence is missing (on_missing semantics, SPEC AC-08), fail on mismatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Numbers with optional thousands separators: 6058 / 6,058 / 33.2 / 4.0
_NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_MM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
_RAL_RE = re.compile(r"RAL\s*(\d{4})", re.IGNORECASE)
_COLOR_WORDS = ["白", "黑", "灰", "蓝", "红", "绿", "黄", "橙", "紫", "棕", "银", "金"]


@dataclass
class RuleOutcome:
    verdict: str = "warning"  # pass/fail/warning/skipped/error
    conclusion: str = ""
    evidence: list[dict] = field(default_factory=list)
    error: str | None = None


def _norm_version(value: str) -> str:
    return re.sub(r"[\s\-/]", "", value).upper()


def _find_comps(ctx, code: str, *keywords: str) -> list[dict]:
    comps = ctx.spatial.get(code, [])
    return [c for c in comps if any(k in c.get("name", "") for k in keywords)]


def _thickness_set(comps: list[dict]) -> set[float]:
    out: set[float] = set()
    for comp in comps:
        for t in comp.get("thicknesses", []) or []:
            value = t.get("thicknessMm") if isinstance(t, dict) else t
            try:
                out.add(round(float(value), 2))
            except (TypeError, ValueError):
                continue
    return out


def _translation(comp: dict) -> tuple[float | None, float | None, float | None]:
    t = comp.get("transform_to_root")
    if not t:
        return None, None, None
    if isinstance(t, dict):
        tx = t.get("translationX", t.get("x"))
        ty = t.get("translationY", t.get("y"))
        tz = t.get("translationZ", t.get("z"))
        return _num(tx), _num(ty), _num(tz)
    if isinstance(t, list) and len(t) >= 3:
        return _num(t[0]), _num(t[1]), _num(t[2])
    return None, None, None


def _num(value) -> float | None:  # noqa: ANN001
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
