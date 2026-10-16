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


def login(client: TestClient, username="admin", password="test-admin-password-123"):
    csrf = client.get("/api/v1/auth/csrf").json()["token"]
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password},
                           headers={"x-csrf-token": csrf})
    assert response.status_code == 200, response.text
    return {"x-csrf-token": response.json()["csrf"]}


def test_project_roles_are_isolated_and_changes_audited(client):
    admin = login(client)
    a = client.post("/api/v1/projects", json={"name": "A"}, headers=admin).json()["id"]
    b = client.post("/api/v1/projects", json={"name": "B"}, headers=admin).json()["id"]
    created = client.post("/api/v1/users", json={"username": "tester", "password": "tester-password-123", "role": "reviewer"}, headers=admin).json()
    uid = created["id"]
    assert client.post(f"/api/v1/projects/{a}/members", json={"user_id": uid, "role": "reviewer"}, headers=admin).status_code == 200
    assert client.post(f"/api/v1/projects/{b}/members", json={"username": "tester", "role": "reader"}, headers=admin).status_code == 200
    assert client.post(f"/api/v1/projects/{b}/members", json={"username": "tester", "role": "manager"}, headers=admin).json()["role"] == "reader"
    assert client.patch(f"/api/v1/projects/{a}/members/{uid}", json={"role": "reader"}).status_code == 403
    client.post("/api/v1/auth/logout", headers=admin)
    tester = login(client, "tester", "tester-password-123")
    projects = {item["id"]: item["role"] for item in client.get("/api/v1/projects").json()}
    assert projects == {a: "reviewer", b: "reader"}
    assert client.post(f"/api/v1/projects/{a}/documents", files={"file": ("req.md", b"# Requirement")}, headers=tester).status_code == 200
    assert client.post(f"/api/v1/projects/{b}/documents", files={"file": ("req.md", b"# Requirement")}, headers=tester).status_code == 403
    assert client.patch(f"/api/v1/projects/{a}/conversion/config", json={"default_prefix_path": "A"}, headers=tester).status_code == 200
    assert client.patch(f"/api/v1/projects/{b}/conversion/config", json={"default_prefix_path": "B"}, headers=tester).status_code == 403
    assert client.get(f"/api/v1/projects/{a}/members").status_code == 200
    assert client.post(f"/api/v1/projects/{a}/members", json={"username": "admin"}, headers=tester).status_code == 403
    client.post("/api/v1/auth/logout", headers=tester)
    admin = login(client)
    assert client.patch(f"/api/v1/projects/{a}/members/{uid}", json={"role": "reader"}, headers=admin).status_code == 200
    events = client.get(f"/api/v1/projects/{a}/member-events").json()
    assert events[0]["action"] == "role_changed" and events[0]["before_role"] == "reviewer"
    assert events[0]["after_role"] == "reader" and events[0]["actor"] == "admin"
    client.post("/api/v1/auth/logout", headers=admin)
    tester = login(client, "tester", "tester-password-123")
    assert client.post(f"/api/v1/projects/{a}/documents", files={"file": ("req.md", b"# Requirement")}, headers=tester).status_code == 403
    assert client.get(f"/api/v1/projects/{a}/member-events").status_code == 403
    client.post("/api/v1/auth/logout", headers=tester)
    admin = login(client)
    assert client.delete(f"/api/v1/projects/{a}/members/{uid}", headers=admin).status_code == 200
    client.post("/api/v1/auth/logout", headers=admin)
    login(client, "tester", "tester-password-123")
    assert {item["id"] for item in client.get("/api/v1/projects").json()} == {b}
    assert client.get(f"/api/v1/projects/{a}/cases").status_code == 403


def test_manager_can_build_personal_project_and_manage_existing_accounts(client):
    admin = login(client)
    project = client.post("/api/v1/projects", json={"name": "Team"}, headers=admin).json()["id"]
    manager = client.post(f"/api/v1/projects/{project}/members/new", json={
        "username": "lead", "password": "lead-password-123", "role": "manager"}, headers=admin)
    assert manager.status_code == 200, manager.text
    reviewer = client.post("/api/v1/users", json={"username": "member", "password": "member-password-123", "role": "reviewer"}, headers=admin).json()
    assert client.post(f"/api/v1/projects/{project}/members/new", json={
        "username": "member", "password": "member-password-123", "role": "reader"}, headers=admin).status_code == 409
    client.post("/api/v1/auth/logout", headers=admin)
    lead = login(client, "lead", "lead-password-123")
    assert client.get("/api/v1/projects").json()[0]["role"] == "manager"
    own = client.post("/api/v1/projects", json={"name": "Personal"}, headers=lead)
    assert own.status_code == 200 and own.json()["role"] == "manager"
    assert client.post("/api/v1/projects", json={"name": "   "}, headers=lead).status_code == 422
    assert client.patch(f"/api/v1/projects/{own.json()['id']}/conversion/config", json={"default_prefix_path": "个人目录"}, headers=lead).status_code == 200
    assert client.post(f"/api/v1/projects/{own.json()['id']}/documents", files={"file": ("own.md", b"# Personal")}, headers=lead).status_code == 200
    assert client.post(f"/api/v1/projects/{project}/members", json={"username": "member", "role": "reviewer"}, headers=lead).status_code == 200
    assert client.patch(f"/api/v1/projects/{project}/members/{reviewer['id']}", json={"role": "reader"}, headers=lead).status_code == 200
    assert client.patch(f"/api/v1/projects/{project}/members/{reviewer['id']}", json={"role": "manager"}, headers=lead).status_code == 403
    assert client.delete(f"/api/v1/projects/{project}/members/{manager.json()['id']}", headers=lead).status_code == 403
    assert client.delete(f"/api/v1/projects/{project}/members/{reviewer['id']}", headers=lead).status_code == 200
    assert client.post(f"/api/v1/projects/{project}/members/new", json={
        "username": "stranger", "password": "some-password-123"}, headers=lead).status_code == 403
    assert client.get(f"/api/v1/projects/{own.json()['id']}/members").status_code == 200
