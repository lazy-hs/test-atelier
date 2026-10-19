import json

from scripts.production_smoke import SmokeCheck, main, run_smoke


GOOD_HEADERS = {
    "x-request-id": "smoke-request",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "content-security-policy": "default-src 'self'; object-src 'none'; script-src 'self'",
    "cache-control": "no-store",
}
GOOD_WEB_HEADERS = {**GOOD_HEADERS, "content-type": "text/html; charset=utf-8"}


def test_smoke_checks_health_metrics_and_worker_without_payloads():
    def fetch(url, headers, timeout):
        if url.endswith("/metrics"):
            if not headers:
                return 401, GOOD_HEADERS, ""
            assert headers == {"X-Metrics-Token": "secret-token"}
            return 200, GOOD_HEADERS, (
                "# HELP test_atelier_process_uptime_seconds\n"
                "test_atelier_process_uptime_seconds 4\n"
                "test_atelier_db_pool_checked_out 1\n"
                "test_atelier_cli_enabled{provider=\"codex_cli\"} 1\n"
                "test_atelier_cli_enabled{provider=\"claude_code\"} 0\n"
                "test_atelier_cli_available{provider=\"codex_cli\"} 1\n"
                "test_atelier_cli_available{provider=\"claude_code\"} 0\n"
                "test_atelier_worker_heartbeat_threshold_seconds 30"
            )
        if url.endswith("/health/worker"):
            return 200, GOOD_HEADERS, '{"status":"ready"}'
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", metrics_token="secret-token",
                       require_worker=True, attempts=1, fetch=fetch)
    assert all(item.status == "ok" for item in checks)
    assert {item.name for item in checks} == {"live", "ready", "metrics", "metrics-content", "metrics-auth", "worker"}


def test_smoke_verifies_expected_release_on_health_probes():
    def fetch(url, _headers, _timeout):
        if url.endswith("/health/live") or url.endswith("/health/ready"):
            return 200, GOOD_HEADERS, '{"status":"ok","release":"release-2026.10.18"}'
        if url.endswith("/health/worker"):
            return 200, GOOD_HEADERS, '{"status":"ready","worker_release":"release-2026.10.18"}'
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, (
                'test_atelier_process_uptime_seconds 4\n'
                'test_atelier_db_pool_checked_out 1\n'
                'test_atelier_cli_enabled{provider="codex_cli"} 0\n'
                'test_atelier_cli_enabled{provider="claude_code"} 0\n'
                'test_atelier_cli_available{provider="codex_cli"} 0\n'
                'test_atelier_cli_available{provider="claude_code"} 0\n'
                'test_atelier_worker_heartbeat_threshold_seconds 30'
            )
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", expected_release="release-2026.10.18",
                       require_worker=True, attempts=1, fetch=fetch)

    assert all(item.status == "ok" for item in checks)


def test_smoke_rejects_unexpected_release_on_health_probes():
    def fetch(url, _headers, _timeout):
        if url.endswith("/health/live") or url.endswith("/health/ready"):
            return 200, GOOD_HEADERS, '{"status":"ok","release":"old-build"}'
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", expected_release="new-build",
                       attempts=1, fetch=fetch)

    assert checks[0].name == "live"
    assert checks[0].status == "fail"
    assert "发布标识不匹配" in checks[0].detail


def test_smoke_rejects_worker_release_mismatch():
    def fetch(url, _headers, _timeout):
        if url.endswith("/health/live") or url.endswith("/health/ready"):
            return 200, GOOD_HEADERS, '{"status":"ok","release":"new-build"}'
        if url.endswith("/health/worker"):
            return 200, GOOD_HEADERS, '{"status":"ready","worker_release":"old-build"}'
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", expected_release="new-build",
                       require_worker=True, attempts=1, fetch=fetch)

    worker = next(item for item in checks if item.name == "worker")
    assert worker.status == "fail"
    assert "worker 发布标识不匹配" in worker.detail


def test_smoke_reports_missing_security_header():
    def fetch(_url, _headers, _timeout):
        return 200, {"x-request-id": "smoke-request"}, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    assert checks[0].status == "fail"
    assert "安全头" in checks[0].detail


def test_smoke_rejects_malformed_request_id():
    headers = {**GOOD_HEADERS, "x-request-id": "request id with spaces"}
    checks = run_smoke("http://example.test", attempts=1,
                       fetch=lambda *_args: (200, headers, ""))
    assert checks[0].status == "fail"
    assert "X-Request-ID" in checks[0].detail


def test_smoke_checks_edge_served_web_root():
    def fetch(url, _headers, _timeout):
        if url.endswith("/"):
            return 200, GOOD_WEB_HEADERS, "<!doctype html><html><body><div id=\"root\"></div></body></html>"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", require_web=True, attempts=1, fetch=fetch)
    assert checks[0].name == "web"
    assert checks[0].status == "ok"


def test_smoke_rejects_non_html_web_root():
    def fetch(url, _headers, _timeout):
        if url.endswith("/"):
            return 200, GOOD_HEADERS, "application response"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", require_web=True, attempts=1, fetch=fetch)
    assert checks[0].name == "web"
    assert checks[0].status == "fail"
    assert "HTML" in checks[0].detail


def test_smoke_rejects_malformed_web_request_id():
    headers = {**GOOD_WEB_HEADERS, "x-request-id": "request/id"}
    def fetch(url, _headers, _timeout):
        if url.endswith("/"):
            return 200, headers, "<!doctype html><html><div id=\"root\"></div></html>"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", require_web=True, attempts=1, fetch=fetch)
    assert checks[0].status == "fail"
    assert "X-Request-ID" in checks[0].detail


def test_smoke_reports_weak_content_security_policy():
    headers = {**GOOD_HEADERS, "content-security-policy": "default-src *"}
    checks = run_smoke("http://example.test", attempts=1,
                       fetch=lambda *_args: (200, headers, ""))
    assert checks[0].status == "fail"
    assert "Content-Security-Policy" in checks[0].detail


def test_smoke_reports_transient_network_failure():
    def fetch(_url, _headers, _timeout):
        raise RuntimeError("TimeoutError")

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    assert checks[0].status == "fail"
    assert "TimeoutError" in checks[0].detail


def test_smoke_requires_https_when_requested():
    checks = run_smoke("http://example.test", require_https=True, attempts=1,
                       fetch=lambda *_args: (200, GOOD_HEADERS, ""))
    assert len(checks) == 1
    assert checks[0].name == "https" and checks[0].status == "fail"


def test_smoke_requires_hsts_on_https_endpoint():
    checks = run_smoke("https://example.test", require_https=True, attempts=1,
                       fetch=lambda *_args: (200, GOOD_HEADERS, ""))

    assert checks[0].status == "fail"
    assert "Strict-Transport-Security" in checks[0].detail


def test_smoke_accepts_https_endpoint_with_hsts():
    headers = {**GOOD_HEADERS, "strict-transport-security": "max-age=31536000"}
    checks = run_smoke("https://example.test", require_https=True, attempts=1,
                       fetch=lambda *_args: (200, headers, ""))

    assert checks[0].status == "ok"


def test_smoke_reports_unprotected_metrics():
    def fetch(url, headers, timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, "# HELP test_atelier_process_uptime_seconds\n"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", metrics_token="secret-token", attempts=1, fetch=fetch)
    assert next(item for item in checks if item.name == "metrics-auth").status == "fail"


def test_smoke_detects_missing_cli_observability_metrics():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, "test_atelier_process_uptime_seconds 4"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "codex_cli" in content.detail


def test_smoke_rejects_empty_metrics_body():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, ""
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "为空" in content.detail


def test_smoke_rejects_malformed_or_nonfinite_metrics():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, (
                'test_atelier_process_uptime_seconds {bad} NaN\n'
                'test_atelier_cli_enabled{provider="codex_cli"} 0\n'
            )
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "格式无效" in content.detail


def test_smoke_rejects_sensitive_metric_text():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, "# HELP test_atelier_token leaked\n"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "敏感字段" in content.detail


def test_smoke_rejects_nonfinite_metric_values():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, "test_atelier_process_uptime_seconds NaN"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "非有限数值" in content.detail


def test_smoke_accepts_route_labels_with_template_braces():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, (
                'test_atelier_process_uptime_seconds 4\n'
                'test_atelier_db_pool_checked_out 0\n'
                'test_atelier_requests_total{method="GET",route="/api/v1/projects/{project_id}",status="200"} 1\n'
                'test_atelier_requests_total{method="GET",route="/api/v1/projects/{project_id}/documents",status="200"} 1\n'
                'test_atelier_requests_total{method="GET",route="/api/v1/ai/jobs/{job_id}/prompt",status="200"} 1\n'
                'test_atelier_cli_enabled{provider="codex_cli"} 0\n'
                'test_atelier_cli_enabled{provider="claude_code"} 0\n'
                'test_atelier_cli_available{provider="codex_cli"} 0\n'
                'test_atelier_cli_available{provider="claude_code"} 0\n'
                'test_atelier_worker_heartbeat_threshold_seconds 30'
            )
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "ok"


def test_smoke_rejects_duplicate_metric_samples():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            sample = 'test_atelier_process_uptime_seconds 4'
            return 200, GOOD_HEADERS, f"{sample}\n{sample}"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "重复" in content.detail


def test_smoke_rejects_oversized_metric_samples():
    def fetch(url, _headers, _timeout):
        if url.endswith("/metrics"):
            return 200, GOOD_HEADERS, "test_atelier_process_uptime_seconds{label=\"" + ("x" * 70_000) + "\"} 4"
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", attempts=1, fetch=fetch)
    content = next(item for item in checks if item.name == "metrics-content")
    assert content.status == "fail"
    assert "超过" in content.detail


def test_smoke_rejects_disabled_worker_when_required():
    def fetch(url, _headers, _timeout):
        if url.endswith("/health/worker"):
            return 200, GOOD_HEADERS, '{"status":"disabled"}'
        return 200, GOOD_HEADERS, ""

    checks = run_smoke("http://example.test", require_worker=True, attempts=1, fetch=fetch)
    worker = next(item for item in checks if item.name == "worker")
    assert worker.status == "fail"
    assert "disabled" in worker.detail


def test_smoke_jsonl_emits_safe_log_contract(monkeypatch, capsys):
    monkeypatch.setattr("scripts.production_smoke.run_smoke", lambda *args, **kwargs: [
        SmokeCheck("live", "ok", "HTTP 200"),
        SmokeCheck("ready", "fail", "upstream URL https://user:secret@example.test"),
        SmokeCheck("metrics", "warn", "optional check"),
    ])

    assert main(["--jsonl"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["event"] == "production.smoke.failed"
    assert payload["request_id"] == "-"
    assert payload["failed_checks"] == ["ready"]
    assert payload["warning_count"] == 1
    assert payload["checks"] == [
        {"name": "live", "status": "ok"},
        {"name": "ready", "status": "fail"},
        {"name": "metrics", "status": "warn"},
    ]
    assert "secret" not in output


def test_smoke_jsonl_serializes_operator_cancellation(monkeypatch, capsys):
    monkeypatch.setattr("scripts.production_smoke.run_smoke", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert main(["--jsonl"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "production.smoke.cancelled"
    assert payload["request_id"] == "-"
    assert payload["checks"] == []
