import io
import os

import openpyxl
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import (  # noqa: E402
    Base,
    Case,
    ConversionSettings,
    ImportBatch,
    Membership,
    MembershipEvent,
    Project,
    ProjectSkill,
    SourceDocument,
    SessionLocal,
    engine,
)
from api.main import app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Keep document artifacts isolated; project deletion must remove their rows
    # even when no external object storage is configured.
    from api import ai_routes

    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, username="admin", password="test-admin-password-123"):
    csrf = client.get("/api/v1/auth/csrf").json()["token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200, response.text
    return {"x-csrf-token": response.json()["csrf"]}


def workbook() -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["测试用例ID", "用例名称", "用例等级", "是否冒烟用例", "用例步骤"])
    sheet.append(["P-001", "Project deletion case", "1", "否", "step"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def test_project_rename_requires_project_manager_and_updates_list(client):
    admin = login(client)
    project = client.post("/api/v1/projects", json={"name": "Original"}, headers=admin).json()
    project_id = project["id"]

    renamed = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"name": "Renamed"},
        headers=admin,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    listed = client.get("/api/v1/projects").json()
    assert next(item for item in listed if item["id"] == project_id)["name"] == "Renamed"

    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "   "}, headers=admin
    ).status_code == 422
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "No CSRF"}
    ).status_code == 403

    created = client.post(
        "/api/v1/users",
        json={"username": "project-manager", "password": "manager-password-123", "role": "reviewer"},
        headers=admin,
    ).json()
    assert client.post(
        f"/api/v1/projects/{project_id}/members",
        json={"user_id": created["id"], "role": "manager"},
        headers=admin,
    ).status_code == 200
    client.post("/api/v1/auth/logout", headers=admin)

    manager = login(client, "project-manager", "manager-password-123")
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "Manager renamed"}, headers=manager
    ).status_code == 200
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "Reader cannot rename"},
    ).status_code == 403


def test_project_delete_denied_to_non_manager_and_cleans_project_records(client):
    admin = login(client)
    project = client.post("/api/v1/projects", json={"name": "Delete me"}, headers=admin).json()
    project_id = project["id"]

    # Exercise several project-owned tables before deletion.
    preview = client.post(
        f"/api/v1/projects/{project_id}/imports/preview",
        files={"file": ("cases.xlsx", workbook())},
        headers=admin,
    )
    assert preview.status_code == 200, preview.text
    assert client.post(
        f"/api/v1/projects/{project_id}/imports/{preview.json()['id']}/confirm", headers=admin
    ).status_code == 200
    assert client.post(
        f"/api/v1/projects/{project_id}/documents",
        files={"file": ("requirements.md", b"# requirement")},
        headers=admin,
    ).status_code == 200
    assert client.patch(
        f"/api/v1/projects/{project_id}/conversion/config",
        json={"default_prefix_path": "project-data"},
        headers=admin,
    ).status_code == 200

    reviewer = client.post(
        "/api/v1/users",
        json={"username": "project-reviewer", "password": "reviewer-password-123", "role": "reviewer"},
        headers=admin,
    ).json()
    reader = client.post(
        "/api/v1/users",
        json={"username": "project-reader", "password": "reader-password-123", "role": "reviewer"},
        headers=admin,
    ).json()
    for member, role in ((reviewer, "reviewer"), (reader, "reader")):
        assert client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": member["id"], "role": role},
            headers=admin,
        ).status_code == 200

    client.post("/api/v1/auth/logout", headers=admin)
    reviewer_headers = login(client, "project-reviewer", "reviewer-password-123")
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "reviewer"}, headers=reviewer_headers
    ).status_code == 403
    assert client.delete(f"/api/v1/projects/{project_id}", headers=reviewer_headers).status_code == 403
    client.post("/api/v1/auth/logout", headers=reviewer_headers)
    reader_headers = login(client, "project-reader", "reader-password-123")
    assert client.delete(f"/api/v1/projects/{project_id}", headers=reader_headers).status_code == 403

    client.post("/api/v1/auth/logout", headers=reader_headers)
    admin = login(client)
    deleted = client.delete(f"/api/v1/projects/{project_id}", headers=admin)
    assert deleted.status_code == 200, deleted.text
    assert all(item["id"] != project_id for item in client.get("/api/v1/projects").json())
    assert client.get(f"/api/v1/projects/{project_id}/overview").status_code == 404

    with SessionLocal() as db:
        assert db.get(Project, project_id) is None
        assert db.query(ProjectSkill).filter_by(project_id=project_id).count() == 0
        assert db.query(Membership).filter_by(project_id=project_id).count() == 0
        assert db.query(MembershipEvent).filter_by(project_id=project_id).count() == 0
        assert db.query(ImportBatch).filter_by(project_id=project_id).count() == 0
        assert db.query(Case).filter_by(project_id=project_id).count() == 0
        assert db.query(SourceDocument).filter_by(project_id=project_id).count() == 0
        assert db.query(ConversionSettings).filter_by(project_id=project_id).count() == 0
    from api import ai_routes

    assert not [path for path in ai_routes.DOCUMENT_DIR.rglob("*") if path.is_file()]
