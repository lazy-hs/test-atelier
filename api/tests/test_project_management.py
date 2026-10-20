import os

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api import ai_routes  # noqa: E402
from api.db import Base, engine  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


def login(client):
    token = client.get("/api/v1/auth/csrf").json()["token"]
    response = client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "test-admin-password-123",
    }, headers={"x-csrf-token": token})
    assert response.status_code == 200, response.text
    return {"x-csrf-token": response.json()["csrf"]}


def test_project_lifecycle_and_archived_filter(client):
    csrf = login(client)
    project = client.post("/api/v1/projects", json={"name": "原项目"}, headers=csrf).json()
    project_id = project["id"]
    renamed = client.patch(f"/api/v1/projects/{project_id}", json={"name": "新名称"}, headers=csrf)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新名称"

    archived = client.post(f"/api/v1/projects/{project_id}/archive", headers=csrf)
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert client.get("/api/v1/projects").json() == []
    assert client.get("/api/v1/projects?include_archived=true").json()[0]["archived"] is True
    restored = client.post(f"/api/v1/projects/{project_id}/restore", headers=csrf)
    assert restored.status_code == 200 and restored.json()["archived"] is False


def test_duplicate_does_not_copy_members_or_case_data(client):
    csrf = login(client)
    source = client.post("/api/v1/projects", json={"name": "源"}, headers=csrf).json()
    clone = client.post(f"/api/v1/projects/{source['id']}/duplicate", json={"name": "副本"}, headers=csrf)
    assert clone.status_code == 200, clone.text
    assert clone.json()["name"] == "副本"
    assert {item["name"] for item in client.get("/api/v1/projects").json()} == {"源", "副本"}


def test_delete_project_cascades_owned_rows(client):
    csrf = login(client)
    project = client.post("/api/v1/projects", json={"name": "待删"}, headers=csrf).json()
    project_id = project["id"]
    deleted = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/v1/projects/{project_id}/overview").status_code == 404

