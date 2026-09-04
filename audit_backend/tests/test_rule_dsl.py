"""Rule DSL loading tests (rules_40.json contract)."""

from __future__ import annotations

from app.services.rule_engine import RULES, rule_by_key


def test_total_40_rules() -> None:
    assert len(RULES) == 40


def test_rule_keys_unique() -> None:
    keys = [r.rule_key for r in RULES]
    assert len(keys) == len(set(keys))


def test_key_pattern() -> None:
    import re

    pattern = re.compile(r"^(MAN|TOT|DOOR|SIDE|FRONT|CHAS|ROOF|TM|GEN)-\d{2}$")
    for rule in RULES:
        assert pattern.match(rule.rule_key), rule.rule_key


def test_source_split_34_6() -> None:
    engine = [r for r in RULES if r.source != "vlm"]
    vlm = [r for r in RULES if r.source == "vlm"]
    assert len(engine) == 34
    assert len(vlm) == 6
    assert all(r.rule_key.startswith("TM-") for r in vlm)
    assert rule_by_key("TM-01") in vlm


def test_domain_distribution() -> None:
    from collections import Counter

    counts = Counter(r.domain for r in RULES)
    assert counts["说明书"] == 8
    assert counts["总图"] == 5
    assert counts["商标图"] == 6
    assert counts["全局通用"] == 3


def test_starred_22() -> None:
    starred = [r for r in RULES if r.is_starred]
    assert len(starred) == 22


def test_rule_required_fields() -> None:
    for rule in RULES:
        assert rule.rule_key
        assert rule.domain
        assert rule.title
        assert rule.priority in ("P0", "P1")
        assert rule.on_missing in ("pass", "warning", "error")
        assert rule.source in ("json_field", "spatial", "vlm", "doc_compare")


def test_vlm_rules_have_prompt_id() -> None:
    for rule in RULES:
        if rule.source == "vlm":
            assert (rule.params or {}).get("prompt_id"), rule.rule_key


def test_rule_by_key_lookup() -> None:
    assert rule_by_key("MAN-01") is not None
    assert rule_by_key("NOPE-99") is None
