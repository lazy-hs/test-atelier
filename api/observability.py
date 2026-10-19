"""Structured application logging and request correlation helpers.

The application deliberately logs lifecycle metadata rather than request
bodies, uploaded text, prompts, or model responses.  Those payloads can
contain project-sensitive material and are already persisted in their proper
domain records when needed for review.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import re
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone


_REQUEST_ID = contextvars.ContextVar("test_atelier_request_id", default="-")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HANDLER_MARKER = "test_atelier_json_handler"
_MAX_REDACTION_INPUT = 64 * 1024
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|token|password|secret)\s*[:=]\s*[^\r\n,;&}\s]+"
)
# Environment dumps and subprocess diagnostics commonly use uppercase names
# rather than human-readable ``password=`` labels. Keep these explicit so
# redaction also covers credentials that do not contain a separator word.
_SENSITIVE_ENV_PATTERN = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AZURE_OPENAI_API_KEY|AWS_ACCESS_KEY_ID|"
    r"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|GOOGLE_API_KEY|HF_TOKEN|GITHUB_TOKEN|"
    r"NPM_TOKEN|DOCKER_AUTH_CONFIG|DATABASE_URL|POSTGRES_PASSWORD|METRICS_TOKEN|"
    r"AI_STUDIO_SESSION_SECRET|AI_STUDIO_ADMIN_PASSWORD)\s*=\s*[^\r\n,;&}\s]+"
)
_SENSITIVE_COOKIE_PATTERN = re.compile(
    r"(?i)((?:set[-_ ]?cookie|cookie)\s*[:=]\s*)[^\r\n]+"
)
_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|cookie|token|password|secret)=)[^&#\s]+"
)
# Framework and CLI errors sometimes include a JSON fragment. The generic
# key/value pattern above intentionally does not consume quote characters, so
# handle quoted JSON keys and values before applying the broader patterns.
_SENSITIVE_JSON_PATTERN = re.compile(
    r"(?i)([\"'](?:authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|cookie|set[-_ ]?cookie|password|secret|token)[\"']\s*:\s*)([\"'][^\"'\r\n]*[\"']|[^,}\s]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----.*?-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----",
    re.DOTALL,
)
_PRIVATE_KEY_MARKER_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----")
_DATABASE_URL_PATTERN = re.compile(
    r"(?i)\b((?:postgres(?:ql)?(?:\+\w+)?|mysql(?:\+\w+)?|redis(?:s)?|mongodb(?:\+srv)?)://[^:\s/@]+:)([^@\s]+)(@)"
)
_FORBIDDEN_LOG_KEYS = {
    "body", "request_body", "response_body", "prompt", "prompts", "model_response",
    "authorization", "cookie", "set_cookie", "set-cookie", "password", "secret", "api_key", "access_token",
    "refresh_token", "client_secret", "database_url", "session_secret", "metrics_token",
    "remote_api_key", "private_key", "token",
}
_RELEASE_PATTERN = re.compile(r"[^A-Za-z0-9._/@:-]+")
_SUBPROCESS_SECRET_NAMES = frozenset({
    "AI_STUDIO_SESSION_SECRET", "AI_STUDIO_ADMIN_PASSWORD", "AI_STUDIO_REMOTE_API_KEY",
    "DATABASE_URL", "POSTGRES_PASSWORD", "METRICS_TOKEN", "TEST_ATELIER_SKILL_ROOT",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HF_TOKEN",
    "GITHUB_TOKEN", "NPM_TOKEN", "DOCKER_AUTH_CONFIG",
})
# Only these credentials may be intentionally passed to a provider CLI. Keep
# application secrets non-overridable even if a future caller supplies an
# overly broad preserve set.
_SUBPROCESS_PROVIDER_SECRET_NAMES = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HF_TOKEN",
})
_SUBPROCESS_RUNTIME_PREFIXES = (
    "AI_STUDIO_", "CODEX_APP_", "CODEX_SESSION", "CODEX_THREAD", "CODEX_TOOL_",
    "CODEX_MCP_", "CODEX_TASK_", "CODEX_INTERNAL_", "CODEX_PERMISSION_",
)


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    """Read an integer setting without allowing malformed imports to crash."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(value, minimum) if minimum is not None else value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    """Read a finite float setting and apply an optional lower bound."""
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(value, minimum) if minimum is not None else value


def _redact_sensitive_text(value: str) -> str:
    """Remove common credential forms before text reaches structured logs."""
    value = _DATABASE_URL_PATTERN.sub(r"\1[REDACTED]\3", value)
    # Cookie headers can contain signed sessions and are often emitted as a
    # complete ``Cookie: ...`` diagnostic by HTTP clients. Redact the whole
    # header before narrower key/value rules leave individual attributes.
    value = _SENSITIVE_COOKIE_PATTERN.sub(r"\1[REDACTED]", value)
    # Redact the complete Bearer credential before the generic key/value rule
    # sees ``Authorization: Bearer`` and consumes only the word ``Bearer``.
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    value = _SENSITIVE_JSON_PATTERN.sub(r"\1[REDACTED]", value)
    value = _SENSITIVE_QUERY_PATTERN.sub(r"\1[REDACTED]", value)
    value = _SENSITIVE_ENV_PATTERN.sub(lambda match: f"{match.group(0).split('=', 1)[0]}=[REDACTED]", value)
    value = _SENSITIVE_TEXT_PATTERN.sub(r"\1=[REDACTED]", value)
    value = _OPENAI_KEY_PATTERN.sub("[REDACTED]", value)
    value = _AWS_ACCESS_KEY_PATTERN.sub("[REDACTED]", value)
    value = _GITHUB_TOKEN_PATTERN.sub("[REDACTED]", value)
    value = _JWT_PATTERN.sub("[REDACTED]", value)
    value = _PRIVATE_KEY_BLOCK_PATTERN.sub("[PRIVATE_KEY_REDACTED]", value)
    return _PRIVATE_KEY_MARKER_PATTERN.sub("[PRIVATE_KEY_REDACTED]", value)


def redact_sensitive_text(value: str, limit: int = 500) -> str:
    """Redact credentials from operator-facing diagnostics.

    Application logs use the same filter internally, but subprocess errors
    can also be returned as bounded API diagnostics.  Keep this helper public
    so those paths cannot accidentally echo a provider token or database
    password while still preserving a useful failure summary.
    """
    # Bound the input before regex processing so a malformed provider error or
    # client-controlled diagnostic cannot turn redaction into a log-path DoS.
    return _redact_sensitive_text(str(value)[:_MAX_REDACTION_INPUT])[:max(1, int(limit))]


def sanitized_subprocess_environment(
    *,
    extra_secret_names: set[str] | None = None,
    preserve_secret_names: set[str] | None = None,
) -> dict[str, str]:
    """Return a child environment without application or desktop credentials.

    Provider CLIs need their own authentication variables, while parser and
    skill subprocesses should not receive any cloud credentials. Callers may
    explicitly preserve a small allowlist for a trusted provider process; all
    application and desktop integration secrets remain filtered.
    """
    environment = os.environ.copy()
    preserved = frozenset(preserve_secret_names or ()) & _SUBPROCESS_PROVIDER_SECRET_NAMES
    secret_names = (_SUBPROCESS_SECRET_NAMES - preserved) | frozenset(extra_secret_names or ())
    for name in list(environment):
        if name in secret_names or name.startswith(_SUBPROCESS_RUNTIME_PREFIXES):
            environment.pop(name, None)
    return environment


def deployment_release() -> str:
    """Return a bounded, log-safe deployment identifier when configured.

    Operators can set ``AI_STUDIO_RELEASE`` to a build number or commit SHA so
    incidents can be correlated with a rollout without putting arbitrary
    environment values into every log record.
    """
    raw = os.getenv("AI_STUDIO_RELEASE", "").strip()
    if not raw:
        return ""
    safe = redact_sensitive_text(raw, 128)
    return _RELEASE_PATTERN.sub("_", safe)[:128]


def sanitize_log_fields(fields: dict[str, object], *, allowed: set[str] | None = None) -> dict[str, object]:
    """Bound and redact fields emitted by startup shims before JSON encoding.

    API records use :class:`JsonFormatter`, which already has an explicit
    metadata allowlist. Entrypoints print JSON directly before the logging
    stack is configured, so they need the same protection against an
    accidental prompt, request body, or credential field.
    """
    sanitized: dict[str, object] = {}
    for key, value in fields.items():
        name = str(key)
        if allowed is not None and name not in allowed:
            continue
        if name.casefold() in _FORBIDDEN_LOG_KEYS:
            continue
        if isinstance(value, str):
            sanitized[name] = redact_sensitive_text(value, 256)
        elif isinstance(value, (list, tuple, set)):
            sanitized[name] = [redact_sensitive_text(str(item), 256) for item in list(value)[:32]]
        elif isinstance(value, float):
            sanitized[name] = value if math.isfinite(value) else None
        elif isinstance(value, (bool, int)) or value is None:
            sanitized[name] = value
        else:
            sanitized[name] = redact_sensitive_text(str(value), 256)
    return sanitized


class FailureWindow:
    """Small process-local sliding-window limiter for authentication failures.

    This is intentionally a defense-in-depth control for the single API
    process.  Production deployments should also rate-limit at the proxy so
    the limit is shared across replicas and can use the real client address.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10000):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._entries: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float, create: bool = True) -> deque[float]:
        entries = self._entries.get(key)
        if entries is None:
            if not create:
                return deque()
            entries = deque()
            self._entries[key] = entries
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()
        return entries

    def blocked(self, keys: tuple[str, ...], now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            return any(len(self._prune(key, current, create=False)) >= self.limit for key in keys)

    def record(self, keys: tuple[str, ...], now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            for key in keys:
                self._prune(key, current).append(current)
            if len(self._entries) > self.max_keys:
                # Drop the oldest key buckets first.  This bounds memory when
                # an attacker rotates usernames or source addresses.
                oldest = sorted(self._entries.items(), key=lambda item: item[1][-1] if item[1] else current)
                for key, _ in oldest[:len(self._entries) - self.max_keys]:
                    self._entries.pop(key, None)

    def clear(self, keys: tuple[str, ...]) -> None:
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def retry_after(self, keys: tuple[str, ...], now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            waits = []
            for key in keys:
                entries = self._prune(key, current, create=False)
                if len(entries) >= self.limit:
                    waits.append(max(0.0, entries[0] + self.window_seconds - current))
            return max(1, int(max(waits, default=0.0) + 0.999))


def request_id() -> str:
    """Return the request correlation id for the current execution context."""
    return _REQUEST_ID.get()


def new_request_id(candidate: str | None = None) -> str:
    """Accept safe caller ids and replace malformed or oversized values."""
    value = (candidate or "").strip()
    return value if _REQUEST_ID_PATTERN.fullmatch(value) else uuid.uuid4().hex


def set_request_id(value: str):
    return _REQUEST_ID.set(value)


def reset_request_id(token) -> None:
    _REQUEST_ID.reset(token)


def monotonic() -> float:
    return time.perf_counter()


def _safe_exception(record: logging.LogRecord) -> tuple[str, str | None]:
    """Keep useful exception identity while avoiding payload/secret leakage."""
    if not record.exc_info or record.exc_info[0] is None:
        return "", None
    exc_type = record.exc_info[0].__name__
    if os.getenv("LOG_INCLUDE_TRACEBACK", "0").lower() not in {"1", "true", "yes", "on"}:
        return exc_type, None
    # Keep stack frames for temporary diagnosis but omit exception messages.
    # Provider/client exceptions can embed prompt fragments or model output,
    # and credential redaction alone cannot reliably identify those payloads.
    text = _redact_sensitive_text("".join(traceback.format_tb(record.exc_info[2])))
    return exc_type, text[:4000]


def _safe_message(record: logging.LogRecord) -> str:
    """Prevent pre-rendered framework tracebacks from bypassing exception filtering."""
    message = record.getMessage()[:_MAX_REDACTION_INPUT]
    include_traceback = os.getenv("LOG_INCLUDE_TRACEBACK", "0").lower() in {"1", "true", "yes", "on"}
    if not include_traceback and (message.startswith("Traceback (most recent call last):") or "\nTraceback (most recent call last):" in message):
        message = message.splitlines()[-1] if message.splitlines() else "framework error"
    message = _redact_sensitive_text(message)
    return message[:4000]


class JsonFormatter(logging.Formatter):
    """Render one compact JSON object per log line.

    Only explicitly selected metadata fields are emitted.  This prevents an
    accidental ``extra`` value from serializing a request body or model
    payload into production logs.
    """

    _FIELDS = (
        "event", "method", "path", "route", "status", "duration_ms", "error_type",
        "job_id", "analysis_id", "project_id", "kind", "provider", "attempt", "prompt_calls",
        "prompt_length", "case_id", "completed", "total", "error_count", "process_pid", "queue_limit",
        "actor_id", "target_user_id", "role", "active_jobs", "released_reservations", "recovered_reservations", "shutdown_timeout", "component",
        "version", "revision", "expected_revision", "taxonomy_version_id", "suggestion_id", "case_version", "action", "count",
        "required", "source", "retry_after_seconds", "worker_id", "lease_seconds",
        "script", "returncode", "recovered_jobs", "reason", "file_size", "row_count", "warning_count", "failure_count", "failed_checks",
        "text_length", "conversion_mode", "output_mode", "database_kind", "session_max_age", "max_request_bytes", "request_bytes", "declared_bytes",
        "worker_age_seconds", "worker_release", "error_log_interval_seconds",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "schema_version": 1,
            "level": record.levelname,
            "logger": record.name,
            "service": "test-atelier",
            "environment": os.getenv("AI_STUDIO_ENV", "development")[:32],
            "message": _safe_message(record),
            "request_id": request_id(),
        }
        release = deployment_release()
        if release:
            payload["release"] = release
        for field in self._FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                # Extra fields are selected explicitly, but some originate
                # from multipart/form values. Bound strings so a malformed
                # client cannot amplify one structured log line.
                if isinstance(value, str):
                    value = redact_sensitive_text(value, 256)
                elif isinstance(value, (list, tuple, set)):
                    value = [redact_sensitive_text(str(item), 256) for item in list(value)[:32]]
                elif isinstance(value, float):
                    # Strict JSON collectors reject non-standard NaN/Infinity
                    # tokens, so turn malformed numeric metadata into null.
                    value = value if math.isfinite(value) else None
                elif not isinstance(value, (bool, int, float)):
                    value = redact_sensitive_text(str(value), 256)
                payload[field] = value
        exception_type, exception_text = _safe_exception(record)
        if exception_type:
            payload["exception_type"] = exception_type
        if exception_text:
            payload["exception"] = exception_text
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class SafeTextFormatter(logging.Formatter):
    """Keep the opt-in human-readable format subject to the same redaction."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        message = _safe_message(record)
        exception_type, exception_text = _safe_exception(record)
        suffix = f" [{exception_type}]" if exception_type else ""
        if exception_text:
            suffix += f"\n{exception_text}"
        return f"{timestamp} {record.levelname} {record.name} {message}{suffix}"


def configure_logging() -> logging.Logger:
    """Configure the ``api`` logger once and return it.

    Uvicorn owns its access/error loggers, so application logs use a dedicated
    logger hierarchy. Dependency records fall through a bounded root sink so
    JSON output stays stable under both Uvicorn and pytest instead of relying
    on Python's plain-text lastResort handler.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    output_format = os.getenv("LOG_FORMAT", "json").lower()
    # Production startup failures must remain machine-readable even when an
    # operator supplied an invalid logging format. The runtime preflight will
    # still reject the configuration; forcing JSON here preserves the log
    # contract for that rejection path itself.
    if os.getenv("AI_STUDIO_ENV", "development").strip().lower() == "production":
        output_format = "json"

    def add_handler(target: logging.Logger, *, replace_existing: bool = False,
                    propagate: bool = False) -> None:
        target.setLevel(level)
        target.propagate = propagate
        handler = next((item for item in target.handlers if getattr(item, _HANDLER_MARKER, False)), None)
        if handler is not None:
            return
        if replace_existing:
            # Uvicorn installs its colorized text handler after application
            # import. Remove it before adding our sink or production logs
            # would contain duplicate, differently formatted error records.
            for existing in list(target.handlers):
                target.removeHandler(existing)
                existing.close()
        handler = logging.StreamHandler()
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            SafeTextFormatter()
            if output_format == "text" else JsonFormatter()
        )
        target.addHandler(handler)

    logger = logging.getLogger("api")
    add_handler(logger)
    # Dependency loggers are intentionally kept at WARNING or above.  At
    # DEBUG/INFO SQLAlchemy can emit complete SQL statements (and, depending
    # on engine configuration, bound values), while HTTP clients can include
    # query strings or request metadata.  Application code records bounded
    # route/provider metadata separately, so these low-level records add
    # little operational value and create an avoidable payload-leak path.
    dependency_level = max(level, logging.WARNING)
    for name in (
        "sqlalchemy.engine", "sqlalchemy.pool", "sqlalchemy.orm",
        "httpx", "httpcore", "urllib3", "multipart",
    ):
        logging.getLogger(name).setLevel(dependency_level)
    # Keep third-party records (SQLAlchemy, HTTP clients and parser libraries)
    # on the same JSON stream. Application and Uvicorn loggers use dedicated
    # sinks below; the root sink catches dependency warnings/errors that would
    # otherwise fall through Python's plain-text lastResort handler.
    add_handler(logging.getLogger(), replace_existing=True)
    # Middleware emits the canonical request event, so suppress Uvicorn's
    # duplicate access line. Error/startup records use the same JSON sink once
    # lifespan runs (Uvicorn may install its own handlers before then).
    logging.getLogger("uvicorn.access").disabled = True
    add_handler(logging.getLogger("uvicorn.error"), replace_existing=True)
    # Libraries such as PDF/OXML parsers may emit Python warnings directly;
    # route them through the same bounded formatter instead of allowing a raw
    # stderr line to break the JSON log stream.
    logging.captureWarnings(True)
    add_handler(logging.getLogger("py.warnings"), replace_existing=True)
    return logger
