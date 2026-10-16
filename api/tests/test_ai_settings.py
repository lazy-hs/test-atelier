import os

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, engine  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture
def context():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        token = client.get("/api/v1/auth/csrf").json()["token"]
        logged = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-admin-password-123"}, headers={"x-csrf-token": token})
        headers = {"x-csrf-token": logged.json()["csrf"]}
        project = client.post("/api/v1/projects", json={"name": "模型设置"}, headers=headers).json()
        yield client, headers, f"/api/v1/projects/{project['id']}"


def test_admin_can_save_masked_settings_and_reader_cannot(context):
    client, headers, root = context
    initial = client.get(f"{root}/ai/admin-config").json()
    assert initial["local_model"] == "qwen3:8b" and initial["remote_enabled"] is False
    secret = "sk-test-secret"
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:14b", "remote_enabled": True,
               "remote_url": "https://example.com/v1", "remote_model": "remote-model", "remote_api_key": secret,
               "codex_enabled": True, "codex_command": "codex", "codex_model": "gpt-test",
               "claude_enabled": True, "claude_command": "claude", "claude_model": "sonnet-test"}
    saved = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["local_model"] == "qwen3:14b" and body["remote_enabled"] is True
    assert body["remote_api_key_set"] is True and secret not in saved.text
    assert body["codex_enabled"] is True and body["codex_model"] == "gpt-test"
    assert body["claude_enabled"] is True and body["claude_model"] == "sonnet-test"
    reread = client.get(f"{root}/ai/admin-config").json()
    assert reread["remote_api_key_set"] is True and secret not in client.get(f"{root}/ai/config").text
    user = client.post("/api/v1/users", json={"username": "reader", "password": "reader-password-123", "role": "reader"}, headers=headers)
    assert user.status_code == 200
    assert client.post(f"{root}/members", json={"user_id": user.json()["id"]}, headers=headers).status_code == 200
    client.post("/api/v1/auth/logout", headers=headers)
    csrf = client.get("/api/v1/auth/csrf").json()["token"]
    logged = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password-123"}, headers={"x-csrf-token": csrf})
    reader_headers = {"x-csrf-token": logged.json()["csrf"]}
    assert client.get(f"{root}/ai/config").status_code == 200
    assert client.get(f"{root}/ai/admin-config").status_code == 403
    assert client.put(f"{root}/ai/admin-config", json=payload, headers=reader_headers).status_code == 403


def test_invalid_remote_settings_are_rejected(context):
    client, headers, root = context
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b", "remote_enabled": True,
               "remote_url": "", "remote_model": "", "remote_api_key": ""}
    response = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert response.status_code == 422


def test_cli_update_keeps_existing_remote_key(context):
    client, headers, root = context
    base = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
            "remote_enabled": True, "remote_url": "https://example.com/v1",
            "remote_model": "remote-model", "remote_api_key": "sk-existing"}
    assert client.put(f"{root}/ai/admin-config", json=base, headers=headers).status_code == 200
    cli = {**base, "remote_api_key": "", "codex_enabled": True, "codex_command": "codex"}
    saved = client.put(f"{root}/ai/admin-config", json=cli, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["codex_enabled"] is True and saved.json()["remote_api_key_set"] is True


def test_cli_command_rejects_shell_syntax(context):
    client, headers, root = context
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "codex_enabled": True, "codex_command": "codex --danger"}
    response = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert response.status_code == 422
