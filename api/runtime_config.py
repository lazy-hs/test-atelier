"""Load the deployment runtime JSON without requiring operator environment variables.

``config/runtime.json`` is the deployment control plane.  A small compatibility
adapter still exports the validated values to legacy modules that read
``os.environ``; the operator never has to export those variables and secrets
remain file-backed whenever the consumer supports ``*_FILE``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CONFIG = PROJECT_ROOT / "config" / "runtime.json"
SCHEMA_VERSION = 1
# Configuration aliases retained for legacy modules. In production they are
# scrubbed before loading runtime.json so an inherited shell cannot override
# the file-backed deployment contract.
_COMPATIBILITY_KEYS = {
    "AI_STUDIO_ENV", "AI_STUDIO_RELEASE", "AI_STUDIO_DATA_DIR", "AI_STUDIO_HOST", "AI_STUDIO_PORT",
    "AI_STUDIO_API_TARGET", "DATABASE_URL", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT",
    "TEST_ATELIER_SKILL_ROOT", "TEST_ATELIER_HARNESS_URL", "TEST_ATELIER_HARNESS_ONLY",
    "TEST_ATELIER_HARNESS_TIMEOUT", "TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES", "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES",
    "TEST_ATELIER_HARNESS_CHUNK_CHARS", "TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", "TEST_ATELIER_HARNESS_MAX_CHUNKS",
    "TEST_ATELIER_HARNESS_PROJECTOR_ENABLED", "TEST_ATELIER_HARNESS_PROJECTOR_POLL_SECONDS",
    "TEST_ATELIER_HARNESS_PROJECTOR_LEASE_SECONDS", "TEST_ATELIER_HARNESS_PROJECTOR_MAX_EVENTS",
    "TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT_SECONDS", "TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT",
    "HARNESS_DSH_PROTOCOL", "HARNESS_DSH_INPUT_MODE", "HARNESS_CONFIRMATION_TRANSPORT", "HARNESS_DSH_COMMAND",
    "HARNESS_DSH_ARGS_JSON", "HARNESS_MODEL_MAX_TOKENS", "HARNESS_TASK_TIMEOUT_MS", "HARNESS_STAGED_TASK_TIMEOUT_MS", "HARNESS_STAGED_UPLOAD_TIMEOUT_MS",
    "TEST_ATELIER_WEB_SHELL_PILOT", "TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS", "TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE",
    "HARNESS_MAX_ACTIVE_TASKS", "HARNESS_MAX_OUTPUT_BYTES", "HARNESS_MAX_EVENT_FRAME_BYTES", "HARNESS_REDUCE_BATCH_CHARS",
    "HARNESS_REDUCE_BATCH_SIZE", "AI_STUDIO_SESSION_MAX_AGE", "AI_STUDIO_LOGIN_FAILURE_LIMIT",
    "AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS", "AI_STUDIO_MAX_REQUEST_BYTES", "AI_STUDIO_MAX_DOCUMENT_BYTES",
    "AI_STUDIO_MAX_DOCUMENT_REQUEST_BYTES", "AI_STUDIO_TRUST_PROXY_HEADERS", "COOKIE_SECURE", "METRICS_ENABLED",
    "AI_STUDIO_EXTERNAL_WORKER", "AI_STUDIO_WORKER_POLL_SECONDS", "AI_STUDIO_WORKER_HEARTBEAT_SECONDS",
    "AI_STUDIO_WORKER_LEASE_SECONDS", "AI_STUDIO_WORKER_HEARTBEAT", "AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS",
    "AI_STUDIO_REQUIRE_MIGRATIONS", "AI_STUDIO_MIGRATION_HEAD", "AI_STUDIO_MIGRATION_TIMEOUT", "AI_STUDIO_SHUTDOWN_TIMEOUT",
    "AI_STUDIO_MAX_SUBMITTED_JOBS", "AI_STUDIO_SKILL_SCRIPT_TIMEOUT", "AI_STUDIO_CLI_TIMEOUT", "AI_STUDIO_CLI_TEST_TIMEOUT",
    "AI_STUDIO_REMOTE_ENABLED", "AI_STUDIO_REMOTE_URL", "AI_STUDIO_REMOTE_MODEL", "AI_STUDIO_CODEX_ENABLED",
    "AI_STUDIO_CODEX_COMMAND", "AI_STUDIO_CODEX_MODEL", "AI_STUDIO_CLAUDE_ENABLED", "AI_STUDIO_CLAUDE_COMMAND",
    "AI_STUDIO_CLAUDE_MODEL", "LOG_LEVEL", "LOG_FORMAT", "LOG_INCLUDE_TRACEBACK", "OPENAI_API_BASE", "OPENAI_MODEL",
    "AI_STUDIO_ADMIN_USER", "AI_STUDIO_SESSION_SECRET_FILE", "AI_STUDIO_ADMIN_PASSWORD_FILE", "TEST_ATELIER_SCOPE_SECRET_FILE",
    "TEST_ATELIER_HARNESS_TOKEN_FILE", "AI_STUDIO_DATABASE_PASSWORD_FILE", "AI_STUDIO_DATABASE_HOST", "AI_STUDIO_DATABASE_PORT",
    "AI_STUDIO_DATABASE_NAME", "AI_STUDIO_DATABASE_USER", "METRICS_TOKEN_FILE", "OPENAI_API_KEY_FILE", "OPENAI_API_KEY",
    "AI_STUDIO_SESSION_SECRET", "AI_STUDIO_ADMIN_PASSWORD", "TEST_ATELIER_SCOPE_SECRET", "TEST_ATELIER_HARNESS_TOKEN",
    "POSTGRES_PASSWORD", "METRICS_TOKEN", "AI_STUDIO_REMOTE_API_KEY", "DATABASE_URL",
}


class RuntimeConfigError(RuntimeError):
    """Raised when a present runtime configuration is malformed."""


def runtime_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the fixed configuration path used by local and container runs."""
    if path is not None:
        return Path(path).expanduser().resolve()
    # Internal maintenance commands may select an alternate runtime file
    # before importing the application modules. Operators still configure via
    # JSON, not by exporting this variable in normal service startup.
    override = os.getenv("TEST_ATELIER_RUNTIME_CONFIG", "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_RUNTIME_CONFIG


def _validate(document: Any, path: Path) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise RuntimeConfigError(f"runtime config must be a JSON object: {path}")
    try:
        version = int(document.get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError(f"unsupported runtime config schema: {path}") from exc
    if version != SCHEMA_VERSION:
        raise RuntimeConfigError(f"unsupported runtime config schema {version}: {path}")
    return document


@lru_cache(maxsize=8)
def _read_cached(path_text: str, mtime_ns: int, size: int) -> dict[str, Any]:
    path = Path(path_text)
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")), path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeConfigError(f"cannot read runtime config: {path}") from exc


def load_runtime_config(path: str | os.PathLike[str] | None = None, *, required: bool = False) -> dict[str, Any]:
    """Read and validate the JSON file; return an empty document when optional."""
    target = runtime_config_path(path)
    try:
        stat = target.stat()
    except FileNotFoundError:
        if required:
            raise RuntimeConfigError(f"runtime config is required: {target}")
        return {}
    except OSError as exc:
        raise RuntimeConfigError(f"cannot stat runtime config: {target}") from exc
    return _read_cached(str(target), stat.st_mtime_ns, stat.st_size)


def clear_runtime_config_cache() -> None:
    """Clear the tiny stat-keyed cache for tests and config reload commands."""
    _read_cached.cache_clear()


def _at(document: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, Mapping):
            return default
        value = value.get(key)
    return default if value is None else value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return str(Path(value).expanduser())


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bool(value: Any, default: bool = False) -> str:
    if value is None:
        return "1" if default else "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    return "1" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "0"


def _number(value: Any, default: int | float) -> str:
    try:
        return str(int(value)) if isinstance(default, int) else str(float(value))
    except (TypeError, ValueError):
        return str(default)


def _read_secret_file(filename: str, minimum: int = 1) -> str:
    if not filename:
        return ""
    path = Path(filename)
    if not path.is_absolute():
        raise RuntimeConfigError(f"secret path must be absolute: {path}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeConfigError(f"secret file is unavailable: {path}") from exc
    if len(value) < minimum or any(char in value for char in "\r\n\x00"):
        raise RuntimeConfigError(f"secret file is invalid: {path}")
    return value


def _database_url(document: Mapping[str, Any]) -> str:
    database = _at(document, "database", default={})
    explicit = _text(_at(database, "url", default=""))
    if explicit:
        return explicit
    engine = _text(_at(database, "engine", default="sqlite")).lower()
    if engine not in {"postgres", "postgresql"}:
        return "sqlite:///./data/ai_studio.db"
    host = _text(_at(database, "host", default="db")) or "db"
    port = _number(_at(database, "port", default=5432), 5432)
    name = _text(_at(database, "name", default="ai_studio")) or "ai_studio"
    user = _text(_at(database, "user", default="ai_studio")) or "ai_studio"
    password_file = _path(_at(database, "password_file", default=""))
    if not password_file:
        raise RuntimeConfigError("postgres runtime config requires database.password_file")
    if not Path(password_file).is_file():
        raise RuntimeConfigError(f"postgres password file is unavailable: {password_file}")
    # The bounded deployment_secret_file reader assembles the URL only after
    # the file source has been installed in the process contract.
    return ""


def environment_from_config(document: Mapping[str, Any], *, load_model_key: bool = True) -> dict[str, str]:
    """Flatten supported runtime sections into the existing module contract."""
    paths = _at(document, "paths", default={})
    server = _at(document, "server", default={})
    harness = _at(document, "harness", default={})
    model = _at(document, "model", default={})
    atelier = _at(document, "test_atelier", default={})
    secrets = _at(document, "secrets", default={})
    app = _at(document, "application", default={})
    security = _at(document, "security", default={})
    database = _at(document, "database", default={})
    worker = _at(document, "worker", default={})
    logging_config = _at(document, "logging", default={})
    limits = _at(document, "limits", default={})
    ai = _at(document, "ai", default={})

    values: dict[str, str] = {
        "AI_STUDIO_ENV": _text(_at(app, "environment", default="development"), "development"),
        "AI_STUDIO_RELEASE": _text(_at(app, "release", default="")),
        "AI_STUDIO_DATA_DIR": _path(_at(paths, "data_dir", default=_at(app, "data_dir", default="data"))) or "data",
        "AI_STUDIO_HOST": _text(_at(server, "bind_host", default="0.0.0.0"), "0.0.0.0"),
        "AI_STUDIO_PORT": _number(_at(server, "api_port", default=_at(server, "port", default=8000)), 8000),
        "AI_STUDIO_API_TARGET": _text(_at(server, "api_target", default="")),
        "DATABASE_URL": _database_url(document),
        "DB_POOL_SIZE": _number(_at(database, "pool_size", default=5), 5),
        "DB_MAX_OVERFLOW": _number(_at(database, "max_overflow", default=10), 10),
        "DB_POOL_TIMEOUT": _number(_at(database, "pool_timeout", default=30), 30),
        "TEST_ATELIER_SKILL_ROOT": _path(_at(paths, "skill_root", default="")),
        "TEST_ATELIER_HARNESS_URL": _text(_at(harness, "url", default="")),
        "TEST_ATELIER_HARNESS_ONLY": _bool(_at(harness, "harness_only", default=False)),
        "TEST_ATELIER_HARNESS_TIMEOUT": _number(_at(harness, "timeout_seconds", default=30), 30),
        "TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES": _number(_at(harness, "max_response_bytes", default=16 * 1024 * 1024), 16 * 1024 * 1024),
        "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES": _number(_at(harness, "max_request_bytes", default=8 * 1024 * 1024), 8 * 1024 * 1024),
        "TEST_ATELIER_HARNESS_CHUNK_CHARS": _number(_at(harness, "chunk_chars", default=12000), 12000),
        "TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS": _number(_at(harness, "chunk_overlap_chars", default=400), 400),
        "TEST_ATELIER_HARNESS_MAX_CHUNKS": _number(_at(harness, "max_chunks", default=128), 128),
        "TEST_ATELIER_HARNESS_PROJECTOR_ENABLED": _bool(_at(harness, "projector_enabled", default=True), True),
        "TEST_ATELIER_HARNESS_PROJECTOR_POLL_SECONDS": _number(_at(harness, "projector_poll_seconds", default=1), 1),
        "TEST_ATELIER_HARNESS_PROJECTOR_LEASE_SECONDS": _number(_at(harness, "projector_lease_seconds", default=120), 120),
        "TEST_ATELIER_HARNESS_PROJECTOR_MAX_EVENTS": _number(_at(harness, "projector_max_events", default=256), 256),
        "TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT_SECONDS": _number(_at(harness, "projector_heartbeat_seconds", default=5), 5),
        "TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT": _path(_at(paths, "projector_heartbeat", default="")),
        "HARNESS_DSH_PROTOCOL": _text(_at(harness, "protocol", default="headless"), "headless"),
        "HARNESS_DSH_INPUT_MODE": _text(_at(harness, "input_mode", default="stdin"), "stdin"),
        "HARNESS_CONFIRMATION_TRANSPORT": _text(_at(harness, "confirmation_transport", default="none"), "none"),
        "HARNESS_DSH_COMMAND": _text(_at(harness, "dsh_command", default="")),
        "HARNESS_TASK_TIMEOUT_MS": _number(_at(harness, "task_timeout_ms", default=900000), 900000),
        "HARNESS_STAGED_TASK_TIMEOUT_MS": _number(_at(harness, "staged_task_timeout_ms", default=7200000), 7200000),
        "HARNESS_STAGED_UPLOAD_TIMEOUT_MS": _number(_at(harness, "staged_upload_timeout_ms", default=900000), 900000),
        "HARNESS_MAX_ACTIVE_TASKS": _number(_at(harness, "max_active_tasks", default=4), 4),
        "HARNESS_MAX_OUTPUT_BYTES": _number(_at(harness, "max_output_bytes", default=16 * 1024 * 1024), 16 * 1024 * 1024),
        "HARNESS_MAX_EVENT_FRAME_BYTES": _number(_at(harness, "max_event_frame_bytes", default=15 * 1024 * 1024), 15 * 1024 * 1024),
        "HARNESS_REDUCE_BATCH_CHARS": _number(_at(harness, "reduce_batch_chars", default=48000), 48000),
        "HARNESS_REDUCE_BATCH_SIZE": _number(_at(harness, "reduce_batch_size", default=16), 16),
        "TEST_ATELIER_WEB_SHELL_PILOT": _bool(_at(_at(harness, "web_shell", default={}), "enabled", default=False)),
        "TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS": _bool(_at(_at(harness, "web_shell", default={}), "member_access", default=False)),
        "TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE": _bool(_at(_at(harness, "web_shell", default={}), "restricted_profile", default=True), True),
        "AI_STUDIO_SESSION_MAX_AGE": _number(_at(security, "session_max_age", default=28800), 28800),
        "AI_STUDIO_LOGIN_FAILURE_LIMIT": _number(_at(security, "login_failure_limit", default=8), 8),
        "AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS": _number(_at(security, "login_failure_window_seconds", default=60), 60),
        "AI_STUDIO_MAX_REQUEST_BYTES": _number(_at(limits, "max_request_bytes", default=25 * 1024 * 1024), 25 * 1024 * 1024),
        "AI_STUDIO_MAX_DOCUMENT_BYTES": _number(_at(limits, "max_document_bytes", default=100 * 1024 * 1024), 100 * 1024 * 1024),
        "AI_STUDIO_MAX_DOCUMENT_REQUEST_BYTES": _number(_at(limits, "max_document_request_bytes", default=110 * 1024 * 1024), 110 * 1024 * 1024),
        "AI_STUDIO_TRUST_PROXY_HEADERS": _bool(_at(security, "trust_proxy_headers", default=False)),
        "COOKIE_SECURE": _bool(_at(security, "cookie_secure", default=False)),
        "METRICS_ENABLED": _bool(_at(security, "metrics_enabled", default=True), True),
        "AI_STUDIO_EXTERNAL_WORKER": _bool(_at(worker, "external", default=False)),
        "AI_STUDIO_WORKER_POLL_SECONDS": _number(_at(worker, "poll_seconds", default=1), 1),
        "AI_STUDIO_WORKER_HEARTBEAT_SECONDS": _number(_at(worker, "heartbeat_seconds", default=5), 5),
        "AI_STUDIO_WORKER_LEASE_SECONDS": _number(_at(worker, "lease_seconds", default=900), 900),
        "AI_STUDIO_WORKER_HEARTBEAT": _path(_at(paths, "worker_heartbeat", default="")),
        "AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS": _number(_at(worker, "error_log_interval_seconds", default=30), 30),
        "AI_STUDIO_REQUIRE_MIGRATIONS": _bool(_at(app, "require_migrations", default=True), True),
        "AI_STUDIO_MIGRATION_HEAD": _text(_at(app, "migration_head", default="20261047_auth_session_revocations"), "20261047_auth_session_revocations"),
        "AI_STUDIO_MIGRATION_TIMEOUT": _number(_at(app, "migration_timeout", default=120), 120),
        "AI_STUDIO_SHUTDOWN_TIMEOUT": _number(_at(app, "shutdown_timeout", default=25), 25),
        "AI_STUDIO_MAX_SUBMITTED_JOBS": _number(_at(worker, "max_submitted_jobs", default=100), 100),
        "AI_STUDIO_SKILL_SCRIPT_TIMEOUT": _number(_at(limits, "skill_script_timeout", default=120), 120),
        "AI_STUDIO_CLI_TIMEOUT": _number(_at(ai, "cli_timeout", default=600), 600),
        "AI_STUDIO_CLI_TEST_TIMEOUT": _number(_at(ai, "cli_test_timeout", default=45), 45),
        "AI_STUDIO_REMOTE_ENABLED": _bool(_at(ai, "remote_enabled", default=False)),
        "AI_STUDIO_REMOTE_URL": _text(_at(ai, "remote_url", default="")),
        "AI_STUDIO_REMOTE_MODEL": _text(_at(ai, "remote_model", default="")),
        "AI_STUDIO_CODEX_ENABLED": _bool(_at(ai, "codex_enabled", default=False)),
        "AI_STUDIO_CODEX_COMMAND": _text(_at(ai, "codex_command", default="codex"), "codex"),
        "AI_STUDIO_CODEX_MODEL": _text(_at(ai, "codex_model", default="")),
        "AI_STUDIO_CLAUDE_ENABLED": _bool(_at(ai, "claude_enabled", default=False)),
        "AI_STUDIO_CLAUDE_COMMAND": _text(_at(ai, "claude_command", default="claude"), "claude"),
        "AI_STUDIO_CLAUDE_MODEL": _text(_at(ai, "claude_model", default="")),
        "LOG_LEVEL": _text(_at(logging_config, "level", default="INFO"), "INFO"),
        "LOG_FORMAT": _text(_at(logging_config, "format", default="json"), "json"),
        "LOG_INCLUDE_TRACEBACK": _bool(_at(logging_config, "include_traceback", default=False)),
        "OPENAI_API_BASE": _text(_at(model, "api_base", default="")),
        "OPENAI_MODEL": _text(_at(model, "model", default="")),
        "HARNESS_MODEL_MAX_TOKENS": _number(_at(model, "max_tokens", default=32768), 32768),
        "AI_STUDIO_ADMIN_USER": _text(_at(atelier, "admin_user", default="admin"), "admin"),
    }

    for env_name, section, key in (
        ("AI_STUDIO_SESSION_SECRET_FILE", security, "session_secret_file"),
        ("AI_STUDIO_ADMIN_PASSWORD_FILE", atelier, "admin_password_file"),
        ("TEST_ATELIER_SCOPE_SECRET_FILE", secrets, "scope_secret_file"),
        ("TEST_ATELIER_HARNESS_TOKEN_FILE", harness, "token_file"),
        ("AI_STUDIO_DATABASE_PASSWORD_FILE", database, "password_file"),
        ("METRICS_TOKEN_FILE", security, "metrics_token_file"),
    ):
        path = _path(section.get(key) if isinstance(section, Mapping) else "")
        if path:
            values[env_name] = path
    database_engine = _text(_at(database, "engine", default="sqlite")).lower()
    database_password_file = _path(_at(database, "password_file", default=""))
    if database_engine in {"postgres", "postgresql"} and database_password_file:
        # Keep the password out of DATABASE_URL and let the existing bounded
        # *_FILE reader build the connection URL at the final boundary.
        values["DATABASE_URL"] = ""
        values["AI_STUDIO_DATABASE_PASSWORD_FILE"] = database_password_file
        values["AI_STUDIO_DATABASE_HOST"] = _text(_at(database, "host", default="db"), "db")
        values["AI_STUDIO_DATABASE_PORT"] = _number(_at(database, "port", default=5432), 5432)
        values["AI_STUDIO_DATABASE_NAME"] = _text(_at(database, "name", default="ai_studio"), "ai_studio")
        values["AI_STUDIO_DATABASE_USER"] = _text(_at(database, "user", default="ai_studio"), "ai_studio")
    if "AI_STUDIO_SESSION_SECRET_FILE" not in values:
        fallback = _path(_at(atelier, "session_secret_file", default=""))
        if fallback:
            values["AI_STUDIO_SESSION_SECRET_FILE"] = fallback

    key_file = _path(_at(model, "api_key_file", default=""))
    if key_file:
        values["OPENAI_API_KEY_FILE"] = key_file
        # Harness currently consumes the OpenAI-compatible key directly. It is
        # loaded only into this process from the protected file, never JSON.
        if load_model_key:
            values["OPENAI_API_KEY"] = _read_secret_file(key_file, 16)
    # An empty URL means the independent Host is disabled. Do not synthesize
    # localhost here: that would make every local launcher start a Projector
    # even when the operator did not configure Harness.
    dsh_args = _at(harness, "dsh_args", default=None)
    if isinstance(dsh_args, list) and all(isinstance(item, str) for item in dsh_args):
        values["HARNESS_DSH_ARGS_JSON"] = json.dumps(dsh_args)
    return {key: value for key, value in values.items() if value != ""}


def install_runtime_config(*, required: bool = False, force: bool = True) -> bool:
    """Install JSON values into the compatibility environment for this process."""
    document = load_runtime_config(required=required)
    if not document:
        return False
    if _text(_at(document, "application", "environment", default="development")).lower() == "production":
        for name in list(os.environ):
            if (name in _COMPATIBILITY_KEYS or name.startswith(("AI_STUDIO_", "TEST_ATELIER_", "HARNESS_", "OPENAI_", "DB_", "LOG_"))):
                os.environ.pop(name, None)
    for name, value in environment_from_config(document, load_model_key=False).items():
        if force or not os.environ.get(name):
            os.environ[name] = value
    if os.environ.get("AI_STUDIO_DATABASE_PASSWORD_FILE"):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("POSTGRES_PASSWORD", None)
    return True


def runtime_environment(*, required: bool = False, load_model_key: bool = False) -> dict[str, str]:
    """Return flattened values without mutating the process or reading a key by default."""
    document = load_runtime_config(required=required)
    return environment_from_config(document, load_model_key=load_model_key) if document else {}

