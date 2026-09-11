from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import projects
from app.main import app

FORM = {
    "name": "upload-test",
    "tech_req": "按客户要求审核",
    "revision_reason_type": "部门要求",
}


@pytest.fixture(autouse=True)
def empty_upload_dir():  # noqa: ANN201
    shutil.rmtree(projects.settings.upload_dir, ignore_errors=True)
    projects.settings.upload_dir.mkdir(parents=True)
    yield
    shutil.rmtree(projects.settings.upload_dir, ignore_errors=True)


def _upload(client: TestClient, filename: str, content: bytes, kind: str, mime: str):
    return client.post(
        "/api/v1/projects/upload",
        data={**FORM, "kinds": kind},
        files={"files": (filename, content, mime)},
    )


def test_upload_success_and_delete_removes_only_managed_copy() -> None:
    external = projects.settings.upload_dir.parent / "external.json"
    external.write_text("keep", encoding="utf-8")
    with TestClient(app) as client:
        response = _upload(
            client,
            "000A22G1G_test_api.json",
            b'{"customProperties": []}',
            "api_json",
            "application/json",
        )
        assert response.status_code == 201, response.text
        project_id = response.json()["data"]["id"]
        saved = Path(client.get(f"/api/v1/projects/{project_id}").json()["data"]["project"]["files"][0]["path"])
        assert saved.is_file() and saved.parent.parent == projects.settings.upload_dir.resolve()
        assert client.delete(f"/api/v1/projects/{project_id}").status_code == 200
        assert not saved.parent.exists()
        assert external.read_text(encoding="utf-8") == "keep"


def test_upload_rejects_mismatch_traversal_and_invalid_content() -> None:
    with TestClient(app) as client:
        mismatch = client.post(
            "/api/v1/projects/upload",
            data={**FORM, "kinds": "api_json"},
            files=[
                ("files", ("a.json", b"{}", "application/json")),
                ("files", ("b.json", b"{}", "application/json")),
            ],
        )
        assert mismatch.status_code == 400
        assert _upload(client, "../escape.pdf", b"%PDF-1.4", "pdf", "application/pdf").status_code == 400
        assert _upload(client, "000A22G1G_test_api.json", b"not-json", "api_json", "application/json").status_code == 400
        assert _upload(client, "000A22G1G_test_api.json", b"{}", "api_json", "text/plain").status_code == 400
        assert not any(projects.settings.upload_dir.iterdir())


def test_upload_size_failure_cleans_partial_batch(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(projects, "_MAX_FILE", 4)
    with TestClient(app) as client:
        response = _upload(client, "000A22G1G_test_api.json", b'{"x": 1}', "api_json", "application/json")
    assert response.status_code == 413
    assert not any(projects.settings.upload_dir.iterdir())


def test_delete_helper_never_touches_external_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    projects._remove_managed_uploads([SimpleNamespace(path=str(outside / "keep.txt"))])
    assert outside.is_dir()
