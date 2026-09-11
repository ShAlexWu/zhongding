"""Multi-file VLM input regression tests for remaining visual trademark rules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import SessionLocal
from app.main import app
from app.models import Project, ProjectFile, RuleResult
from app.services import task_service
from app.services.rule_engine import build_context, rule_by_key
from app.services.vlm_client import RenderedPage, VLMClient, VLMOutcome
from app.workers.worker import Runner, _adjudicate_tm01

DATA_DIR = Path(os.environ["DATA_DIR"])
TM_PDF = DATA_DIR / "CIMC20GP_26A-00-页面.pdf"
GA_PDF = DATA_DIR / "000A22G1G_总装配_21A-00_api.pdf"
TM01_VIEWS = (
    (1, "general", "door_end"),
    (2, "marking", "door_end"),
    (3, "general", "side"),
    (4, "marking", "side"),
    (5, "general", "front_end"),
    (6, "marking", "front_end"),
    (7, "general", "roof"),
    (8, "marking", "roof"),
)


# ---------------------------------------------------------------- helpers


def _add_file(session, pid: str, file_id: str, kind: str, code: str, path: Path) -> None:  # noqa: ANN001
    session.add(
        ProjectFile(
            id=file_id,
            project_id=pid,
            kind=kind,
            code=code,
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size_bytes=path.stat().st_size,
        )
    )


def _make_project(pid: str, *, tech_req: str, trademark: bool = True, ga: bool = False) -> str:
    session = SessionLocal()
    try:
        session.add(Project(id=pid, name=pid, customer_inputs={"tech_req": tech_req}))
        session.flush()
        task_service.ensure_rule_rows(session, pid)
        if trademark:
            _add_file(session, pid, f"{pid}-tm", "trademark", "trademark", TM_PDF)
        if ga:
            _add_file(session, pid, f"{pid}-ga", "pdf", "000A22G1G", GA_PDF)
        session.commit()
    finally:
        session.close()
    return pid


def _run_rule_row(pid: str, rule_key: str) -> RuleResult:
    session = SessionLocal()
    try:
        project = session.get(Project, pid)
        ctx = build_context(session, project)
        rule = rule_by_key(rule_key)
    finally:
        session.close()
    runner = Runner()
    asyncio.run(runner._run_vlm_rule(pid, rule, ctx))  # noqa: SLF001
    session = SessionLocal()
    try:
        return session.query(RuleResult).filter_by(project_id=pid, rule_key=rule_key).one()
    finally:
        session.close()


# ---------------------------------------------------------------- tests


def test_tm01_is_routed_to_vlm() -> None:
    from app.services.handlers.registry import HANDLERS

    rule = rule_by_key("TM-01")
    assert rule and rule.source == "vlm"
    assert "TM-01" not in HANDLERS


def test_tm01_collects_four_pdf_crop_pairs_and_title_block() -> None:
    rule = rule_by_key("TM-01")
    ctx = SimpleNamespace(
        pdf_paths={"trademark": str(TM_PDF), "000A22G1G": str(GA_PDF)},
        pdf_file_ids={"trademark": "f-tm", "000A22G1G": "f-ga"},
    )

    files, missing = Runner._collect_vlm_files(rule, ctx)  # noqa: SLF001

    assert missing == []
    assert [item["view"] for item in files] == [
        "door_end", "door_end", "side", "side", "front_end", "front_end",
        "roof", "roof", "title_block",
    ]
    assert [item["label"] for item in files[:8]] == ["总图PDF", "商标图PDF"] * 4
    assert all(item["zoom"] == 4.0 and item["crop"] for item in files)


def test_dry_run_mock_evidence_lists_both_files() -> None:
    """Spec test 1: dry-run TM-01 mock evidence names trademark AND assembly."""
    client = VLMClient(settings)
    outcome = asyncio.run(
        client.call_files(
            prompt_id="tm_four_views_vs_ga",
            rule_title="商标图四视图与总图一致性",
            files=[
                {"label": "商标图", "path": str(TM_PDF), "pages": [1], "file_id": "f-tm"},
                {"label": "总图 000A22G1G", "path": str(GA_PDF), "pages": [1], "file_id": "f-ga"},
            ],
        )
    )
    assert outcome.status == "done"
    assert len(outcome.evidence) == 2
    texts = " ".join(ev["text"] for ev in outcome.evidence)
    assert "CIMC20GP_26A-00-页面.pdf" in texts
    assert "000A22G1G_总装配_21A-00_api.pdf" in texts
    assert {ev["file_id"] for ev in outcome.evidence} == {"f-tm", "f-ga"}
    assert outcome.vlm_raw and outcome.vlm_raw.get("mock") is True


def test_tm01_missing_ga_forces_warning_and_review() -> None:
    """Spec test 2: assembly absent -> verdict warning, conclusion asks 人工复核."""
    with TestClient(app):
        pid = _make_project("mfnga", tech_req="外部颜色 RAL 9016", trademark=True, ga=False)
    row = _run_rule_row(pid, "TM-01")
    assert row.verdict == "warning"
    assert "总图 000A22G1G" in (row.conclusion or "")
    assert "缺失" in (row.conclusion or "")
    assert "需人工复核" in (row.conclusion or "")
    assert row.vlm_raw is None  # missing comparison basis: do not spend a model call


def test_tm04_missing_manual_forces_warning_without_model_call() -> None:
    """Three-source comparison cannot run without its manual baseline."""
    with TestClient(app):
        pid = _make_project("mfnmanual", tech_req="外部颜色 RAL 9016", trademark=True, ga=True)
    row = _run_rule_row(pid, "TM-04")
    assert row.verdict == "warning"
    assert row.status == "done"
    assert "说明书" in (row.conclusion or "")
    assert row.vlm_raw is None


def test_tm03_empty_tech_req_forces_warning() -> None:
    """Spec test 3: tech_req empty -> verdict warning + 客户技术要求未提供."""
    with TestClient(app):
        pid = _make_project("mfnreq", tech_req="", trademark=True, ga=False)
    row = _run_rule_row(pid, "TM-03")
    assert row.verdict == "warning"
    assert "客户技术要求未提供" in (row.conclusion or "")
    assert "需人工复核" in (row.conclusion or "")


def test_tm03_with_tech_req_keeps_model_verdict() -> None:
    """Counterpart: non-empty tech_req must not trigger the forced-warning note."""
    with TestClient(app):
        pid = _make_project("mfreq", tech_req="客户 LOGO 印于门板右侧", trademark=True, ga=False)
    row = _run_rule_row(pid, "TM-03")
    assert "客户技术要求未提供" not in (row.conclusion or "")


def test_tm05_ignores_requirements_unrelated_to_customer_identity() -> None:
    class UnexpectedVLM:
        async def call_files(self, **_kwargs) -> VLMOutcome:  # noqa: ANN003
            raise AssertionError("TM-05 must not call the model without a customer name/address baseline")

    with TestClient(app):
        pid = _make_project(
            "tm05scope",
            tech_req=(
                "Fork Pocket Top plate 4.0 mm Thk.; paint suppliers add Gaojing; "
                "self-adhesive film decal guaranteed 9 years"
            ),
        )

    asyncio.run(Runner(vlm_client=UnexpectedVLM()).run_single(pid, "TM-05"))  # type: ignore[arg-type]
    session = SessionLocal()
    try:
        row = session.query(RuleResult).filter_by(project_id=pid, rule_key="TM-05").one()
        assert row.verdict == "warning"
        assert "客户名称/地址基准未提供" in (row.conclusion or "")
        assert row.vlm_raw is None
    finally:
        session.close()


def test_tm05_rejects_model_verdicts_outside_name_and_address() -> None:
    class OffScopeVLM:
        async def call_files(self, **kwargs) -> VLMOutcome:  # noqa: ANN003
            assert "4.0 mm" not in kwargs["doc_anchor"]
            return VLMOutcome(
                verdict="fail",
                conclusion="板材厚度、油漆供应商和质保期未体现",
                vlm_raw={"facts": {"customer_name": "GVCT", "customer_address": "London"}},
            )

    with TestClient(app):
        pid = _make_project(
            "tm05guard",
            tech_req="客户名称 GVCT；客户地址 London；Fork Pocket Top plate 4.0 mm Thk.",
        )

    asyncio.run(Runner(vlm_client=OffScopeVLM()).run_single(pid, "TM-05"))  # type: ignore[arg-type]
    session = SessionLocal()
    try:
        row = session.query(RuleResult).filter_by(project_id=pid, rule_key="TM-05").one()
        assert row.verdict == "warning"
        assert "客户名称" in (row.conclusion or "")
        assert all(word not in (row.conclusion or "") for word in ("板材", "油漆", "质保"))
    finally:
        session.close()


def test_all_dependencies_missing_finishes_as_manual_review() -> None:
    """No files is a terminal manual-review result, never a pending item."""
    with TestClient(app):
        pid = _make_project("mfskip", tech_req="外部颜色 RAL 9016", trademark=False, ga=False)
    row = _run_rule_row(pid, "TM-01")
    assert row.verdict == "warning"
    assert row.status == "done"
    assert "需人工复核" in (row.conclusion or "")


def test_legacy_single_file_call_still_works() -> None:
    """Backward compatibility: old call() signature adapts onto multi-file path."""
    client = VLMClient(settings)
    outcome = asyncio.run(
        client.call(
            prompt_id="gen_sealant_notes",
            rule_title="打胶注释完整性检查",
            pdf_path=str(GA_PDF),
            pages=[1],
            file_id="f-legacy",
        )
    )
    assert outcome.status == "done"
    assert outcome.evidence and outcome.evidence[0]["file_id"] == "f-legacy"


def test_prompts_declare_tm01_crop_order_and_tm04_inputs() -> None:
    from app.services.prompts import build_prompt

    tm01 = build_prompt("tm_four_views_vs_ga", "规则标题", "基准")
    assert "1=总图门端，2=商标图门端" in tm01
    assert "7=总图 SECTION D-D，8=商标图顶视图" in tm01
    assert "9=商标图标题栏" in tm01
    assert "evidence 必须恰好 8 条" in tm01
    assert "rect 必须填 null" in tm01
    assert '"door_end_verdict"' in tm01
    assert '"roof_verdict"' in tm01
    assert '"marking_drawing_number"' in tm01
    assert "不得根据文件名或总图 BOM 补写" in tm01
    tm04 = build_prompt("tm_weight_plate_value", "规则标题", "基准")
    assert "第 1 张为商标图" in tm04
    assert "第 2 张为总图" in tm04
    assert "evidence 必须恰好 6 条" in tm04
    assert '"max_gross_kg"' in tm04


def test_tm01_real_path_sends_rendered_crop_images() -> None:
    raw = {
        "verdict": "pass",
        "conclusion": "四视图一致",
        "evidence": [
            {
                "page": page,
                "rect": None,
                "text": f"TM-01 VIEW {role} {view}: 波形与通风器已识别",
            }
            for page, role, view in TM01_VIEWS
        ],
        "facts": {
            "door_end_verdict": "pass",
            "door_end_conclusion": "门端一致",
            "side_verdict": "pass",
            "side_conclusion": "侧板一致",
            "front_end_verdict": "pass",
            "front_end_conclusion": "前端一致",
            "roof_verdict": "pass",
            "roof_conclusion": "顶板一致",
            "marking_drawing_number": "000A22G1M",
            "marking_drawing_number_evidence": {
                "image_index": 9,
                "rect": {"x": 0.1, "y": 0.4, "w": 0.7, "h": 0.2},
                "text": "000A22G1M",
            },
        },
    }

    class Completions:
        kwargs: dict = {}

        async def create(self, **kwargs):  # noqa: ANN003, ANN202
            self.kwargs = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
            )

    completions = Completions()
    client = VLMClient(replace(settings, vlm_dry_run=False, dashscope_api_key="sk-test"))
    client._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=completions)
    )
    rule = rule_by_key("TM-01")
    files, _ = Runner._collect_vlm_files(  # noqa: SLF001
        rule,
        SimpleNamespace(
            pdf_paths={"trademark": str(TM_PDF), "000A22G1G": str(GA_PDF)},
            pdf_file_ids={"trademark": "f-tm", "000A22G1G": "f-ga"},
        ),
    )
    outcome = asyncio.run(client.call_files(
        "tm_four_views_vs_ga", "商标图四视图与总图一致性", files
    ))
    content = completions.kwargs["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text"] + ["image_url"] * 9
    assert outcome.verdict == "pass"


def _tm01_images() -> list[RenderedPage]:
    files, _ = Runner._collect_vlm_files(  # noqa: SLF001
        rule_by_key("TM-01"),
        SimpleNamespace(
            pdf_paths={"trademark": str(TM_PDF), "000A22G1G": str(GA_PDF)},
            pdf_file_ids={"trademark": "f-tm", "000A22G1G": "f-ga"},
        ),
    )
    images = []
    for seq, item in enumerate(files, 1):
        crop = item["crop"]
        images.append(RenderedPage(
            page=1,
            png_b64="",
            file_id=item["file_id"],
            file_label=item["label"],
            seq=seq,
            pdf_width=1191,
            pdf_height=842,
            crop=(crop["x"], crop["y"], crop["w"], crop["h"]),
        ))
    return images


def test_tm01_pass_requires_all_eight_slice_evidence() -> None:
    client = VLMClient(settings)
    outcome = client._finalize(  # noqa: SLF001
        {
            "verdict": "pass",
            "conclusion": "一致",
            "evidence": [{"page": 2, "rect": None, "text": "TM-01 VIEW marking door_end: 已识别"}],
        },
        _tm01_images(),
        time.monotonic(),
        1,
        prompt_id="tm_four_views_vs_ga",
    )
    assert outcome.verdict == "warning"
    assert "八切片" in outcome.conclusion


def test_tm01_valid_eight_slice_evidence_can_pass() -> None:
    evidence = [
        {
            "page": page,
            "rect": None,
            "text": f"TM-01 VIEW {role} {view}: 已识别波形与通风器",
        }
        for page, role, view in TM01_VIEWS
    ]
    outcome = VLMClient(settings)._finalize(  # noqa: SLF001
        {
            "verdict": "pass",
            "conclusion": "一致",
            "evidence": evidence,
            "facts": {
                "door_end_verdict": "pass",
                "door_end_conclusion": "门端一致",
                "side_verdict": "pass",
                "side_conclusion": "侧板一致",
                "front_end_verdict": "pass",
                "front_end_conclusion": "前端一致",
                "roof_verdict": "pass",
                "roof_conclusion": "顶板一致",
                "marking_drawing_number": "000A22G1M",
                "marking_drawing_number_evidence": {
                    "image_index": 9,
                    "rect": {"x": 0.1, "y": 0.4, "w": 0.7, "h": 0.2},
                    "text": "DWG. NO. 000A22G1M",
                },
            },
        },
        _tm01_images(),
        time.monotonic(),
        1,
        prompt_id="tm_four_views_vs_ga",
    )
    assert outcome.verdict == "pass"
    assert len(outcome.evidence) == 8
    assert {item["file_id"] for item in outcome.evidence} == {"f-ga", "f-tm"}
    first_rect = outcome.evidence[0]["rect"]
    assert {key: round(value, 2) for key, value in first_rect.items()} == {
        "x": 95.28, "y": 580.98, "w": 285.84, "h": 244.18,
    }
    fact = outcome.vlm_raw["facts"]
    assert fact["marking_drawing_number"] == "000A22G1M"
    assert fact["marking_drawing_number_evidence"]["file_id"] == "f-tm"
    assert fact["marking_drawing_number_evidence"]["rect"]["w"] > 1


def test_invalid_image_index_is_not_reassigned_to_a_real_file() -> None:
    outcome = VLMClient(settings)._finalize(  # noqa: SLF001
        {
            "verdict": "pass",
            "conclusion": "一致",
            "evidence": [{"page": 99, "rect": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "text": "伪证据"}],
        },
        _tm01_images(),
        time.monotonic(),
        1,
        prompt_id="tm_four_views_vs_ga",
    )
    assert outcome.verdict == "warning"
    assert outcome.evidence == []
    assert "图片序号无效" in outcome.conclusion


def test_tm01_ignores_model_rect_and_uses_crop_boundary() -> None:
    evidence = [
        {
            "page": page,
            "rect": {"x": 5000, "y": 5000, "w": 10, "h": 10},
            "text": f"TM-01 VIEW {role} {view}: 伪定位",
        }
        for page, role, view in TM01_VIEWS
    ]
    outcome = VLMClient(settings)._finalize(  # noqa: SLF001
        {"verdict": "pass", "conclusion": "一致", "evidence": evidence},
        _tm01_images(),
        time.monotonic(),
        1,
        prompt_id="tm_four_views_vs_ga",
    )
    assert outcome.verdict == "pass"
    assert all(item["rect"]["x"] < 1191 for item in outcome.evidence)


def test_tm01_adjudicates_each_view_independently() -> None:
    evidence = [
        {
            "image_index": page,
            "text": f"TM-01 VIEW {role} {view}: 波形与通风器-{role}",
        }
        for page, role, view in TM01_VIEWS
    ]
    outcome = _adjudicate_tm01(VLMOutcome(
        verdict="fail",
        conclusion="旧的混合结论",
        evidence=evidence,
        vlm_raw={"facts": {
            "door_end_verdict": "pass",
            "door_end_conclusion": "门端一致",
            "side_verdict": "fail",
            "side_conclusion": "侧板通风器数量不同",
            "front_end_verdict": "pass",
            "front_end_conclusion": "前端一致",
            "roof_verdict": "warning",
            "roof_conclusion": "顶板波形不清",
        }},
    ))

    assert outcome.verdict == "fail"
    verdicts = {item["checkpoint"]: item["checkpoint_verdict"] for item in outcome.evidence}
    assert verdicts == {
        "门端视图": "pass",
        "侧板视图": "fail",
        "前端视图": "pass",
        "顶板视图": "warning",
    }
    assert "旧的混合结论" not in outcome.conclusion


def test_tm01_rejects_pairwise_copied_descriptions() -> None:
    raw_evidence = [
        {
            "page": page,
            "rect": None,
            "text": f"TM-01 VIEW general|marking {view}: 完全相同的描述",
        }
        for page, _, view in TM01_VIEWS
    ]
    outcome = VLMClient(settings)._finalize(  # noqa: SLF001
        {
            "verdict": "pass",
            "conclusion": "一致",
            "evidence": raw_evidence,
            "facts": {
                "door_end_verdict": "pass",
                "door_end_conclusion": "门端一致",
                "side_verdict": "pass",
                "side_conclusion": "侧板一致",
                "front_end_verdict": "pass",
                "front_end_conclusion": "前端一致",
                "roof_verdict": "pass",
                "roof_conclusion": "顶板一致",
            },
        },
        _tm01_images(),
        time.monotonic(),
        1,
        prompt_id="tm_four_views_vs_ga",
    )
    outcome = _adjudicate_tm01(outcome)

    assert outcome.verdict == "warning"
    assert {item["checkpoint_verdict"] for item in outcome.evidence} == {"warning"}
    assert all("|" not in item["text"] for item in outcome.evidence)
    assert all("无法确认模型已分别识别" in item["text"] for item in outcome.evidence)


def test_crop_evidence_maps_back_to_the_original_pdf_page() -> None:
    entry = RenderedPage(
        page=1,
        png_b64="",
        pdf_width=1000,
        pdf_height=800,
        crop=(0.3, 0.2, 0.4, 0.5),
    )

    rect = VLMClient._evidence_rect(  # noqa: SLF001
        {"x": 0.25, "y": 0.2, "w": 0.5, "h": 0.4}, entry
    )

    assert rect == {"x": 400.0, "y": 400.0, "w": 200.0, "h": 160.0}


def test_rerun_clears_stale_vlm_result_fields() -> None:
    with TestClient(app):
        pid = _make_project("mfclear", tech_req="x", trademark=True, ga=True)
    session = SessionLocal()
    try:
        row = session.query(RuleResult).filter_by(project_id=pid, rule_key="TM-01").one()
        row.verdict = "warning"
        row.status = "done"
        row.conclusion = "旧结论"
        row.evidence = [{"type": "pdf", "text": "旧证据"}]
        row.vlm_raw = {"old": True}
        row.tokens_in = 100
        row.tokens_out = 20
        row.cost_cny = 1.2
        row.latency_ms = 5000
        session.flush()
        task_service.rerun_rule(session, pid, "TM-01")
        session.commit()
        session.refresh(row)
        man01 = session.query(RuleResult).filter_by(project_id=pid, rule_key="MAN-01").one()
        assert row.verdict == "pending"
        assert row.status == "pending"
        assert row.conclusion == ""
        assert row.evidence == []
        assert row.vlm_raw is None
        assert row.tokens_in is None
        assert row.tokens_out is None
        assert row.cost_cny is None
        assert row.latency_ms is None
        assert man01.verdict == "pending"
        assert man01.status == "running"
        assert man01.evidence == []
    finally:
        session.close()


def test_tm03_tm05_single_rerun_skips_unrelated_manual_fact_extraction() -> None:
    class RecordingVLM:
        def __init__(self, jobs: list[tuple[str, str]]) -> None:
            self.jobs = jobs
            self.calls = 0
            self.statuses: list[str] = []

        async def call_files(self, **_kwargs) -> VLMOutcome:  # noqa: ANN003
            project_id, rule_key = self.jobs[self.calls]
            session = SessionLocal()
            try:
                row = session.query(RuleResult).filter_by(project_id=project_id, rule_key=rule_key).one()
                self.statuses.append(row.status)
            finally:
                session.close()
            self.calls += 1
            return VLMOutcome(verdict="warning", conclusion="视觉调用已执行", vlm_attempts=1)

    with TestClient(app):
        projects = [
            (_make_project("mfrtm03", tech_req="客户 LOGO 印于门板右侧"), "TM-03"),
            (_make_project("mfrtm05", tech_req="客户名称 ACME，地址 Shanghai"), "TM-05"),
        ]

    client = RecordingVLM(projects)
    runner = Runner(vlm_client=client)  # type: ignore[arg-type]

    async def unexpected_enrichment(_ctx) -> None:  # noqa: ANN001
        raise AssertionError("TM-03/TM-05 单项重跑不应重新调用说明书事实提取")

    runner._enrich_facts = unexpected_enrichment  # type: ignore[method-assign]  # noqa: SLF001
    for project_id, rule_key in projects:
        asyncio.run(runner.run_single(project_id, rule_key))
    assert client.calls == 2
    assert client.statuses == ["running", "running"]
