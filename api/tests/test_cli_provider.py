import json
import stat
import threading
import time

import pytest

from api import ai_engine


def test_cli_provider_uses_argv_and_parses_jsonl(monkeypatch):
    calls = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"type": "item.completed", "item": {"text": json.dumps({"ok": True})}})

    def run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(ai_engine.subprocess, "run", run)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    result = ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex", "gpt-test")
    assert result == {"ok": True}
    assert calls["args"][:9] == ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules", "--disable", "plugins", "--sandbox", "read-only"]
    assert "--model" in calls["args"] and "gpt-test" in calls["args"]
    assert "model_reasoning_effort=\"low\"" in calls["args"]
    assert calls["args"][-1] == "-"
    assert calls["kwargs"]["input"] == "hello"
    assert calls["kwargs"]["shell"] is not True


def test_cli_output_parses_array_schema(monkeypatch):
    """Array schemas must match as a whole; the governance prompt relies on it."""
    schema = {"type": "array", "items": {"type": "object", "required": ["case_id", "decision", "reason"],
                                         "properties": {"case_id": {"type": "integer"}}}}
    stdout = json.dumps([{"case_id": 1, "decision": "keep", "reason": "newer"}])
    assert ai_engine._json_from_cli_output(stdout, schema) == [
        {"case_id": 1, "decision": "keep", "reason": "newer"}]

    wrapped = json.dumps({"type": "item.completed", "item": {"text": stdout}})
    assert ai_engine._json_from_cli_output(wrapped, schema) == [
        {"case_id": 1, "decision": "keep", "reason": "newer"}]

    incomplete = json.dumps([{"case_id": 1, "decision": "keep"}])
    with pytest.raises(ai_engine.StructuredOutputError):
        ai_engine._json_from_cli_output(incomplete, schema)


def test_cli_provider_rejects_shell_syntax():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(RuntimeError, match="单个可执行"):
        ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex --danger", "")


def test_cli_environment_excludes_application_secrets(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_REMOTE_API_KEY", "remote-secret")
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db/app")
    monkeypatch.setenv("AI_STUDIO_CODEX_ENABLED", "1")
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/tmp/desktop.sock")
    monkeypatch.setenv("CODEX_SESSION_ID", "desktop-session")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "cli-auth-is-allowed")
    environment = ai_engine._cli_environment()
    assert "AI_STUDIO_REMOTE_API_KEY" not in environment
    assert "AI_STUDIO_SESSION_SECRET" not in environment
    assert "DATABASE_URL" not in environment
    assert "AI_STUDIO_CODEX_ENABLED" not in environment
    assert "CODEX_APP_TOOLS_PIPE_PATH" not in environment
    assert "CODEX_SESSION_ID" not in environment
    assert environment["ANTHROPIC_API_KEY"] == "cli-auth-is-allowed"


def test_checked_secret_decryption_surfaces_session_secret_rotation():
    encrypted = ai_engine.encrypt_secret("provider-secret")
    assert ai_engine.decrypt_secret_checked(encrypted) == "provider-secret"
    with pytest.raises(RuntimeError, match="无法解密"):
        ai_engine.decrypt_secret_checked("not-a-valid-fernet-token")


def test_cli_provider_allows_executable_absolute_path_with_spaces(tmp_path):
    executable = tmp_path / "CLI tools" / "codex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    assert ai_engine._validate_cli_command(str(executable)) == str(executable)


def test_cli_provider_timeout_explains_session_recovery(monkeypatch):
    def run(*args, **kwargs):
        error = ai_engine.subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs["timeout"])
        error.stderr = b"startup warning\nprovider failed password=hunter2\n"
        raise error

    monkeypatch.setattr(ai_engine.subprocess, "run", run)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(ai_engine.CliTimeout, match="执行超时（7 秒）") as error:
        ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex", "", timeout=7)
    assert "b'" not in str(error.value)
    assert "hunter2" not in str(error.value)


def test_cli_provider_errors_use_user_facing_name(monkeypatch):
    class Result:
        returncode = 1
        stderr = ""
        stdout = ""

    monkeypatch.setattr(ai_engine.subprocess, "run", lambda *args, **kwargs: Result())
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(RuntimeError, match="Codex CLI 执行失败"):
        ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex", "")


def test_cli_provider_redacts_credentials_from_failure_detail(monkeypatch):
    class Result:
        returncode = 1
        stderr = "provider failed postgresql://app:db-secret@db/app password=hunter2 Bearer abc.def"
        stdout = ""

    monkeypatch.setattr(ai_engine.subprocess, "run", lambda *args, **kwargs: Result())
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(RuntimeError) as error:
        ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex", "")
    message = str(error.value)
    assert "db-secret" not in message
    assert "hunter2" not in message
    assert "Bearer abc.def" not in message
    assert "[REDACTED]" in message


def test_cli_provider_disabled(monkeypatch):
    monkeypatch.setattr(ai_engine, "CODEX_ENABLED", False)
    with pytest.raises(RuntimeError, match="Codex CLI"):
        ai_engine.request_structured("hello", {}, "codex_cli")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_cli_env_boolean_accepts_deployment_truthy_values(monkeypatch, value):
    monkeypatch.setenv("AI_STUDIO_CODEX_ENABLED", value)
    assert ai_engine._env_truthy("AI_STUDIO_CODEX_ENABLED") is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", ""])
def test_cli_env_boolean_rejects_false_values(monkeypatch, value):
    monkeypatch.setenv("AI_STUDIO_CODEX_ENABLED", value)
    assert ai_engine._env_truthy("AI_STUDIO_CODEX_ENABLED") is False


def test_cli_connection_uses_a_real_structured_turn(monkeypatch):
    calls = {}

    def request(prompt, schema, provider, command, model, timeout=None):
        calls.update(prompt=prompt, schema=schema, provider=provider, command=command,
                     model=model, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(ai_engine, "_request_cli_structured", request)
    ai_engine.test_cli_connection("codex", "codex", "gpt-test")
    assert calls["provider"] == "codex_cli"
    assert calls["command"] == "codex"
    assert calls["timeout"] >= 5
    assert calls["schema"]["required"] == ["ok"]


def test_cli_connection_is_recorded_in_model_metrics(monkeypatch):
    observations = []
    monkeypatch.setattr(ai_engine, "_request_cli_structured", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(ai_engine.metrics, "observe_model",
                        lambda provider, outcome, duration: observations.append((provider, outcome, duration)))
    ai_engine.test_cli_connection("codex_cli", "codex")
    assert observations and observations[0][:2] == ("codex_cli", "success")
    assert observations[0][2] >= 0


def test_model_request_logs_metrics_for_success_and_failure(monkeypatch):
    observations = []
    monkeypatch.setattr(ai_engine.metrics, "observe_model",
                        lambda provider, outcome, duration: observations.append((provider, outcome, duration)))
    assert ai_engine._run_logged("codex_cli", lambda: {"ok": True}) == {"ok": True}
    with pytest.raises(RuntimeError):
        ai_engine._run_logged("codex_cli", lambda: (_ for _ in ()).throw(RuntimeError("provider down")))
    assert [item[1] for item in observations] == ["success", "failure"]
    assert all(item[2] >= 0 for item in observations)


def test_model_request_records_cli_timeout_as_distinct_outcome(monkeypatch):
    observations = []
    records = []
    monkeypatch.setattr(ai_engine.metrics, "observe_model",
                        lambda provider, outcome, duration: observations.append((provider, outcome)))
    monkeypatch.setattr(ai_engine.logger, "warning",
                        lambda message, **kwargs: records.append((message, kwargs.get("extra", {}))))
    with pytest.raises(ai_engine.CliTimeout):
        ai_engine._run_logged("codex_cli", lambda: (_ for _ in ()).throw(ai_engine.CliTimeout("timed out")))
    assert observations == [("codex_cli", "timeout")]
    assert records and records[-1][0] == "model.request.timeout"
    assert records[-1][1]["error_type"] == "CliTimeout"


def test_remote_request_inlines_schema_and_falls_back_to_json_object(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                request = type("Request", (), {"url": "https://relay.example/v1/chat/completions"})()
                response = type("ResponseError", (), {"status_code": self.status_code, "request": request})()
                raise ai_engine.httpx.HTTPStatusError("provider error", request=request, response=response)

        def json(self):
            return self._payload

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        if len(calls) == 1:
            return Response(500, {"error": {"message": "schema unsupported"}})
        return Response(200, {"choices": [{"message": {"content": "```json\n{\"ok\":true}\n```"}}]})

    monkeypatch.setattr(ai_engine.httpx, "post", post)
    monkeypatch.setattr(ai_engine, "REMOTE_ENABLED", True)
    monkeypatch.setattr(ai_engine, "REMOTE_URL", "https://relay.example/v1")
    monkeypatch.setattr(ai_engine, "REMOTE_MODEL", "relay-model")
    monkeypatch.setattr(ai_engine, "REMOTE_API_KEY", "remote-secret")
    schema = {
        "$defs": {"Item": {"title": "Item", "type": "object", "properties": {
            "ok": {"type": "boolean", "default": False}}, "required": ["ok"]}},
        "type": "object", "properties": {"item": {"$ref": "#/$defs/Item"}},
        "required": ["item"], "title": "Result",
    }
    result = ai_engine.request_structured("return JSON", schema, "remote")
    assert result == {"ok": True}
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert "$defs" not in json.dumps(calls[0]["response_format"])
    assert calls[0]["response_format"]["json_schema"]["schema"]["properties"]["item"]["properties"]["ok"]["type"] == "boolean"
    assert calls[1]["response_format"] == {"type": "json_object"}


def test_remote_request_keeps_provider_error_when_json_object_fallback_fails(monkeypatch):
    class Response:
        status_code = 500

        def raise_for_status(self):
            request = type("Request", (), {"url": "https://relay.example/v1/chat/completions"})()
            response = type("ResponseError", (), {"status_code": 500, "request": request})()
            raise ai_engine.httpx.HTTPStatusError("provider error", request=request, response=response)

        def json(self):
            return {"error": {"message": "still unavailable"}}

    monkeypatch.setattr(ai_engine.httpx, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(ai_engine, "REMOTE_ENABLED", True)
    monkeypatch.setattr(ai_engine, "REMOTE_URL", "https://relay.example/v1")
    monkeypatch.setattr(ai_engine, "REMOTE_MODEL", "relay-model")
    monkeypatch.setattr(ai_engine, "REMOTE_API_KEY", "remote-secret")
    with pytest.raises(ai_engine.httpx.HTTPStatusError):
        ai_engine.request_structured("return JSON", {"type": "object"}, "remote")


def test_cli_availability_logs_missing_enabled_provider(monkeypatch):
    records = []
    monkeypatch.setattr(ai_engine, "CODEX_ENABLED", True)
    monkeypatch.setattr(ai_engine, "CODEX_COMMAND", "missing-codex-for-test")
    monkeypatch.setattr(ai_engine, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(ai_engine.shutil, "which", lambda command: None)
    monkeypatch.setattr(ai_engine.logger, "warning", lambda message, **kwargs: records.append((message, kwargs)))
    ai_engine.log_cli_availability("test-worker")
    assert records and records[0][0] == "ai.cli.unavailable"


def test_cli_availability_logs_present_enabled_provider_without_path(monkeypatch):
    records = []
    monkeypatch.setattr(ai_engine, "CODEX_ENABLED", True)
    monkeypatch.setattr(ai_engine, "CODEX_COMMAND", "/host/user/.local/bin/codex")
    monkeypatch.setattr(ai_engine, "CLAUDE_ENABLED", False)
    monkeypatch.setattr(ai_engine.shutil, "which", lambda _command: "/host/user/.local/bin/codex")
    monkeypatch.setattr(ai_engine.logger, "info", lambda message, **kwargs: records.append((message, kwargs)))
    ai_engine.log_cli_availability("test-worker")
    assert records and records[0][0] == "ai.cli.available"
    assert "/host/user" not in str(records[0])


def test_cli_availability_gauge_refreshes_when_settings_change(monkeypatch):
    monkeypatch.setattr(ai_engine.shutil, "which", lambda command: "/usr/local/bin/codex" if command == "codex" else None)
    original = {"codex_enabled": ai_engine.CODEX_ENABLED, "codex_command": ai_engine.CODEX_COMMAND,
                "codex_model": ai_engine.CODEX_MODEL, "claude_enabled": ai_engine.CLAUDE_ENABLED,
                "claude_command": ai_engine.CLAUDE_COMMAND, "claude_model": ai_engine.CLAUDE_MODEL}
    try:
        ai_engine.apply_config({"codex_enabled": True, "codex_command": "codex",
                                "claude_enabled": False, "claude_command": "claude"})
        metrics_output = ai_engine.metrics.render_prometheus()
        assert 'test_atelier_cli_enabled{provider="codex_cli"} 1' in metrics_output
        assert 'test_atelier_cli_available{provider="codex_cli"} 1' in metrics_output
        ai_engine.apply_config({"codex_enabled": False, "codex_command": "codex",
                                "claude_enabled": False, "claude_command": "claude"})
        metrics_output = ai_engine.metrics.render_prometheus()
        assert 'test_atelier_cli_enabled{provider="codex_cli"} 0' in metrics_output
        assert 'test_atelier_cli_available{provider="codex_cli"} 0' in metrics_output
    finally:
        ai_engine.apply_config(original)


def test_cli_worker_stop_cancels_child_process(tmp_path):
    script = tmp_path / "slow-cli"
    script.write_text("#!/bin/sh\nsleep 30\nprintf '{\"ok\":true}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    stop = threading.Event()
    ai_engine.set_cancel_event(stop)
    result = {}

    def invoke():
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        try:
            ai_engine._request_cli_structured("hello", schema, "codex_cli", str(script), "", timeout=30)
        except Exception as exc:  # noqa: BLE001 - assert the cancellation type below
            result["error"] = exc

    thread = threading.Thread(target=invoke)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5)
    ai_engine.set_cancel_event(None)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), ai_engine.CliCancelled)


def test_http_provider_stop_cancels_blocking_request(monkeypatch):
    """Provider shutdown must not wait for the full HTTP client timeout."""
    started = threading.Event()
    stop = threading.Event()
    result = {}

    def blocking_request():
        started.set()
        # Simulate a provider socket that does not observe our cancellation
        # event; the wrapper must still release the worker promptly.
        time.sleep(30)
        return object()

    ai_engine.set_cancel_event(stop)
    try:
        thread = threading.Thread(target=lambda: _capture_error(
            result, lambda: ai_engine._run_http_cancellable(blocking_request)), daemon=True)
        thread.start()
        assert started.wait(2)
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert isinstance(result.get("error"), ai_engine.CliCancelled)
    finally:
        ai_engine.set_cancel_event(None)


def _capture_error(target, operation):
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - test helper captures cancellation
        target["error"] = exc
