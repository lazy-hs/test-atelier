import os
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, CodexMessage, CodexSession, Project, SessionLocal, User, engine, utcnow  # noqa: E402
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
def context(tmp_path, monkeypatch):
    from api import ai_routes, conversion_routes
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
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
    # The legacy project workdir setting must not reintroduce checkout context
    # into a newly created workbench session.
    monkeypatch.setenv("AI_STUDIO_CODEX_WORKDIR", str(Path.cwd()))
    calls = []

    def fake_turn(prompt, command, model, cwd):
        calls.append((prompt, command, model, cwd))
        return f"answer-{len(calls)}"

    monkeypatch.setattr("api.ai_engine.request_cli_text", fake_turn)
    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "验证会话"}, headers=headers)
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    workspace = Path(created.json()["working_directory"])
    assert workspace.is_dir()
    assert not list(workspace.iterdir())
    assert workspace.resolve() != Path.cwd().resolve()
    assert workspace.stat().st_mode & 0o777 == 0o700
    second_created = client.post(root, json={"title": "另一个会话"}, headers=headers).json()
    assert second_created["working_directory"] != str(workspace)

    first = client.post(f"{root}/{session_id}/turn", json={"prompt": "检查项目"}, headers=headers)
    assert first.status_code == 200, first.text
    assert [item["role"] for item in first.json()["messages"]] == ["user", "assistant"]
    second = client.post(f"{root}/{session_id}/turn", json={"prompt": "继续修复"}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["messages"][-1]["content"] == "answer-2"
    assert "user: 检查项目" in calls[1][0]
    assert calls[0][1:] == ("codex", "gpt-test", calls[0][3])
    client.post("/api/v1/auth/logout", headers=headers)
    assert client.get(root, headers=login(client)).json()[0]["messages"][-1]["content"] == "answer-2"


def test_codex_workspace_rejects_linked_storage_root_and_invalid_session_id(tmp_path, monkeypatch):
    from api import ai_routes

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "test-atelier-codex-sessions"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This host cannot create symlinks")
    with monkeypatch.context() as patch:
        patch.setattr(ai_routes.tempfile, "gettempdir", lambda: str(tmp_path))
        with pytest.raises(OSError, match="符号链接"):
            ai_routes._create_codex_session_workspace("safe-session")
        assert not ai_routes._workspace_belongs_to_session(str(root / "safe-session"), "safe-session")
    assert not list(outside.iterdir())
    with pytest.raises(OSError, match="ID 不合法"):
        ai_routes._create_codex_session_workspace("../unsafe")


def test_codex_workspace_rejects_windows_junction_root(tmp_path, monkeypatch):
    from api import ai_routes

    root = tmp_path / "test-atelier-codex-sessions"
    root.mkdir()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == root or original(self))
    monkeypatch.setattr(ai_routes.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(OSError, match="junction"):
        ai_routes._create_codex_session_workspace("safe-session")
    assert not ai_routes._workspace_belongs_to_session(str(root / "safe-session"), "safe-session")
    assert not (root / "safe-session").exists()


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


def test_codex_workspace_is_not_created_after_project_deletion_intent(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    from api import ai_routes

    original = ai_engine._validate_cli_command
    workspaces = []
    monkeypatch.setattr(ai_routes, "_create_codex_session_workspace", lambda session_id: workspaces.append(session_id))

    def delete_during_validation(command):
        result = original(command)
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return result

    monkeypatch.setattr(ai_engine, "_validate_cli_command", delete_during_validation)
    created = client.post(f"/api/v1/projects/{project_id}/codex/sessions",
                          json={"title": "不可遗留工作区"}, headers=headers)
    assert created.status_code == 409, created.text
    assert workspaces == []


@pytest.mark.parametrize("stream", [False, True])
def test_codex_turn_rechecks_project_deletion_before_starting(context, monkeypatch, stream):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    from api import ai_routes

    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "栅栏检查"}, headers=headers).json()
    original = ai_routes._workspace_belongs_to_session
    marked = False

    def mark_during_lookup(path, session_id):
        nonlocal marked
        if not marked:
            marked = True
            with SessionLocal() as db:
                db.get(Project, project_id).deletion_pending_at = utcnow()
                db.commit()
        return original(path, session_id)

    monkeypatch.setattr(ai_routes, "_workspace_belongs_to_session", mark_during_lookup)
    endpoint = f"{root}/{created['id']}/turn" + ("/stream" if stream else "")
    response = client.post(endpoint, json={"prompt": "不应启动"}, headers=headers)
    assert response.status_code == 409, response.text
    assert created["id"] not in ai_routes._CODEX_RUNNING_SESSIONS
    with SessionLocal() as db:
        assert db.get(CodexSession, created["id"]).status == "ready"
        assert db.query(CodexMessage).filter_by(session_id=created["id"]).count() == 0


def test_project_delete_refuses_a_codex_process_until_it_finishes(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "执行任务"}, headers=headers).json()
    workspace = Path(created["working_directory"])

    def fake_turn(prompt, command, model, cwd):
        assert Path(cwd) == workspace
        blocked = client.delete(f"/api/v1/projects/{project_id}", headers=headers)
        assert blocked.status_code == 409, blocked.text
        with SessionLocal() as db:
            assert db.get(Project, project_id).deletion_pending_at is None
        return "任务完成"

    monkeypatch.setattr("api.ai_engine.request_cli_text", fake_turn)
    turn = client.post(f"{root}/{created['id']}/turn", json={"prompt": "执行"}, headers=headers)
    assert turn.status_code == 200, turn.text
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 200
    assert not workspace.exists()


def test_sync_uncertain_cli_termination_keeps_deletion_blocked(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    from api import ai_routes

    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "待核对进程"}, headers=headers).json()

    def uncertain_turn(*args):
        raise ai_engine.CliTerminationUncertain("unverified tree")

    monkeypatch.setattr(ai_engine, "request_cli_text", uncertain_turn)
    response = client.post(f"{root}/{created['id']}/turn", json={"prompt": "执行"}, headers=headers)
    assert response.status_code == 503
    with SessionLocal() as db:
        assert db.get(CodexSession, created["id"]).status == "running"
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 409
    with SessionLocal() as db:
        db.get(CodexSession, created["id"]).status = "failed"
        db.commit()
    ai_routes._CODEX_RUNNING_SESSIONS.discard(created["id"])
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 200


def test_stream_disconnect_stops_cli_before_session_can_be_deleted(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    from api import ai_routes

    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "中断任务"}, headers=headers).json()
    workspace = Path(created["working_directory"])
    lifecycle = []

    def fake_stream(*args, **kwargs):
        try:
            lifecycle.append("started")
            yield '{"type":"item.completed","item":{"type":"agent_message","text":"部分输出"}}'
            while True:
                yield None
        finally:
            lifecycle.append("stopped")

    endpoint = next(route.endpoint for outer in app.routes
                    for route in getattr(getattr(outer, "original_router", None), "routes", [])
                    if route.name == "codex_turn_stream")
    with monkeypatch.context() as patch:
        patch.setattr(ai_engine, "stream_cli_text", fake_stream)
        patch.setattr(ai_routes, "StreamingResponse", lambda iterator, **kwargs: iterator)
        with SessionLocal() as db:
            stream = endpoint(project_id, created["id"], ai_routes.CodexTurnRequest(prompt="中断"),
                              user=db.get(User, 1), db=db)
            assert "starting" in next(stream)
            assert "部分输出" in next(stream)
            assert lifecycle == ["started"]
            stream.close()
    assert lifecycle == ["started", "stopped"]
    with SessionLocal() as db:
        assert db.get(CodexSession, created["id"]).status == "failed"
    assert created["id"] not in ai_routes._CODEX_RUNNING_SESSIONS
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 200
    assert not workspace.exists()


def test_stream_uncertain_cli_termination_keeps_deletion_blocked(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    from api import ai_routes

    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "待核对进程"}, headers=headers).json()

    def fake_stream(*args, **kwargs):
        try:
            yield '{"type":"item.completed","item":{"type":"agent_message","text":"部分"}}'
            while True:
                yield None
        finally:
            raise ai_engine.CliTerminationUncertain("unverified tree")

    endpoint = next(route.endpoint for outer in app.routes
                    for route in getattr(getattr(outer, "original_router", None), "routes", [])
                    if route.name == "codex_turn_stream")
    with monkeypatch.context() as patch:
        patch.setattr(ai_engine, "stream_cli_text", fake_stream)
        patch.setattr(ai_routes, "StreamingResponse", lambda iterator, **kwargs: iterator)
        with SessionLocal() as db:
            stream = endpoint(project_id, created["id"], ai_routes.CodexTurnRequest(prompt="中断"),
                              user=db.get(User, 1), db=db)
            next(stream)
            next(stream)
            with pytest.raises(ai_engine.CliTerminationUncertain):
                stream.close()
    with SessionLocal() as db:
        assert db.get(CodexSession, created["id"]).status == "running"
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 409
    # The test has no actual child; emulate an operator's process check before retry.
    with SessionLocal() as db:
        db.get(CodexSession, created["id"]).status = "failed"
        db.commit()
    ai_routes._CODEX_RUNNING_SESSIONS.discard(created["id"])
    assert client.delete(f"/api/v1/projects/{project_id}", headers=headers).status_code == 200


def test_listing_legacy_codex_session_does_not_repair_after_deletion_intent(context, monkeypatch):
    client, headers, project_id = context
    from api import ai_routes

    with SessionLocal() as db:
        db.add(CodexSession(id="legacy-read-race", project_id=project_id, user_id=1,
                            title="旧会话", status="ready", model="", working_directory=str(Path.cwd()),
                            created_at=utcnow(), updated_at=utcnow()))
        db.commit()
    original = ai_routes._workspace_belongs_to_session
    workspaces = []
    marked = False

    def mark_before_repair(path, session_id):
        nonlocal marked
        valid = original(path, session_id)
        if not valid and not marked:
            marked = True
            with SessionLocal() as db:
                db.get(Project, project_id).deletion_pending_at = utcnow()
                db.commit()
        return valid

    monkeypatch.setattr(ai_routes, "_workspace_belongs_to_session", mark_before_repair)
    monkeypatch.setattr(ai_routes, "_create_codex_session_workspace", lambda session_id: workspaces.append(session_id))
    result = client.get(f"/api/v1/projects/{project_id}/codex/sessions", headers=headers)
    assert result.status_code == 409, result.text
    assert workspaces == []
    with SessionLocal() as db:
        assert db.get(CodexSession, "legacy-read-race").working_directory == str(Path.cwd())


def test_embedded_runner_uses_workspace_write_and_extracts_text(monkeypatch, tmp_path):
    calls = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"type": "turn.completed", "output": "已完成"})

    def run(args, prompt, cwd, timeout):
        calls["args"] = args
        calls["prompt"] = prompt
        calls["cwd"] = cwd
        calls["timeout"] = timeout
        return Result()

    monkeypatch.setattr(ai_engine, "_run_cli", run)
    result = ai_engine.request_cli_text("请检查项目", "codex", "gpt-test", str(tmp_path))
    assert result == "已完成"
    assert calls["args"][:13] == [
        "codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
        "--disable", "plugins", "--sandbox", "workspace-write", "--color", "never",
        "-c", "notify=[]",
    ]
    assert calls["args"][-1] == "-"
    assert calls["cwd"] == str(tmp_path)
    assert calls["prompt"] == "请检查项目"
    assert calls["timeout"] == ai_engine.CLI_TIMEOUT


def test_codex_turn_stream_persists_transcript_and_sends_sse(context, monkeypatch):
    client, headers, project_id = context
    enable_codex(client, headers, project_id)
    monkeypatch.setattr("api.ai_engine.stream_cli_text", lambda *args, **kwargs: iter([
        None,
        '{"type":"item.completed","item":{"type":"agent_message","text":"第一段"}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"第一段第二段"}}\n',
    ]))
    root = f"/api/v1/projects/{project_id}/codex/sessions"
    created = client.post(root, json={"title": "流式验证"}, headers=headers).json()
    response = client.post(f"{root}/{created['id']}/turn/stream", json={"prompt": "流式执行"}, headers=headers)
    assert response.status_code == 200, response.text
    assert "event: status" in response.text and '"phase": "waiting"' in response.text
    assert 'event: delta\ndata: {"text": "第一段"}' in response.text
    assert 'event: delta\ndata: {"text": "第二段"}' in response.text
    assert "event: done" in response.text
    listed = client.get(root, headers=headers).json()
    assert listed[0]["messages"][-1]["content"] == "第一段第二段"
