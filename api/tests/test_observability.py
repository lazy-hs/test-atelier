import asyncio
import ast
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

os.environ.setdefault("AI_STUDIO_SESSION_SECRET", "test-only-session-secret-32-characters-long")

from api.observability import (FailureWindow, JsonFormatter, SafeTextFormatter, configure_logging,
                               deployment_release, env_float, env_int, new_request_id,
                               sanitized_subprocess_environment)


def test_environment_numeric_helpers_use_safe_defaults(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_INT", "not-a-number")
    monkeypatch.setenv("TEST_ATELIER_FLOAT", "Infinity")

    assert env_int("TEST_ATELIER_INT", 7, 1) == 7
    assert env_float("TEST_ATELIER_FLOAT", 2.5, 1.0) == 2.5


def test_environment_numeric_helpers_apply_lower_bounds(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_INT", "0")
    monkeypatch.setenv("TEST_ATELIER_FLOAT", "0.1")

    assert env_int("TEST_ATELIER_INT", 7, 1) == 1
    assert env_float("TEST_ATELIER_FLOAT", 2.5, 1.0) == 1.0


def test_subprocess_environment_never_preserves_application_secrets(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    environment = sanitized_subprocess_environment(
        preserve_secret_names={"AI_STUDIO_SESSION_SECRET", "OPENAI_API_KEY"}
    )
    assert "AI_STUDIO_SESSION_SECRET" not in environment
    assert environment["OPENAI_API_KEY"] == "provider-secret"


def test_deployment_release_is_bounded_and_log_safe(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "build 2026/10?token=secret")
    release = deployment_release()
    assert release == "build_2026/10_token_REDACTED_"
    assert len(release) <= 128


def test_json_formatter_includes_optional_deployment_release(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "git-abc123")
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "started", (), None)
    record.event = "application.started"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["release"] == "git-abc123"


def test_configure_logging_routes_dependency_records_to_json(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    root = logging.getLogger()
    handler = next(item for item in root.handlers if getattr(item, "test_atelier_json_handler", False))
    record = logging.LogRecord("sqlalchemy.engine", logging.WARNING, __file__, 1,
                               "dependency failed password=hunter2", (), None)
    payload = json.loads(handler.format(record))
    assert payload["logger"] == "sqlalchemy.engine"
    assert payload["message"] == "dependency failed password=[REDACTED]"
    assert payload["schema_version"] == 1
    # Low-level dependency logs must not re-enable SQL/HTTP payloads when an
    # operator turns on DEBUG for the application logger.
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING


def test_json_formatter_includes_worker_release_metadata():
    record = logging.LogRecord("worker", logging.INFO, __file__, 1, "heartbeat", (), None)
    record.event = "worker.heartbeat"
    record.worker_release = "worker-build-42"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["worker_release"] == "worker-build-42"


def test_json_formatter_includes_worker_error_log_interval():
    record = logging.LogRecord("worker", logging.INFO, __file__, 1, "started", (), None)
    record.event = "worker.started"
    record.error_log_interval_seconds = 30.0
    payload = json.loads(JsonFormatter().format(record))
    assert payload["error_log_interval_seconds"] == 30.0


def test_json_formatter_keeps_database_kind_for_startup_diagnostics():
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "migration checked", (), None)
    record.event = "database.migrations.checked"
    record.database_kind = "postgresql"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["database_kind"] == "postgresql"


def test_health_payload_exposes_configured_release(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "git-abc123")
    assert main.health_live()["release"] == "git-abc123"


def test_metrics_never_render_non_finite_values():
    from api import metrics

    metrics.reset_for_tests()
    metrics.set_worker_health(True, float("inf"))
    metrics.observe_request("GET", "/health", 200, float("nan"))
    metrics.observe_model("local", "success", float("inf"))

    output = metrics.render_prometheus()
    assert " inf" not in output.lower()
    assert " nan" not in output.lower()
from api import metrics
from api import ai_jobs, main


def test_request_id_accepts_safe_values_and_replaces_untrusted_input():
    assert new_request_id("gateway-123") == "gateway-123"
    generated = new_request_id("bad value\nwith-injection")
    assert generated != "bad value\nwith-injection"
    assert len(generated) == 32


def test_failure_window_blocks_and_expires_without_growing_for_unknown_keys():
    limiter = FailureWindow(limit=2, window_seconds=10, max_keys=3)
    keys = ("client-a", "user-a")
    assert limiter.blocked(keys, now=100) is False
    limiter.record(keys, now=100)
    limiter.record(keys, now=101)
    assert limiter.blocked(keys, now=101) is True
    assert limiter.retry_after(keys, now=101) == 9
    assert limiter.blocked(keys, now=111) is False
    for index in range(20):
        assert limiter.blocked((f"unknown-{index}",), now=200 + index) is False
    assert len(limiter._entries) <= 2


def test_production_runtime_requires_http_security(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_ENV", "production")
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret-123456")
    with pytest.raises(main.CookieSecureRequired, match="COOKIE_SECURE"):
        main._validate_runtime_security()

    monkeypatch.setenv("COOKIE_SECURE", "1")
    monkeypatch.setenv("METRICS_TOKEN", "short")
    with pytest.raises(main.MetricsTokenRequired, match="METRICS_TOKEN"):
        main._validate_runtime_security()

    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret-123456")
    main._validate_runtime_security()


def test_production_runtime_requires_structured_logs(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_ENV", "production")
    monkeypatch.setenv("COOKIE_SECURE", "1")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret-123456")
    monkeypatch.setenv("LOG_FORMAT", "text")
    with pytest.raises(main.ProductionConfigurationError, match="LOG_FORMAT=json"):
        main._validate_runtime_security()

    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(main.ProductionConfigurationError, match="LOG_LEVEL"):
        main._validate_runtime_security()


def test_production_runtime_requires_explicit_migrations():
    with pytest.raises(main.ExplicitMigrationsRequired, match="AI_STUDIO_REQUIRE_MIGRATIONS"):
        main._validate_migration_policy("production", False)
    main._validate_migration_policy("production", True)
    main._validate_migration_policy("development", False)


def test_json_formatter_omits_payload_like_extra_fields():
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "request.completed", (), None)
    record.event = "request.completed"
    record.method = "POST"
    record.path = "/api/v1/projects/1/documents"
    record.status = 200
    record.body = "uploaded requirement contents"
    record.prompt = "model prompt"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["schema_version"] == 1
    assert payload["event"] == "request.completed"
    assert payload["status"] == 200
    assert "body" not in payload
    assert "prompt" not in payload


def test_static_logger_extra_keys_are_formatter_allowlisted():
    """Prevent a new log field from silently disappearing in production."""
    root = Path(__file__).resolve().parents[2]
    logger_methods = {"debug", "info", "warning", "error", "exception", "log"}
    keys: set[str] = set()
    for package in (root / "api", root / "scripts"):
        for path in package.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in logger_methods:
                    continue
                extra = next((item.value for item in node.keywords if item.arg == "extra"), None)
                if not isinstance(extra, ast.Dict):
                    continue
                keys.update(key.value for key in extra.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
    assert keys - set(JsonFormatter._FIELDS) == set()


def test_json_formatter_bounds_selected_string_fields():
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "completed", (), None)
    record.event = "x" * 500
    record.source = "y" * 500
    payload = json.loads(JsonFormatter().format(record))
    assert len(payload["event"]) == 256
    assert len(payload["source"]) == 256


def test_json_formatter_bounds_redaction_input_before_serializing():
    record = logging.LogRecord("api", logging.WARNING, __file__, 1, "x" * 200_000, (), None)
    payload = json.loads(JsonFormatter().format(record))
    assert len(payload["message"]) == 4_000


def test_json_formatter_includes_bounded_domain_audit_fields():
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "suggestion.decided", (), None)
    record.event = "suggestion.decided"
    record.suggestion_id = 12
    record.case_version = 4
    record.action = "accepted"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["suggestion_id"] == 12
    assert payload["case_version"] == 4
    assert payload["action"] == "accepted"


def test_json_formatter_keeps_non_finite_numbers_valid_json():
    record = logging.LogRecord("api", logging.INFO, __file__, 1, "completed", (), None)
    record.duration_ms = math.nan
    record.worker_age_seconds = math.inf
    payload = json.loads(JsonFormatter().format(record))
    assert payload["duration_ms"] is None
    assert payload["worker_age_seconds"] is None


def test_json_formatter_bounds_selected_collections_and_redacts_values():
    record = logging.LogRecord("api", logging.WARNING, __file__, 1, "failed", (), None)
    record.failed_checks = ["token=secret"] * 100
    payload = json.loads(JsonFormatter().format(record))
    assert len(payload["failed_checks"]) == 32
    assert payload["failed_checks"][0] == "token=[REDACTED]"


def test_metrics_use_route_templates_and_prometheus_labels():
    metrics.reset_for_tests()
    metrics.begin_request()
    metrics.observe_request("GET", "/api/v1/projects/{project_id}", 200, 12.5)
    metrics.begin_request()
    metrics.observe_request("GET", "/api/v1/projects/{project_id}", 404, 4)
    output = metrics.render_prometheus()
    assert 'route="/api/v1/projects/{project_id}"' in output
    assert 'status="200"} 1' in output
    assert 'status="404"} 1' in output
    assert "test_atelier_inflight_requests 0" in output


def test_metrics_bound_unknown_http_methods():
    metrics.reset_for_tests()
    metrics.begin_request()
    metrics.observe_request("X-custom-attacker-method", "/api/v1/health/live", 200, 1)
    output = metrics.render_prometheus()
    assert 'method="<other>"' in output
    assert "X-custom-attacker-method" not in output


def test_metrics_expose_ai_queue_utilization():
    metrics.reset_for_tests()
    metrics.set_ai_queue(2, 100)
    output = metrics.render_prometheus()
    assert "test_atelier_ai_jobs_active 2" in output
    assert "test_atelier_ai_jobs_capacity 100" in output


def test_metrics_expose_worker_heartbeat_health():
    metrics.reset_for_tests()
    metrics.set_worker_health(True, 2.5)
    metrics.set_worker_heartbeat_threshold(45)
    output = metrics.render_prometheus()
    assert "test_atelier_worker_up 1" in output
    assert "test_atelier_worker_heartbeat_age_seconds 2.500" in output
    assert "test_atelier_worker_heartbeat_threshold_seconds 45.000" in output


def test_metrics_expose_database_pool_utilization():
    metrics.reset_for_tests()
    metrics.set_database_pool(5, 3, 1)
    output = metrics.render_prometheus()
    assert "test_atelier_db_pool_size 5" in output
    assert "test_atelier_db_pool_checked_out 3" in output
    assert "test_atelier_db_pool_overflow 1" in output


def test_metrics_expose_bounded_model_outcomes():
    metrics.reset_for_tests()
    metrics.observe_model("codex_cli", "success", 1250)
    metrics.observe_model("codex_cli", "timeout", 500)
    metrics.observe_model("unknown-provider", "timeout", 500)
    output = metrics.render_prometheus()
    assert 'provider="codex_cli",outcome="success"} 1' in output
    assert 'provider="codex_cli",outcome="timeout"} 1' in output
    assert 'provider="other",outcome="failure"} 1' in output
    assert 'provider="codex_cli",outcome="success"} 1.250000' in output
    assert "unknown-provider" not in output


def test_metrics_expose_cli_availability_without_path_details():
    metrics.reset_for_tests()
    metrics.set_cli_availability("codex_cli", True, enabled=True)
    metrics.set_cli_availability("claude_code", False, enabled=False)
    metrics.set_cli_availability("/user/supplied/path", True)
    output = metrics.render_prometheus()
    assert 'test_atelier_cli_enabled{provider="codex_cli"} 1' in output
    assert 'test_atelier_cli_enabled{provider="claude_code"} 0' in output
    assert 'test_atelier_cli_available{provider="codex_cli"} 1' in output
    assert 'test_atelier_cli_available{provider="claude_code"} 0' in output
    assert "/user/supplied/path" not in output


def test_json_formatter_hides_traceback_by_default_and_redacts_secrets(monkeypatch):
    try:
        raise RuntimeError("authorization=Bearer-secret password=hunter2")
    except RuntimeError:
        record = logging.LogRecord("api", logging.ERROR, __file__, 1, "failed", (), None)
        record.exc_info = __import__("sys").exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["exception_type"] == "RuntimeError"
    assert "exception" not in payload
    monkeypatch.setenv("LOG_INCLUDE_TRACEBACK", "1")
    payload = json.loads(JsonFormatter().format(record))
    assert "Bearer-secret" not in payload["exception"]
    assert "hunter2" not in payload["exception"]


def test_json_formatter_traceback_omits_exception_message_payload(monkeypatch):
    monkeypatch.setenv("LOG_INCLUDE_TRACEBACK", "1")
    message = "model output contains sensitive requirement text"
    try:
        raise RuntimeError(message)
    except RuntimeError:
        record = logging.LogRecord("api", logging.ERROR, __file__, 1, "failed", (), None)
        record.exc_info = __import__("sys").exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["exception_type"] == "RuntimeError"
    assert "sensitive requirement text" not in payload["exception"]


def test_json_formatter_redacts_bearer_and_openai_style_tokens():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        "provider failed Bearer abc.def-ghi sk-1234567890abcdef", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "abc.def-ghi" not in payload["message"]
    assert "sk-1234567890abcdef" not in payload["message"]
    assert "[REDACTED]" in payload["message"]


def test_json_formatter_redacts_bearer_tokens_after_authorization_label():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        "provider response Authorization: Bearer abc.def-ghi", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "abc.def-ghi" not in payload["message"]


def test_json_formatter_redacts_uppercase_environment_credentials():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1, "env OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz AWS_SECRET_ACCESS_KEY=secret", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in payload["message"]
    assert "AWS_SECRET_ACCESS_KEY=secret" not in payload["message"]
    assert "OPENAI_API_KEY=[REDACTED]" in payload["message"]
    assert payload["message"].count("[REDACTED]") == 2


def test_json_formatter_redacts_common_cloud_tokens_and_jwt():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        "credentials AKIAIOSFODNN7EXAMPLE ghp_123456789012345678901234567890 "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature123456 "
        "-----BEGIN PRIVATE KEY-----\nsecret-key-material\n-----END PRIVATE KEY-----",
        (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    for secret in ("AKIAIOSFODNN7EXAMPLE", "ghp_123456789012345678901234567890", "eyJhbGciOiJIUzI1NiJ9"):
        assert secret not in payload["message"]
    assert "secret-key-material" not in payload["message"]
    assert "PRIVATE_KEY_REDACTED" in payload["message"]


def test_json_formatter_redacts_quoted_json_credentials():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        'provider response {"api_key":"sk-json-secret-123456", "set-cookie":"session=p@ss", "password":"p@ss"}', (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "sk-json-secret-123456" not in payload["message"]
    assert "p@ss" not in payload["message"]
    assert payload["message"].count("[REDACTED]") == 3


def test_json_formatter_redacts_quoted_generic_tokens():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        'provider response {"token":"session-token", "access_token":"access-secret"}', (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "session-token" not in payload["message"]
    assert "access-secret" not in payload["message"]
    assert payload["message"].count("[REDACTED]") == 2


def test_json_formatter_redacts_cookie_headers_and_cookie_query_values():
    record = logging.LogRecord(
        "httpx", logging.WARNING, __file__, 1,
        "request Cookie: session=signed-session; csrf=csrf-secret "
        "https://example.test/callback?cookie=tracking-secret&attempt=2",
        (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "signed-session" not in payload["message"]
    assert "csrf-secret" not in payload["message"]
    assert "tracking-secret" not in payload["message"]


def test_json_formatter_redacts_database_url_credentials():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        "database connection failed postgresql+psycopg://app:db-secret@db:5432/atelier", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "db-secret" not in payload["message"]
    assert "postgresql+psycopg://app:[REDACTED]@db:5432/atelier" in payload["message"]


def test_json_formatter_redacts_bare_and_query_tokens_without_eating_other_fields():
    record = logging.LogRecord(
        "api", logging.WARNING, __file__, 1,
        "provider https://model.test/run?token=private-token&attempt=2 token=another-secret",
        (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "private-token" not in payload["message"]
    assert "another-secret" not in payload["message"]
    assert "attempt=2" in payload["message"]


def test_json_formatter_does_not_allow_framework_traceback_in_message(monkeypatch):
    monkeypatch.delenv("LOG_INCLUDE_TRACEBACK", raising=False)
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1,
        "Traceback (most recent call last):\nRuntimeError: password=hunter2", (), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "Traceback" not in payload["message"]
    assert "hunter2" not in payload["message"]


def test_text_formatter_uses_the_same_redaction_policy(monkeypatch):
    record = logging.LogRecord(
        "api", logging.ERROR, __file__, 1,
        "provider failed token=private-token", (), None,
    )
    try:
        raise RuntimeError("password=hunter2")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()
    rendered = SafeTextFormatter().format(record)
    assert "private-token" not in rendered
    assert "hunter2" not in rendered
    assert "RuntimeError" in rendered


def test_configure_logging_is_idempotent():
    logger = configure_logging()
    configure_logging()
    marked = [handler for handler in logger.handlers if getattr(handler, "test_atelier_json_handler", False)]
    assert len(marked) == 1


def test_production_logging_forces_json_for_invalid_format(monkeypatch):
    logger = logging.getLogger("api")
    uvicorn_logger = logging.getLogger("uvicorn.error")
    original_handlers = list(logger.handlers)
    original_uvicorn_handlers = list(uvicorn_logger.handlers)
    try:
        logger.handlers.clear()
        uvicorn_logger.handlers.clear()
        monkeypatch.setenv("AI_STUDIO_ENV", "production")
        monkeypatch.setenv("LOG_FORMAT", "text")
        configure_logging()
        marked = [handler for handler in logger.handlers if getattr(handler, "test_atelier_json_handler", False)]
        assert len(marked) == 1
        assert isinstance(marked[0].formatter, JsonFormatter)
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            logger.addHandler(handler)
        for handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(handler)
            handler.close()
        for handler in original_uvicorn_handlers:
            uvicorn_logger.addHandler(handler)


def test_configure_logging_replaces_uvicorn_text_sink(monkeypatch):
    uvicorn_logger = logging.getLogger("uvicorn.error")
    original_handlers = list(uvicorn_logger.handlers)
    try:
        uvicorn_logger.handlers.clear()
        uvicorn_logger.addHandler(logging.StreamHandler())
        configure_logging()
        marked = [handler for handler in uvicorn_logger.handlers if getattr(handler, "test_atelier_json_handler", False)]
        assert len(marked) == 1
        assert all(getattr(handler, "test_atelier_json_handler", False) for handler in uvicorn_logger.handlers)
    finally:
        for handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            uvicorn_logger.addHandler(handler)


def test_ai_submission_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(ai_jobs, "_submission_slots", threading.BoundedSemaphore(0))
    assert ai_jobs.submit_worker("queue-test", lambda _job_id: None) is False


def test_ai_submission_deduplicates_active_job(monkeypatch):
    monkeypatch.setattr(ai_jobs, "_active_jobs", {"already-running"})
    monkeypatch.setattr(ai_jobs, "_accepting", True)
    assert ai_jobs.submit_worker("already-running", lambda _job_id: None) is True


def test_shutdown_cancels_queued_jobs_without_locking_cleanup(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(ai_jobs, "executor", executor)
    monkeypatch.setattr(ai_jobs, "_accepting", True)
    monkeypatch.setattr(ai_jobs, "_active_jobs", set())
    monkeypatch.setattr(ai_jobs, "_futures", {})
    monkeypatch.setattr(ai_jobs, "_submission_slots", threading.BoundedSemaphore(2))
    assert ai_jobs.submit_worker("running", lambda _job_id: (started.set(), release.wait(2))) is True
    assert started.wait(1)
    assert ai_jobs.submit_worker("queued", lambda _job_id: None) is True
    began = time.monotonic()
    assert ai_jobs.shutdown_workers(0.05) == 1
    assert time.monotonic() - began < 1
    release.set()
    time.sleep(0.05)


def test_shutdown_signals_embedded_workers(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    monkeypatch.setattr(ai_jobs, "executor", executor)
    monkeypatch.setattr(ai_jobs, "_accepting", True)
    monkeypatch.setattr(ai_jobs, "_active_jobs", set())
    monkeypatch.setattr(ai_jobs, "_futures", {})
    monkeypatch.setattr(ai_jobs, "_submission_slots", threading.BoundedSemaphore(1))
    ai_jobs.start_workers()
    assert ai_jobs.submit_worker("embedded-cli", lambda _job_id: (started.set(), ai_jobs._shutdown_event.wait(2)))
    assert started.wait(1)
    assert ai_jobs.shutdown_workers(0.5) == 0


def test_lifespan_cleans_embedded_workers_when_startup_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "initialize", lambda: None)
    monkeypatch.setattr(ai_jobs, "start_workers", lambda: None)
    monkeypatch.setattr(ai_jobs, "recover_jobs", lambda: (_ for _ in ()).throw(RuntimeError("recovery failed")))
    monkeypatch.setattr(ai_jobs, "shutdown_workers", lambda timeout: calls.append(timeout) or 0)

    async def run_lifespan():
        with pytest.raises(RuntimeError, match="recovery failed"):
            async with main.lifespan(main.app):
                pass

    asyncio.run(run_lifespan())
    assert calls
