"""API smoke tests via TestClient (env configured in conftest: VLM_DRY_RUN=1)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def _build_payload(data_dir: Path) -> dict:
    files = []
    for code in ("000A22G1G", "000A22G1B", "000A22G1E", "000A22G1F", "000A22G1R", "000A22G1S"):
        pdf = next(data_dir.glob(f"{code}_*.pdf"))
        files.append({"kind": "pdf", "path": str(pdf)})
        files.append({"kind": "api_json", "path": str(pdf.with_name(pdf.name.replace("_api.pdf", "_api.json")))})
        files.append({"kind": "spatial", "path": str(pdf.with_name(pdf.name.replace("_api.pdf", "_spatial.json")))})
    files.append({"kind": "docx", "path": str(next(data_dir.glob("*.docx")))})
    files.append({"kind": "trademark", "path": str(data_dir / "CIMC20GP_26A-00-页面.pdf")})
    return {
        "name": "smoke-test",
        "customer_inputs": {
            "tech_req": "外部颜色 RAL 9016，板厚 4.0mm，质保 5 年",
            "new_material": "",
            "revision_reason": {"type": "标准更新", "other_text": ""},
        },
        "files": files,
    }


def test_health() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["status"] == "ok"
        assert body["data"]["db_writable"] is True
        assert body["data"]["vlm_configured"] is False  # key removed in conftest
        assert body["data"]["vlm_dry_run"] is True


def test_internal_model_key_update_requires_token(monkeypatch) -> None:
    from app.api.v1 import health as health_api
    from app.core.config import settings

    original_key = settings.dashscope_api_key
    original_dry_run = settings.vlm_dry_run
    monkeypatch.setenv("PASSWORD", "test-internal-token")
    try:
        with TestClient(app) as client:
            denied = client.put("/api/v1/internal/model-key", json={"api_key": "example-api-key"})
            assert denied.status_code == 403

            runner = health_api._runner
            assert runner is not None
            runner.vlm._client = object()
            updated = client.put(
                "/api/v1/internal/model-key",
                json={"api_key": "example-api-key"},
                headers={
                    "X-Internal-Config-Token": hashlib.sha256(
                        b"test-internal-token"
                    ).hexdigest()
                },
            )
            assert updated.status_code == 200
            assert updated.json()["data"] == {"configured": True, "runtime_only": True}
            assert settings.dashscope_api_key == "example-api-key"
            assert settings.vlm_dry_run is False
            assert runner.vlm._client is None
    finally:
        settings.dashscope_api_key = original_key
        settings.vlm_dry_run = original_dry_run


def test_rulesets_contract() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/rulesets")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 40
        assert len(data["items"]) == 40
        required = {"rule_key", "domain", "title", "source", "priority", "is_starred", "on_missing"}
        assert required <= set(data["items"][0].keys())


def test_delete_project_cascades_records_and_keeps_running_project() -> None:
    from app.core.db import SessionLocal
    from app.models import Project, RuleResult
    from app.services import task_service

    with TestClient(app) as client:
        session = SessionLocal()
        try:
            session.add(Project(id="p_delete", name="delete", customer_inputs={}))
            session.flush()
            task_service.ensure_rule_rows(session, "p_delete")
            session.commit()
        finally:
            session.close()

        response = client.delete("/api/v1/projects/p_delete")
        assert response.status_code == 200
        assert response.json()["data"] == {"project_id": "p_delete", "deleted": True}

        session = SessionLocal()
        try:
            assert session.get(Project, "p_delete") is None
            assert session.query(RuleResult).filter_by(project_id="p_delete").count() == 0
            session.add(Project(id="p_running_delete", name="running", status="running", customer_inputs={}))
            session.commit()
        finally:
            session.close()

        response = client.delete("/api/v1/projects/p_running_delete")
        assert response.status_code == 409
        assert response.json()["code"] == 4090
        session = SessionLocal()
        try:
            session.get(Project, "p_running_delete").status = "created"
            session.commit()
        finally:
            session.close()
        assert client.delete("/api/v1/projects/p_running_delete").status_code == 200


def test_existing_rule_rows_follow_new_engine_routing() -> None:
    from app.core.db import SessionLocal
    from app.models import Project, RuleResult
    from app.services import task_service

    with TestClient(app):
        session = SessionLocal()
        try:
            project = Project(id="p_engine_sync", name="sync", customer_inputs={})
            session.add(project)
            session.flush()
            task_service.ensure_rule_rows(session, project.id)
            session.flush()
            session.query(RuleResult).filter_by(project_id=project.id, rule_key="DOOR-03").one().engine = "vlm"
            session.query(RuleResult).filter_by(project_id=project.id, rule_key="TM-01").one().engine = "rule"
            session.query(RuleResult).filter_by(project_id=project.id, rule_key="GEN-01").one().title = "旧标题"
            session.flush()
            assert task_service.sync_rule_engines(session) == 3
            assert session.query(RuleResult).filter_by(project_id=project.id, rule_key="DOOR-03").one().engine == "rule"
            assert session.query(RuleResult).filter_by(project_id=project.id, rule_key="TM-01").one().engine == "vlm"
            assert session.query(RuleResult).filter_by(project_id=project.id, rule_key="GEN-01").one().title == "各分图钣金厚度标注完整性检查（排除总图）"
        finally:
            session.rollback()
            session.close()


def test_full_pipeline(real_docx_path) -> None:  # noqa: ANN001
    data_dir = real_docx_path.parent
    with TestClient(app) as client:
        payload = _build_payload(data_dir)
        create = client.post("/api/v1/projects", json=payload)
        assert create.status_code == 201, create.text
        project = create.json()["data"]
        pid = project["id"]
        assert project["status"] == "created"

        start = client.post(f"/api/v1/projects/{pid}/start")
        assert start.status_code == 200, start.text
        assert start.json()["data"]["task_status"] == "running"

        # poll until all 40 items leave pending (dry-run VLM is fast)
        deadline = time.time() + 90
        done = 0
        while time.time() < deadline:
            resp = client.get(f"/api/v1/projects/{pid}/rules")
            items = resp.json()["data"]["items"]
            done = sum(1 for i in items if i["verdict"] != "pending")
            if done == 40:
                break
            time.sleep(1.0)
        assert done == 40, f"only {done}/40 finished"

        detail = client.get(f"/api/v1/projects/{pid}").json()["data"]
        assert detail["project"]["status"] in ("completed", "running", "aggregating")
        assert detail["summary"]["pending"] == 0

        # report
        report = client.get(f"/api/v1/projects/{pid}/report", params={"format": "md"})
        assert report.status_code == 200
        assert "# 审图报告" in report.json()["data"]["content"]
