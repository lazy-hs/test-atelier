#!/usr/bin/env python3
"""Run a read-only smoke check against a running Test Atelier deployment.

This intentionally uses only the standard library so it can run from an
operator workstation or a deployment job without installing application
dependencies. It never submits a document or AI task and never prints
response bodies or metric tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    status: str
    detail: str


Response = tuple[int, dict[str, str], str]
Fetcher = Callable[[str, dict[str, str], float], Response]
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PROMETHEUS_SAMPLE_PATTERN = re.compile(
    # Label values are quoted strings and may legitimately contain braces,
    # for example the FastAPI route template ``{project_id}``. Keep the
    # closing sample brace distinct from braces inside quoted values while
    # still rejecting unquoted nested braces.
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>(?:[^"{}\n]|"(?:\\.|[^"\\\n])*")*)\})?\s+'
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:Inf|NaN))"
    r"(?:\s+\d+)?$"
)
_SENSITIVE_METRIC_TEXT_PATTERN = re.compile(
    r"(?i)(?:password|secret|token|api[_-]?key|authorization|cookie|prompt|document)"
)
_ROUTE_LABEL_PATTERN = re.compile(r'(?i)route="(?:\\.|[^"\\])*"')
_MAX_METRIC_LINE_BYTES = 64 * 1024
_MAX_METRIC_LINES = 10_000


def http_get(url: str, headers: dict[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(256 * 1024).decode("utf-8", errors="replace")
            return response.status, {key.lower(): value for key, value in response.headers.items()}, body
    except urllib.error.HTTPError as exc:
        return exc.code, {key.lower(): value for key, value in exc.headers.items()}, ""
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(type(exc).__name__) from exc


def _request_with_retries(fetch: Fetcher, url: str, headers: dict[str, str], timeout: float,
                          attempts: int, retry_seconds: float) -> Response:
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            return fetch(url, headers, timeout)
        except RuntimeError as exc:
            last_error = exc
            if index + 1 < attempts:
                time.sleep(retry_seconds)
    raise RuntimeError(str(last_error or "request failed"))


def _validate_metrics_body(body: str, required_markers: tuple[str, ...]) -> str | None:
    """Validate the bounded Prometheus exposition returned by a deployment."""
    if not body:
        return "metrics 响应为空"
    samples = 0
    seen_samples: set[str] = set()
    if len(body.splitlines()) > _MAX_METRIC_LINES:
        return f"metrics 样本行超过 {_MAX_METRIC_LINES} 行"
    for line_number, raw_line in enumerate(body.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if len(raw_line.encode("utf-8", errors="replace")) > _MAX_METRIC_LINE_BYTES:
            return f"metrics 第 {line_number} 行超过 {_MAX_METRIC_LINE_BYTES} 字节"
        # Route labels are low-sensitivity metric dimensions and can contain
        # endpoint names such as ``/documents`` or ``/prompt``. Inspect the
        # metric name, HELP text, and other labels without treating a route
        # path as a leaked payload marker.
        metric_text = _ROUTE_LABEL_PATTERN.sub('route=""', line)
        if _SENSITIVE_METRIC_TEXT_PATTERN.search(metric_text):
            return f"metrics 第 {line_number} 行包含敏感字段"
        if line.startswith("#"):
            continue
        match = _PROMETHEUS_SAMPLE_PATTERN.fullmatch(line)
        if match is None:
            return f"metrics 第 {line_number} 行格式无效"
        if match.group("value").casefold() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf"}:
            return f"metrics 第 {line_number} 行包含非有限数值"
        if line in seen_samples:
            return f"metrics 第 {line_number} 行重复"
        seen_samples.add(line)
        samples += 1
    if samples == 0:
        return "metrics 未返回任何样本"
    missing = [marker for marker in required_markers if marker not in body]
    if missing:
        return f"缺少指标：{', '.join(missing)}"
    return None


def run_smoke(base_url: str, *, metrics_token: str = "", expected_release: str = "",
              require_worker: bool = False, require_web: bool = False, require_https: bool = False,
              timeout: float = 5.0,
              attempts: int = 3, retry_seconds: float = 1.0,
              fetch: Fetcher = http_get) -> list[SmokeCheck]:
    base = base_url.rstrip("/")
    checks: list[SmokeCheck] = []
    if require_https and urlparse(base).scheme.lower() != "https":
        return [SmokeCheck("https", "fail", "生产 smoke 要求 HTTPS 地址")]

    def check_endpoint(name: str, path: str, expected: int = 200, headers: dict[str, str] | None = None,
                       body_check: Callable[[str], str | None] | None = None) -> Response | None:
        try:
            status, response_headers, body = _request_with_retries(
                fetch, base + path, headers or {}, timeout, max(1, attempts), max(0.0, retry_seconds))
        except RuntimeError as exc:
            checks.append(SmokeCheck(name, "fail", f"请求失败：{exc}"))
            return None
        if status != expected:
            checks.append(SmokeCheck(name, "fail", f"HTTP {status}，期望 {expected}"))
            return None
        request_id = response_headers.get("x-request-id", "")
        if not request_id:
            checks.append(SmokeCheck(name, "fail", "响应缺少 X-Request-ID"))
            return None
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            checks.append(SmokeCheck(name, "fail", "响应 X-Request-ID 格式无效"))
            return None
        if response_headers.get("x-content-type-options", "").lower() != "nosniff":
            checks.append(SmokeCheck(name, "fail", "响应缺少 nosniff 安全头"))
            return None
        if response_headers.get("x-frame-options", "").upper() != "DENY":
            checks.append(SmokeCheck(name, "fail", "响应缺少 DENY frame 安全头"))
            return None
        required_headers = {
            "referrer-policy": "strict-origin-when-cross-origin",
            "permissions-policy": "camera=(), microphone=(), geolocation=()",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-resource-policy": "same-origin",
        }
        for header, expected in required_headers.items():
            if response_headers.get(header, "").lower() != expected:
                checks.append(SmokeCheck(name, "fail", f"响应缺少或错误的 {header} 安全头"))
                return None
        csp = response_headers.get("content-security-policy", "").lower()
        if "default-src 'self'" not in csp or "object-src 'none'" not in csp or "script-src 'self'" not in csp:
            checks.append(SmokeCheck(name, "fail", "响应缺少或错误的 Content-Security-Policy 安全头"))
            return None
        if response_headers.get("cache-control", "").lower() != "no-store":
            checks.append(SmokeCheck(name, "fail", "API 响应缺少 no-store 缓存策略"))
            return None
        if require_https and not response_headers.get("strict-transport-security", ""):
            checks.append(SmokeCheck(name, "fail", "HTTPS 响应缺少 Strict-Transport-Security"))
            return None
        if body_check is not None:
            body_error = body_check(body)
            if body_error:
                checks.append(SmokeCheck(name, "fail", body_error))
                return None
        checks.append(SmokeCheck(name, "ok", f"HTTP {status}"))
        return status, response_headers, body

    def check_web_root() -> None:
        """Check the edge-served SPA without requiring API cache headers."""
        try:
            status, response_headers, body = _request_with_retries(
                fetch, base + "/", {}, timeout, max(1, attempts), max(0.0, retry_seconds))
        except RuntimeError as exc:
            checks.append(SmokeCheck("web", "fail", f"请求失败：{exc}"))
            return
        if status != 200:
            checks.append(SmokeCheck("web", "fail", f"HTTP {status}，期望 200"))
            return
        web_request_id = response_headers.get("x-request-id", "")
        if not web_request_id:
            checks.append(SmokeCheck("web", "fail", "响应缺少 X-Request-ID"))
            return
        if not _REQUEST_ID_PATTERN.fullmatch(web_request_id):
            checks.append(SmokeCheck("web", "fail", "响应 X-Request-ID 格式无效"))
            return
        if not response_headers.get("content-type", "").lower().startswith("text/html"):
            checks.append(SmokeCheck("web", "fail", "Web 根路径不是 HTML 响应"))
            return
        if response_headers.get("x-content-type-options", "").lower() != "nosniff":
            checks.append(SmokeCheck("web", "fail", "Web 响应缺少 nosniff 安全头"))
            return
        if response_headers.get("x-frame-options", "").upper() != "DENY":
            checks.append(SmokeCheck("web", "fail", "Web 响应缺少 DENY frame 安全头"))
            return
        required_headers = {
            "referrer-policy": "strict-origin-when-cross-origin",
            "permissions-policy": "camera=(), microphone=(), geolocation=()",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-resource-policy": "same-origin",
        }
        for header, expected in required_headers.items():
            if response_headers.get(header, "").lower() != expected:
                checks.append(SmokeCheck("web", "fail", f"Web 响应缺少或错误的 {header} 安全头"))
                return
        csp = response_headers.get("content-security-policy", "").lower()
        if "default-src 'self'" not in csp or "object-src 'none'" not in csp or "script-src 'self'" not in csp:
            checks.append(SmokeCheck("web", "fail", "Web 响应缺少或错误的 Content-Security-Policy 安全头"))
            return
        if "<html" not in body.lower() and 'id="root"' not in body.lower():
            checks.append(SmokeCheck("web", "fail", "Web 根路径未返回 SPA 文档"))
            return
        if require_https and not response_headers.get("strict-transport-security", ""):
            checks.append(SmokeCheck("web", "fail", "HTTPS 响应缺少 Strict-Transport-Security"))
            return
        checks.append(SmokeCheck("web", "ok", "HTTP 200"))

    def release_body_check(body: str) -> str | None:
        """Verify that health probes report the release being smoke-tested."""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return "健康响应不是有效 JSON"
        observed = payload.get("release", "")
        if observed != expected_release:
            return f"发布标识不匹配：实际 {observed or '空'}，期望 {expected_release}"
        return None

    if require_web:
        check_web_root()
    health_body_check = release_body_check if expected_release else None
    check_endpoint("live", "/api/v1/health/live", body_check=health_body_check)
    check_endpoint("ready", "/api/v1/health/ready", body_check=health_body_check)
    metrics_headers = {"X-Metrics-Token": metrics_token} if metrics_token else {}
    metrics_response = check_endpoint("metrics", "/api/v1/metrics", headers=metrics_headers)
    if metrics_token:
        try:
            status, _, _ = _request_with_retries(fetch, base + "/api/v1/metrics", {}, timeout,
                                                  max(1, attempts), max(0.0, retry_seconds))
            if status not in {401, 403}:
                checks.append(SmokeCheck("metrics-auth", "fail", f"未授权 metrics 返回 HTTP {status}，期望 401 或 403"))
            else:
                checks.append(SmokeCheck("metrics-auth", "ok", f"未授权请求返回 HTTP {status}"))
        except RuntimeError as exc:
            checks.append(SmokeCheck("metrics-auth", "fail", f"未授权 metrics 请求失败：{exc}"))
    if metrics_response is not None:
        # The standard-library fetcher deliberately avoids reading successful
        # bodies. Custom deployment fetchers may return the exposition text.
        body = metrics_response[2]
        required_metrics = (
            "test_atelier_process_uptime_seconds",
            "test_atelier_db_pool_checked_out",
            'test_atelier_cli_enabled{provider="codex_cli"}',
            'test_atelier_cli_enabled{provider="claude_code"}',
            'test_atelier_cli_available{provider="codex_cli"}',
            'test_atelier_cli_available{provider="claude_code"}',
            "test_atelier_worker_heartbeat_threshold_seconds",
        )
        metrics_error = _validate_metrics_body(body, required_metrics)
        if metrics_error:
            checks.append(SmokeCheck("metrics-content", "fail", metrics_error))
        else:
            checks.append(SmokeCheck("metrics-content", "ok", "Prometheus 指标可解析"))
    if require_worker:
        def worker_body_check(body: str) -> str | None:
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                return "worker 健康响应不是有效 JSON"
            if payload.get("status") != "ready":
                return f"worker 状态为 {payload.get('status', 'unknown')}，期望 ready"
            if expected_release and payload.get("worker_release") != expected_release:
                observed = payload.get("worker_release") or "空"
                return f"worker 发布标识不匹配：实际 {observed}，期望 {expected_release}"
            return None

        check_endpoint("worker", "/api/v1/health/worker", body_check=worker_body_check)
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--metrics-token", default="")
    parser.add_argument("--expected-release", default="", help="要求 live/ready/worker 返回的发布标识")
    parser.add_argument("--require-worker", action="store_true")
    parser.add_argument("--require-web", action="store_true", help="检查边缘层是否返回可用 SPA HTML")
    parser.add_argument("--require-https", action="store_true", help="要求 HTTPS，并校验 HSTS 响应头")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--retry-seconds", type=float, default=1.0)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", dest="as_json")
    output.add_argument("--jsonl", action="store_true", help="输出一条符合日志契约的单行 JSON 事件")
    args = parser.parse_args(argv)
    try:
        checks = run_smoke(args.base_url, metrics_token=args.metrics_token,
                           expected_release=args.expected_release,
                           require_worker=args.require_worker, require_web=args.require_web,
                           require_https=args.require_https,
                           timeout=max(0.1, args.timeout),
                           attempts=max(1, args.attempts), retry_seconds=max(0.0, args.retry_seconds))
    except KeyboardInterrupt:
        if args.jsonl:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "schema_version": 1, "level": "WARNING", "service": "test-atelier",
                "environment": os.getenv("AI_STUDIO_ENV", "production")[:32],
                "event": "production.smoke.cancelled", "request_id": "-",
                "check_count": 0, "failure_count": 0, "warning_count": 0,
                "failed_checks": [], "checks": [],
            }
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print("生产 smoke 检查已取消")
        return 130
    failures = [item.name for item in checks if item.status == "fail"]
    warnings = sum(item.status == "warn" for item in checks)
    if args.jsonl:
        # Smoke details may contain proxy-specific URLs or host paths. Keep
        # the ingestion form bounded to check names, statuses and counters.
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "schema_version": 1,
            "level": "ERROR" if failures else "INFO",
            "service": "test-atelier",
            "environment": os.getenv("AI_STUDIO_ENV", "production")[:32],
            "event": "production.smoke.failed" if failures else "production.smoke.completed",
            "request_id": "-",
            "check_count": len(checks),
            "failure_count": len(failures),
            "warning_count": warnings,
            "failed_checks": failures[:32],
            "checks": [{"name": item.name, "status": item.status} for item in checks],
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif args.as_json:
        print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"[{item.status.upper():4}] {item.name}: {item.detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
