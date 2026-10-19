import os
import json

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, engine  # noqa: E402
from api.main import app  # noqa: E402
from api import ai_engine  # noqa: E402


def login(client: TestClient, username: str = "admin", password: str = "test-admin-password-123"):
    csrf = client.get("/api/v1/auth/csrf").json()["token"]
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 200, response.text
    return {"x-csrf-token": response.json()["csrf"]}


@pytest.fixture
def context():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        headers = login(client)
        project = client.post("/api/v1/projects", json={"name": "Codex 工作台"}, headers=headers)
        assert project.status_code == 200, project.text
        yield client, headers, project.json()["id"]


def enable_codex(client: TestClient, headers: dict[str, str], project_id: int):
    response = client.put(
        f"/api/v1/projects/{project_id}/ai/admin-config",
        json={
            "local_url": "http://127.0.0.1:11434",
            "local_model": "qwen3:8b",
            "codex_enabled": True,
            "codex_command": "codex",
            "codex_model": "gpt-test",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_codex_session_turn_keeps_bounded_conversation(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    calls = []

    def fake_turn(prompt, command, model, cwd):
        calls.append((prompt, command, model, cwd))
        return f"answer-{len(calls)}"

    monkeypatch.setattr("api.ai_engine.request_cli_text", fake_turn)
    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "验证会话"}, headers=headers)
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]

    first = client.post(f"{root}/{session_id}/turn", json={"prompt": "检查项目"}, headers=headers)
    assert first.status_code == 200, first.text
    assert [item["role"] for item in first.json()["messages"]] == ["user", "assistant"]
    second = client.post(f"{root}/{session_id}/turn", json={"prompt": "继续修复"}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["messages"][-1]["content"] == "answer-2"
    assert "user: 检查项目" in calls[1][0]
    assert calls[0][1:] == ("codex", "gpt-test", calls[0][3])


def test_codex_session_creation_requires_project_write_access(context):
    client, admin_headers, project_id = context
    enable_codex(client, admin_headers, project_id)
    created = client.post(
        "/api/v1/users",
        json={"username": "codex-reader", "password": "reader-password-123", "role": "reviewer"},
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    member = client.post(
        f"/api/v1/projects/{project_id}/members",
        json={"user_id": created.json()["id"], "role": "reader"},
        headers=admin_headers,
    )
    assert member.status_code == 200, member.text
    client.post("/api/v1/auth/logout", headers=admin_headers)
    reader_headers = login(client, "codex-reader", "reader-password-123")
    root = f"/api/v1/projects/{project_id}/codex/sessions"
    assert client.get(root, headers=reader_headers).status_code == 200
    assert client.post(root, json={"title": "不应创建"}, headers=reader_headers).status_code == 403


def test_embedded_runner_uses_workspace_write_and_extracts_text(monkeypatch, tmp_path):
    calls = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"type": "turn.completed", "output": "已完成"})

    def run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(ai_engine.subprocess, "run", run)
    result = ai_engine.request_cli_text("请检查项目", "codex", "gpt-test", str(tmp_path))
    assert result == "已完成"
    assert calls["args"][:13] == [
        "codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
        "--disable", "plugins", "--sandbox", "workspace-write", "--color", "never",
        "-c", "notify=[]",
    ]
    assert calls["args"][-1] == "-"
    assert calls["kwargs"]["cwd"] == str(tmp_path)
    assert calls["kwargs"]["shell"] is False
