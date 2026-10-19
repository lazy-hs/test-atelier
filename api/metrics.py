"""Small dependency-free Prometheus registry for production probes.

The API intentionally keeps only low-cardinality request labels (method,
route template and status).  Request paths and query strings are never used as
labels, so an attacker cannot grow the registry by sending arbitrary URLs.
"""

from __future__ import annotations

import os
import math
import threading
import time
from collections import Counter, defaultdict


_started_at = time.monotonic()
_lock = threading.Lock()
_inflight = 0
_ai_active = 0
_ai_limit = 0
_worker_up = 0
_worker_age = 0.0
_worker_heartbeat_threshold = 30.0
_db_pool_size = 0
_db_pool_checked_out = 0
_db_pool_overflow = 0
_requests = Counter()
_duration_count = Counter()
_duration_sum = defaultdict(float)
_model_requests = Counter()
_model_duration_sum = defaultdict(float)
_cli_enabled = {provider: 0 for provider in ("codex_cli", "claude_code")}
_cli_available = {provider: 0 for provider in ("codex_cli", "claude_code")}
_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
_KNOWN_PROVIDERS = {"local", "remote", "codex_cli", "claude_code"}
_KNOWN_MODEL_OUTCOMES = {"success", "failure", "cancelled", "timeout"}


def _finite_nonnegative(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number) if math.isfinite(number) else 0.0


def enabled() -> bool:
    """Allow operators to disable the endpoint when metrics are not needed."""
    return os.getenv("METRICS_ENABLED", "1").lower() not in {"0", "false", "off", "no"}


def begin_request() -> None:
    global _inflight
    with _lock:
        _inflight += 1


def observe_request(method: str, route: str, status: int, duration_ms: float) -> None:
    global _inflight
    # Route comes from the framework template and is bounded; callers should
    # still pass a fallback when routing failed before a template was set.
    normalized_method = str(method or "").upper()
    if normalized_method not in _KNOWN_METHODS:
        normalized_method = "<other>"
    key = (normalized_method, route or "<unmatched>", str(status))
    with _lock:
        _inflight = max(0, _inflight - 1)
        _requests[key] += 1
        _duration_count[key] += 1
        _duration_sum[key] += _finite_nonnegative(duration_ms) / 1000


def set_ai_queue(active: int, limit: int) -> None:
    """Publish bounded AI worker utilization without job-level labels."""
    global _ai_active, _ai_limit
    with _lock:
        _ai_active = max(0, int(active))
        _ai_limit = max(0, int(limit))


def set_worker_health(up: bool, age_seconds: float = 0.0) -> None:
    """Publish the last durable worker heartbeat for alerting."""
    global _worker_up, _worker_age
    with _lock:
        _worker_up = 1 if up else 0
        _worker_age = _finite_nonnegative(age_seconds)


def set_worker_heartbeat_threshold(seconds: float) -> None:
    """Publish the effective heartbeat staleness threshold used by health checks."""
    global _worker_heartbeat_threshold
    with _lock:
        _worker_heartbeat_threshold = max(1.0, _finite_nonnegative(seconds))


def set_database_pool(size: int, checked_out: int, overflow: int) -> None:
    """Publish bounded SQLAlchemy pool gauges for connection exhaustion alerts."""
    global _db_pool_size, _db_pool_checked_out, _db_pool_overflow
    with _lock:
        _db_pool_size = max(0, int(size))
        _db_pool_checked_out = max(0, int(checked_out))
        _db_pool_overflow = max(0, int(overflow))


def observe_model(provider: str, outcome: str, duration_ms: float) -> None:
    """Record bounded provider outcomes for timeout and failure alerting."""
    normalized_provider = provider if provider in _KNOWN_PROVIDERS else "other"
    normalized_outcome = outcome if outcome in _KNOWN_MODEL_OUTCOMES else "failure"
    # Keep unknown providers collapsed into the generic failure bucket rather
    # than allowing arbitrary outcome labels to create a new series.
    if normalized_provider == "other":
        normalized_outcome = "failure"
    key = (normalized_provider, normalized_outcome)
    with _lock:
        _model_requests[key] += 1
        _model_duration_sum[key] += _finite_nonnegative(duration_ms) / 1000


def set_cli_availability(provider: str, available: bool, enabled: bool | None = None) -> None:
    """Publish CLI configuration and executable availability.

    Keep enablement separate from availability so an optional disabled
    provider does not trigger an alert for a missing executable.
    """
    if provider not in _cli_available:
        return
    with _lock:
        _cli_enabled[provider] = 1 if (available if enabled is None else enabled) else 0
        _cli_available[provider] = 1 if available else 0


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus() -> str:
    with _lock:
        requests = dict(_requests)
        duration_count = dict(_duration_count)
        duration_sum = dict(_duration_sum)
        model_requests = dict(_model_requests)
        model_duration_sum = dict(_model_duration_sum)
        inflight = _inflight
        ai_active = _ai_active
        ai_limit = _ai_limit
        worker_up = _worker_up
        worker_age = _worker_age
        worker_heartbeat_threshold = _worker_heartbeat_threshold
        db_pool_size = _db_pool_size
        db_pool_checked_out = _db_pool_checked_out
        db_pool_overflow = _db_pool_overflow
        cli_enabled = dict(_cli_enabled)
        cli_available = dict(_cli_available)
    lines = [
        "# HELP test_atelier_requests_total Total HTTP requests completed.",
        "# TYPE test_atelier_requests_total counter",
    ]
    for (method, route, status), value in sorted(requests.items()):
        labels = f'method="{_escape(method)}",route="{_escape(route)}",status="{_escape(status)}"'
        lines.append(f"test_atelier_requests_total{{{labels}}} {value}")
    lines.extend([
        "# HELP test_atelier_request_duration_seconds_sum Cumulative HTTP request duration in seconds.",
        "# TYPE test_atelier_request_duration_seconds_sum counter",
    ])
    for (method, route, status), value in sorted(duration_sum.items()):
        labels = f'method="{_escape(method)}",route="{_escape(route)}",status="{_escape(status)}"'
        lines.append(f"test_atelier_request_duration_seconds_sum{{{labels}}} {value:.6f}")
    lines.extend([
        "# HELP test_atelier_request_duration_seconds_count Number of observations in the duration counter.",
        "# TYPE test_atelier_request_duration_seconds_count counter",
    ])
    for (method, route, status), value in sorted(duration_count.items()):
        labels = f'method="{_escape(method)}",route="{_escape(route)}",status="{_escape(status)}"'
        lines.append(f"test_atelier_request_duration_seconds_count{{{labels}}} {value}")
    lines.extend([
        "# HELP test_atelier_model_requests_total Model provider requests by bounded outcome.",
        "# TYPE test_atelier_model_requests_total counter",
    ])
    for (provider, outcome), value in sorted(model_requests.items()):
        labels = f'provider="{_escape(provider)}",outcome="{_escape(outcome)}"'
        lines.append(f"test_atelier_model_requests_total{{{labels}}} {value}")
    lines.extend([
        "# HELP test_atelier_model_request_duration_seconds_sum Cumulative model provider duration in seconds.",
        "# TYPE test_atelier_model_request_duration_seconds_sum counter",
    ])
    for (provider, outcome), value in sorted(model_duration_sum.items()):
        labels = f'provider="{_escape(provider)}",outcome="{_escape(outcome)}"'
        lines.append(f"test_atelier_model_request_duration_seconds_sum{{{labels}}} {value:.6f}")
    lines.extend([
        "# HELP test_atelier_inflight_requests Current requests being processed.",
        "# TYPE test_atelier_inflight_requests gauge",
        f"test_atelier_inflight_requests {inflight}",
        "# HELP test_atelier_ai_jobs_active Number of AI jobs currently admitted to an embedded or durable queue.",
        "# TYPE test_atelier_ai_jobs_active gauge",
        f"test_atelier_ai_jobs_active {ai_active}",
        "# HELP test_atelier_ai_jobs_capacity Maximum number of AI jobs accepted by the process queue.",
        "# TYPE test_atelier_ai_jobs_capacity gauge",
        f"test_atelier_ai_jobs_capacity {ai_limit}",
        "# HELP test_atelier_worker_up Whether the durable worker heartbeat is healthy.",
        "# TYPE test_atelier_worker_up gauge",
        f"test_atelier_worker_up {worker_up}",
        "# HELP test_atelier_worker_heartbeat_age_seconds Age of the durable worker heartbeat.",
        "# TYPE test_atelier_worker_heartbeat_age_seconds gauge",
        f"test_atelier_worker_heartbeat_age_seconds {worker_age:.3f}",
        "# HELP test_atelier_worker_heartbeat_threshold_seconds Effective age threshold before a worker is stale.",
        "# TYPE test_atelier_worker_heartbeat_threshold_seconds gauge",
        f"test_atelier_worker_heartbeat_threshold_seconds {worker_heartbeat_threshold:.3f}",
        "# HELP test_atelier_db_pool_size Configured SQLAlchemy database pool size.",
        "# TYPE test_atelier_db_pool_size gauge",
        f"test_atelier_db_pool_size {db_pool_size}",
        "# HELP test_atelier_db_pool_checked_out Current SQLAlchemy connections in use.",
        "# TYPE test_atelier_db_pool_checked_out gauge",
        f"test_atelier_db_pool_checked_out {db_pool_checked_out}",
        "# HELP test_atelier_db_pool_overflow Current connections above the configured pool size.",
        "# TYPE test_atelier_db_pool_overflow gauge",
        f"test_atelier_db_pool_overflow {db_pool_overflow}",
        "# HELP test_atelier_cli_enabled Whether a local CLI provider is enabled by configuration.",
        "# TYPE test_atelier_cli_enabled gauge",
    ])
    for provider, enabled in sorted(cli_enabled.items()):
        lines.append(f'test_atelier_cli_enabled{{provider="{_escape(provider)}"}} {enabled}')
    lines.extend([
        "# HELP test_atelier_cli_available Whether a configured local CLI executable is available.",
        "# TYPE test_atelier_cli_available gauge",
    ])
    for provider, available in sorted(cli_available.items()):
        lines.append(f'test_atelier_cli_available{{provider="{_escape(provider)}"}} {available}')
    lines.extend([
        "# HELP test_atelier_process_uptime_seconds Process uptime in seconds.",
        "# TYPE test_atelier_process_uptime_seconds gauge",
        f"test_atelier_process_uptime_seconds {time.monotonic() - _started_at:.3f}",
        "",
    ])
    return "\n".join(lines)


def reset_for_tests() -> None:
    """Reset state for isolated tests without exposing a public API route."""
    global _inflight, _started_at, _ai_active, _ai_limit, _worker_up, _worker_age
    global _db_pool_size, _db_pool_checked_out, _db_pool_overflow, _worker_heartbeat_threshold
    with _lock:
        _inflight = 0
        _started_at = time.monotonic()
        _requests.clear()
        _duration_count.clear()
        _duration_sum.clear()
        _model_requests.clear()
        _model_duration_sum.clear()
        _ai_active = 0
        _ai_limit = 0
        _worker_up = 0
        _worker_age = 0.0
        _worker_heartbeat_threshold = 30.0
        _db_pool_size = 0
        _db_pool_checked_out = 0
        _db_pool_overflow = 0
        for provider in _cli_enabled:
            _cli_enabled[provider] = 0
        for provider in _cli_available:
            _cli_available[provider] = 0
