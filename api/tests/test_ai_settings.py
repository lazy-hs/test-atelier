import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import AiSettings, Base, SessionLocal, engine  # noqa: E402
from api import ai_engine  # noqa: E402
from api import ai_routes  # noqa: E402
from api import document_extract  # noqa: E402
from api.main import app  # noqa: E402
import api.main as main  # noqa: E402
from api.observability import FailureWindow  # noqa: E402


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


def test_provider_catalog_round_trips_without_exposing_api_key(context):
    client, headers, root = context
    secret = "sk-provider-secret"
    payload = {
        "local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
        "remote_enabled": True, "remote_url": "https://relay.example/v1",
        "remote_model": "relay-model", "remote_api_key": "",
        "providers": [{"id": "custom-relay", "kind": "remote", "name": "My Relay",
                       "endpoint": "https://relay.example/v1", "model": "relay-model",
                       "enabled": True, "protocol": "openai", "api_key": secret},
                      {"id": "local", "kind": "local", "name": "本机 Ollama",
                       "endpoint": "http://127.0.0.1:11434", "model": "qwen3:8b",
                       "enabled": False, "protocol": "openai"}],
    }
    response = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["providers"][0]["name"] == "My Relay"
    assert body["providers"][0]["api_key_set"] is True
    assert body["local_enabled"] is False
    assert secret not in response.text
    with SessionLocal() as db:
        saved = db.get(AiSettings, 1)
        assert saved is not None and secret not in saved.providers_json


def test_provider_probe_actions_detect_models_and_test_selected(context, monkeypatch):
    client, headers, root = context

    class Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": [{"id": "alpha"}, {"id": "beta", "name": "Beta"}]}

        def raise_for_status(self):
            return None

    calls = []
    monkeypatch.setattr(ai_routes.httpx, "post", lambda url, **kwargs: calls.append(("post", url, kwargs)) or Response())
    monkeypatch.setattr(ai_routes.httpx, "get", lambda url, **kwargs: calls.append(("get", url, kwargs)) or Response())
    payload = {"endpoint": "https://relay.example/v1", "protocol": "openai", "model": "alpha", "api_key": "sk-probe"}
    detected = client.post(f"{root}/ai/admin-config/detect", json=payload, headers=headers)
    assert detected.status_code == 200 and len(detected.json()["results"]) == 5
    listed = client.post(f"{root}/ai/admin-config/models", json=payload, headers=headers)
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["models"]] == ["alpha", "beta"]
    tested = client.post(f"{root}/ai/admin-config/test-models", json={**payload, "models": ["alpha", "beta"]}, headers=headers)
    assert tested.status_code == 200 and len(tested.json()["results"]) == 2
    assert any(kind == "get" and url.endswith("/models") for kind, url, _ in calls)


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


def test_cli_connection_check_runs_a_real_turn(context, monkeypatch):
    client, headers, root = context
    calls = {}

    def check(provider, command, model):
        calls.update(provider=provider, command=command, model=model)

    monkeypatch.setattr("api.ai_engine.test_cli_connection", check)
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "codex_enabled": True, "codex_command": "codex", "codex_model": "gpt-test",
               "provider": "codex_cli"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "CLI 可连接"
    assert calls == {"provider": "codex_cli", "command": "codex", "model": "gpt-test"}


def test_claude_connection_uses_default_command_when_blank(context, monkeypatch):
    client, headers, root = context
    calls = {}
    monkeypatch.setattr("api.ai_engine.test_cli_connection", lambda provider, command, model: calls.update(
        provider=provider, command=command, model=model
    ))
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "claude_enabled": True, "claude_command": "", "claude_model": "sonnet-test",
               "provider": "claude_code"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert calls == {"provider": "claude_code", "command": "claude", "model": "sonnet-test"}


def test_remote_connection_reuses_saved_key_when_payload_is_masked(context, monkeypatch):
    client, headers, root = context
    base = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
            "remote_enabled": True, "remote_url": "https://example.com/v1",
            "remote_model": "remote-model", "remote_api_key": "sk-existing"}
    assert client.put(f"{root}/ai/admin-config", json=base, headers=headers).status_code == 200
    captured = {}
    monkeypatch.setattr(ai_routes.httpx, "get", lambda url, headers, timeout: captured.update(
        url=url, headers=headers, timeout=timeout
    ) or type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"data": []}})())
    payload = {**base, "remote_api_key": "", "provider": "remote"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert captured["headers"] == {"Authorization": "Bearer sk-existing"}


def test_cli_connection_error_does_not_echo_provider_credentials(context, monkeypatch):
    client, headers, root = context

    def fail(*_args):
        raise RuntimeError("provider failed postgresql://app:db-secret@db/app password=hunter2")

    monkeypatch.setattr("api.ai_engine.test_cli_connection", fail)
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "codex_enabled": True, "codex_command": "codex", "provider": "codex_cli"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 502
    assert "db-secret" not in response.text
    assert "hunter2" not in response.text


def test_cli_connection_timeout_returns_gateway_timeout(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setattr("api.ai_engine.test_cli_connection",
                        lambda *_args: (_ for _ in ()).throw(ai_engine.CliTimeout("CLI 执行超时（45 秒）")))
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "codex_enabled": True, "codex_command": "codex", "provider": "codex_cli"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 504
    assert "45 秒" in response.text


def test_provider_connection_test_writes_a_bounded_audit_log(context, monkeypatch):
    client, headers, root = context
    events = []

    def record(level, message, **kwargs):
        events.append((level, message, kwargs.get("extra", {})))

    monkeypatch.setattr(ai_routes.logger, "log", record)
    monkeypatch.setattr(ai_engine, "test_cli_connection", lambda *_args: None)
    payload = {"local_url": "http://127.0.0.1:11434", "local_model": "qwen3:8b",
               "codex_enabled": True, "codex_command": "codex", "provider": "codex_cli"}
    response = client.post(f"{root}/ai/admin-config/test", json=payload, headers=headers)
    assert response.status_code == 200
    assert events and events[-1][1] == "ai.provider.connection_test.succeeded"
    assert events[-1][2]["provider"] == "codex_cli"
    assert events[-1][2]["duration_ms"] >= 0


def test_login_failure_window_returns_retry_after(context, monkeypatch):
    client, _, _ = context
    monkeypatch.setattr(main, "_login_failures", FailureWindow(limit=2, window_seconds=60))
    csrf = client.get("/api/v1/auth/csrf").json()["token"]
    headers = {"x-csrf-token": csrf}
    for _ in range(2):
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}, headers=headers)
        assert response.status_code == 401
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}, headers=headers)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_login_failure_limiter_uses_forwarded_client_only_when_trusted(context, monkeypatch):
    from starlette.requests import Request
    request = Request({
        "type": "http", "method": "GET", "path": "/api/v1/health/live", "headers": [
            (b"x-real-ip", b"203.0.113.9"),
            (b"x-forwarded-for", b"203.0.113.9, 10.0.0.4"),
        ], "client": ("127.0.0.1", 1234), "server": ("test", 80),
        "scheme": "http", "query_string": b"", "root_path": "", "http_version": "1.1",
    })
    assert main._client_address(request) == "127.0.0.1"
    monkeypatch.setenv("AI_STUDIO_TRUST_PROXY_HEADERS", "1")
    assert main._client_address(request) == "203.0.113.9"


def test_request_content_length_limit_is_enforced_before_route(context, monkeypatch):
    client, _, _ = context
    events = []
    monkeypatch.setattr(main.logger, "log", lambda _level, _message, **kwargs: events.append(kwargs.get("extra", {})))
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 1)
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "x"})
    assert response.status_code == 413
    assert response.json()["detail"] == "请求体超过大小限制"
    assert response.headers.get("x-request-id")
    completed = next(item for item in events if item.get("event") == "request.completed")
    assert completed["request_bytes"] == 0
    assert completed["declared_bytes"] > 1
    assert "password" not in completed


def test_streamed_request_limit_rejects_chunked_body(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 3)
    messages = iter([
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ])

    async def receive():
        return next(messages)

    request = Request({
        "type": "http", "method": "POST", "path": "/api/v1/test",
        "headers": [], "client": ("127.0.0.1", 1234), "server": ("test", 80),
        "scheme": "http", "query_string": b"", "root_path": "", "http_version": "1.1",
    }, receive)

    async def call_next(incoming):
        await incoming.body()
        return Response("ok")

    response = asyncio.run(main.request_logging(request, call_next))
    assert response.status_code == 413
    assert response.headers.get("x-request-id")


def test_write_request_emits_audit_event_without_payload(context, monkeypatch):
    client, headers, _ = context
    events = []
    monkeypatch.setattr(main.logger, "info", lambda message, **kwargs: events.append((message, kwargs.get("extra", {}))))
    response = client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200
    audit = next(extra for message, extra in events if message == "request.audit")
    assert audit["route"] == "/api/v1/auth/logout"
    assert audit["status"] == 200
    assert isinstance(audit["duration_ms"], float)
    assert "body" not in audit and "prompt" not in audit


def test_failed_request_is_logged_at_warning_level(context, monkeypatch):
    client, _, _ = context
    events = []
    monkeypatch.setattr(main.logger, "log", lambda level, message, **kwargs: events.append((level, message, kwargs.get("extra", {}))))
    response = client.get("/api/v1/route-that-does-not-exist")
    assert response.status_code == 404
    completed = next(extra for level, message, extra in events if message == "request.completed")
    assert completed["status"] == 404
    assert any(level >= 30 for level, message, _ in events if message == "request.completed")


def test_unmatched_request_path_is_redacted_before_logging():
    request = Request({
        "type": "http", "method": "GET", "path": "/not-found/token=path-secret",
        "headers": [], "client": ("127.0.0.1", 1234), "server": ("test", 80),
        "scheme": "http", "query_string": b"", "root_path": "", "http_version": "1.1",
    })

    path = main._log_path(request)

    assert path == "/not-found/token=[REDACTED]"
    assert "path-secret" not in path


def test_document_converter_timeout_has_specific_structured_error(context, monkeypatch):
    client, headers, base = context

    def timeout(*_args, **_kwargs):
        raise document_extract.DocumentExtractTimeout("旧版 Word 转换超时")

    events = []
    monkeypatch.setattr(ai_routes, "extract", timeout)
    monkeypatch.setattr(ai_routes.logger, "warning",
                        lambda message, **kwargs: events.append((message, kwargs.get("extra", {}))))
    response = client.post(f"{base}/documents", files={"file": ("需求.doc", b"content")}, headers=headers)

    assert response.status_code == 422
    event, fields = next(item for item in events if item[0] == "document.extract.failed")
    assert fields["error_type"] == "DocumentExtractTimeout"


def test_metrics_auth_failure_is_structured_without_token(context, monkeypatch):
    client, _, _ = context
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret")
    events = []
    monkeypatch.setattr(main.logger, "warning", lambda message, **kwargs: events.append((message, kwargs.get("extra", {}))))
    response = client.get("/api/v1/metrics", headers={"X-Metrics-Token": "wrong-token"})
    assert response.status_code == 401
    event, fields = next(item for item in events if item[0] == "metrics.auth.failed")
    assert fields["error_type"] == "InvalidMetricsToken"
    assert "metrics-secret" not in str(fields)
    assert "wrong-token" not in str(fields)


def test_unhandled_error_keeps_request_correlation_and_security_headers():
    request = Request({
        "type": "http", "method": "GET", "path": "/api/v1/test-error",
        "headers": [(b"x-request-id", b"trace-unhandled")], "client": ("127.0.0.1", 1234),
        "server": ("test", 80), "scheme": "http", "query_string": b"", "root_path": "",
        "http_version": "1.1",
    }, lambda: None)

    response = asyncio.run(main.unhandled_exception(request, RuntimeError("boom")))

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-unhandled"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.body.decode().find('"request_id":"trace-unhandled"') >= 0


def test_new_database_seeds_provider_settings_from_environment(monkeypatch):
    Base.metadata.drop_all(engine)
    monkeypatch.setenv("AI_STUDIO_OLLAMA_MODEL", "qwen3:14b")
    monkeypatch.setenv("AI_STUDIO_CODEX_ENABLED", "1")
    monkeypatch.setenv("AI_STUDIO_CODEX_COMMAND", "codex")
    monkeypatch.setenv("AI_STUDIO_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("AI_STUDIO_REMOTE_ENABLED", "1")
    monkeypatch.setenv("AI_STUDIO_REMOTE_URL", "https://example.com/v1")
    monkeypatch.setenv("AI_STUDIO_REMOTE_MODEL", "remote-test")
    monkeypatch.setenv("AI_STUDIO_REMOTE_API_KEY", "sk-seeded")
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
    with SessionLocal() as db:
        settings = db.get(AiSettings, 1)
        assert settings.local_model == "qwen3:14b"
        assert settings.codex_enabled is True and settings.codex_model == "gpt-test"
        assert settings.remote_enabled is True and ai_engine.decrypt_secret(settings.remote_api_key) == "sk-seeded"
