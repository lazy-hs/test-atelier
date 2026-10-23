import io
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import threading
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from openpyxl import Workbook

from .db import (Base, AiQueueState, AiSettings, AiModel, AiTool, AiJob, AnalysisRun, Case, CaseComment, CaseDraft, CaseSuggestion, CodexMessage, CodexSession, ConversionRecord, ConversionSettings, ExecutionRun, ImportBatch,
                 GovernanceChangeLog, GovernanceDirectory, GovernanceDuplicateGroup, GovernanceDuplicateMember, GovernanceExportJob, GovernanceMetricSnapshot, GovernanceValidationRun,
                 Membership, MembershipEvent, Project, ReviewEvent, RunCase, SessionLocal,
                 ProjectSkill, SmokeItem, SmokeSuite, SourceDocument, SuggestionJob, TaxonomyVersion, User,
                 ExecutionEvent, WorkItem, database_kind, engine, utcnow)
from .skill_runner import SkillRunnerError, discover_skills, skill_root
from .importers import ImportErrorDetail, parse
from .governance_core import source_case_timestamps
from .observability import (FailureWindow, configure_logging, deployment_release, env_float, env_int,
                            monotonic, new_request_id, redact_sensitive_text, reset_request_id, set_request_id)
from .storage import ensure_private_directory, open_private_regular_file
from . import config_file, metrics


logger = configure_logging()


class SessionSecretRequired(RuntimeError):
    """Raised when the application session secret is missing or too short."""


secret = os.getenv("AI_STUDIO_SESSION_SECRET")
if not secret or len(secret) < 32:
    logger.error("application.import_failed", extra={
        "event": "application.import_failed", "component": "configuration",
        "error_type": "SessionSecretRequired",
    })
    raise SessionSecretRequired("AI_STUDIO_SESSION_SECRET must contain at least 32 characters")

hasher = PasswordHasher()
LOGIN_FAILURE_LIMIT = env_int("AI_STUDIO_LOGIN_FAILURE_LIMIT", 8, 1)
LOGIN_FAILURE_WINDOW_SECONDS = env_float("AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS", 60, 1.0)
_login_failures = FailureWindow(LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW_SECONDS)
MAX_REQUEST_BYTES = env_int("AI_STUDIO_MAX_REQUEST_BYTES", 25 * 1024 * 1024, 1)
# Multipart requirement uploads are intentionally allowed to exceed the
# generic JSON request ceiling.  The document endpoint still enforces its own
# bounded file limit and parser quotas; this only prevents the ASGI guard from
# rejecting a 100 MB upload before FastAPI can inspect it.
MAX_DOCUMENT_REQUEST_BYTES = env_int(
    "AI_STUDIO_MAX_DOCUMENT_REQUEST_BYTES", 110 * 1024 * 1024, 1
)
SESSION_MAX_AGE = env_int("AI_STUDIO_SESSION_MAX_AGE", 28800, 300)
_ready_for_traffic = False
_readiness_log_lock = threading.Lock()
_readiness_log_last: dict[str, float] = {}
_READINESS_LOG_INTERVAL_SECONDS = 30.0
_HEARTBEAT_RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9._/@:-]{1,128}$")
# Keep the browser policy identical for direct API access and the bundled edge
# proxy. The SPA has no inline scripts; component libraries only need inline
# style attributes.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
    "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:;"
)


class RequestBodyTooLarge(Exception):
    """Raised when a streamed request exceeds the global body limit."""


class ProductionConfigurationError(RuntimeError):
    """Raised when a production-only startup invariant is disabled."""


class CookieSecureRequired(ProductionConfigurationError):
    """Raised when production sessions are not marked HTTPS-only."""


class MetricsTokenRequired(ProductionConfigurationError):
    """Raised when the production metrics endpoint has no strong token."""


class ExplicitMigrationsRequired(ProductionConfigurationError):
    """Raised when production attempts to use implicit schema creation."""


def _login_failure_keys(request: Request, username: str) -> tuple[str, ...]:
    """Use opaque keys so account names and client addresses stay out of state dumps."""
    client = _client_address(request)
    return tuple(
        hashlib.sha256(f"{prefix}:{value.casefold()}".encode("utf-8")).hexdigest()
        for prefix, value in (("client", client), ("username", username.strip()))
    )


def _client_address(request: Request) -> str:
    """Resolve a client address only when proxy headers are explicitly trusted.

    API deployments behind the bundled Nginx proxy otherwise appear as one
    source address to the process-local login limiter.  Never trust forwarded
    headers for direct deployments; operators must opt in after placing the
    API behind a controlled proxy network.
    """
    direct = request.client.host if request.client else "unknown"
    if os.getenv("AI_STUDIO_TRUST_PROXY_HEADERS", "0").lower() not in {"1", "true", "yes", "on"}:
        return direct
    candidates = [request.headers.get("x-real-ip", "")]
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        candidates.extend(part.strip() for part in forwarded.split(","))
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return direct


def _route_template(request: Request) -> str:
    """Return a bounded route label without putting IDs or queries in metrics."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return template or "<unmatched>"


def _log_path(request: Request) -> str:
    """Bound and redact paths so malformed URLs cannot amplify or leak logs."""
    # Query strings are excluded by ``request.url.path``, but clients can still
    # place token-like values in an unmatched path.  Keep the diagnostic path
    # useful while applying the same credential filter as exception messages.
    return redact_sensitive_text(request.url.path[:512], 512)


def _validate_runtime_security() -> None:
    """Keep direct Uvicorn launches subject to production HTTP safeguards."""
    if os.getenv("AI_STUDIO_ENV", "development").strip().lower() != "production":
        return
    if os.getenv("COOKIE_SECURE", "0").strip() != "1":
        raise CookieSecureRequired("生产环境必须启用 COOKIE_SECURE=1")
    if len(os.getenv("METRICS_TOKEN", "").strip()) < 16:
        raise MetricsTokenRequired("生产环境必须配置至少 16 个字符的 METRICS_TOKEN")
    if os.getenv("LOG_FORMAT", "json").strip().lower() != "json":
        # The normal container entrypoint catches this in preflight. Keep the
        # direct Uvicorn path safe too so an operator cannot accidentally boot
        # production with human-readable logs that bypass the JSON contract.
        raise ProductionConfigurationError("生产环境必须使用 LOG_FORMAT=json")
    if os.getenv("LOG_LEVEL", "INFO").strip().upper() not in {
        "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG",
    }:
        raise ProductionConfigurationError("生产环境 LOG_LEVEL 配置无效")


def _validate_migration_policy(environment: str, require_migrations: bool) -> None:
    if environment == "production" and not require_migrations:
        raise ExplicitMigrationsRequired("生产环境必须启用 AI_STUDIO_REQUIRE_MIGRATIONS=1")


def _environment_provider_overrides() -> bool:
    """Return whether provider environment variables contain initial values.

    Older Alembic revisions created an ``ai_settings`` row before the API
    could seed it from environment variables.  Treat that untouched legacy
    row as an empty database only when the operator actually supplied an
    override; a later UI save remains authoritative.
    """
    defaults = {
        "AI_STUDIO_REMOTE_ENABLED": "0",
        "AI_STUDIO_REMOTE_URL": "",
        "AI_STUDIO_REMOTE_MODEL": "",
        "AI_STUDIO_REMOTE_API_KEY": "",
    }
    return any(os.getenv(name, default).strip() != default for name, default in defaults.items())


def _is_legacy_bootstrap_settings(settings: AiSettings) -> bool:
    """Identify the row inserted by the pre-runtime-settings migration."""
    return (
        settings.local_url.rstrip("/") == "http://127.0.0.1:11434"
        and settings.local_model == "qwen3:8b"
        and not settings.remote_enabled
        and not settings.remote_url
        and not settings.remote_model
        and not settings.remote_api_key
        and (not settings.providers_json or settings.providers_json.strip() == "[]")
        and not settings.codex_enabled
        and (settings.codex_command or "codex") == "codex"
        and not settings.codex_model
        and not settings.claude_enabled
        and (settings.claude_command or "claude") == "claude"
        and not settings.claude_model
    )


def _seed_existing_bootstrap_settings(settings: AiSettings, ai_engine) -> None:
    """Apply initial provider environment values to an untouched legacy row."""
    settings.local_url = ""
    settings.local_model = ""
    settings.remote_enabled = os.getenv("AI_STUDIO_REMOTE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    settings.remote_url = os.getenv("AI_STUDIO_REMOTE_URL", "").strip().rstrip("/")
    settings.remote_model = os.getenv("AI_STUDIO_REMOTE_MODEL", "").strip()
    settings.codex_enabled = os.getenv("AI_STUDIO_CODEX_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    settings.codex_command = os.getenv("AI_STUDIO_CODEX_COMMAND", "codex").strip() or "codex"
    settings.codex_model = os.getenv("AI_STUDIO_CODEX_MODEL", "").strip()
    settings.claude_enabled = os.getenv("AI_STUDIO_CLAUDE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    settings.claude_command = os.getenv("AI_STUDIO_CLAUDE_COMMAND", "claude").strip() or "claude"
    settings.claude_model = os.getenv("AI_STUDIO_CLAUDE_MODEL", "").strip()
    remote_key = os.getenv("AI_STUDIO_REMOTE_API_KEY", "").strip()
    if remote_key:
        settings.remote_api_key = ai_engine.encrypt_secret(remote_key)
    settings.updated_at = utcnow()


def _worker_heartbeat_path() -> str:
    configured = os.getenv("AI_STUDIO_WORKER_HEARTBEAT", "").strip()
    if configured:
        return configured
    return str(Path(os.getenv("AI_STUDIO_DATA_DIR", "data")) / ".worker-heartbeat")


def _heartbeat_age(path: str | Path) -> float:
    """Read heartbeat metadata from a descriptor without following links."""
    heartbeat = Path(path)
    # A custom heartbeat path is operator-controlled, but its parent must not
    # redirect the API's health probe through a symlinked storage component.
    stream, _ = open_private_regular_file(heartbeat, root=heartbeat.parent)
    try:
        return max(0.0, time.time() - os.fstat(stream.fileno()).st_mtime)
    finally:
        stream.close()


def _heartbeat_release(path: str | Path) -> str:
    """Read only the worker release marker from a bounded heartbeat file."""
    heartbeat = Path(path)
    stream, _ = open_private_regular_file(heartbeat, root=heartbeat.parent)
    try:
        raw = stream.read(4096)
    finally:
        stream.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        # Older workers wrote a plain timestamp. They remain liveness
        # compatible, but intentionally do not claim a verified build.
        return ""
    release = payload.get("release", "") if isinstance(payload, dict) else ""
    return release if isinstance(release, str) and _HEARTBEAT_RELEASE_PATTERN.fullmatch(release) else ""


def _log_readiness_failure(event: str, *, level: int = logging.ERROR, **fields) -> None:
    """Rate-limit repeated probe failures without hiding the 503 response.

    Health checks can run every few seconds while a dependency is down. Keep
    the first structured record and suppress identical noise for a short
    window so an outage does not overwhelm the log collector.
    """
    key = f"{event}:{fields.get('component', '')}:{fields.get('error_type', '')}"
    now = time.monotonic()
    with _readiness_log_lock:
        previous = _readiness_log_last.get(key, 0.0)
        if previous and now - previous < _READINESS_LOG_INTERVAL_SECONDS:
            return
        _readiness_log_last[key] = now
    logger.log(level, event, extra={"event": event, **fields})


def _configured_skill_root_missing() -> list[str]:
    """Return required skill-repository files missing from a configured root.

    A mounted directory can exist while the actual workflow files are absent
    (for example after an incorrect Docker volume mount).  Readiness must fail
    before traffic reaches an otherwise healthy-looking API in that case.
    Development keeps the historical implicit default path, so this stricter
    check is only applied when ``TEST_ATELIER_SKILL_ROOT`` is explicitly set.
    """
    configured = os.getenv("TEST_ATELIER_SKILL_ROOT", "").strip()
    if not configured:
        return []
    root = Path(configured).expanduser()
    required = (
        root / "AGENTS.md",
        root / "skills" / "hub" / "SKILL.md",
        root / "skills" / "requirement-analysis" / "SKILL.md",
        root / "skills" / "test-cases" / "SKILL.md",
    )
    return [str(path) for path in required if not path.is_file()]


def reconcile_queue_capacity(db: Session) -> tuple[int, int]:
    """Repair durable admission state and return ``(active, released)``.

    Terminal jobs should never hold a queue reservation.  Clearing those
    flags before recounting also makes restore/upgrade recovery safe when a
    previous process stopped between status finalization and cleanup.
    """
    active_statuses = ["queued", "running"]
    external_worker = os.getenv("AI_STUDIO_EXTERNAL_WORKER", "0").lower() in {"1", "true", "yes", "on"}
    recovered_reservations = 0
    if external_worker:
        # A process can crash after committing a job but before reserving its
        # durable queue slot. Recover those rows before recounting so they do
        # not bypass admission control or remain invisible to capacity gauges.
        recovered_reservations = (
            (db.execute(update(AiJob).where(
                AiJob.queue_reserved.is_(False), AiJob.status.in_(active_statuses),
            ).values(queue_reserved=True)).rowcount or 0)
            + (db.execute(update(SuggestionJob).where(
                SuggestionJob.queue_reserved.is_(False), SuggestionJob.status.in_(active_statuses),
            ).values(queue_reserved=True)).rowcount or 0)
        )
    released_reservations = (
        (db.execute(update(AiJob).where(
            AiJob.queue_reserved.is_(True), ~AiJob.status.in_(active_statuses),
        ).values(queue_reserved=False)).rowcount or 0)
        + (db.execute(update(SuggestionJob).where(
            SuggestionJob.queue_reserved.is_(True), ~SuggestionJob.status.in_(active_statuses),
        ).values(queue_reserved=False)).rowcount or 0)
    )
    queue_state = db.scalar(select(AiQueueState).where(AiQueueState.id == 1).with_for_update())
    if queue_state is None:
        queue_state = AiQueueState(id=1)
        try:
            # Several API replicas can reconcile a fresh database together.
            # Isolate the singleton insert so a duplicate winner does not
            # abort the surrounding reservation/status transaction.
            with db.begin_nested():
                db.add(queue_state)
                db.flush()
        except IntegrityError:
            queue_state = db.scalar(select(AiQueueState).where(AiQueueState.id == 1).with_for_update())
            if queue_state is None:
                raise
    queue_state.active_jobs = (
        (db.scalar(select(func.count()).select_from(AiJob).where(
            AiJob.queue_reserved.is_(True), AiJob.status.in_(active_statuses))) or 0)
        + (db.scalar(select(func.count()).select_from(SuggestionJob).where(
            SuggestionJob.queue_reserved.is_(True), SuggestionJob.status.in_(active_statuses))) or 0)
    )
    queue_state.updated_at = utcnow()
    db.flush()
    if recovered_reservations:
        logger.warning("queue.reservations.recovered", extra={
            "event": "queue.reservations.recovered", "component": "queue",
            "recovered_reservations": recovered_reservations,
        })
    return queue_state.active_jobs, released_reservations


def _reconcile_queue_capacity_or_raise(db: Session, *, environment: str) -> tuple[int, int]:
    """Reconcile durable admission state and enforce the production invariant."""
    try:
        return reconcile_queue_capacity(db)
    except SQLAlchemyError as exc:
        db.rollback()
        external_worker = os.getenv("AI_STUDIO_EXTERNAL_WORKER", "0").lower() in {"1", "true", "yes", "on"}
        if environment == "production" or external_worker:
            logger.error("queue.capacity.reconcile_failed", extra={
                "event": "queue.capacity.reconcile_failed", "error_type": type(exc).__name__,
                "required": True,
            })
            raise RuntimeError("队列容量状态无法校验，服务拒绝启动") from exc
        logger.warning("queue.capacity.reconcile_failed", extra={
            "event": "queue.capacity.reconcile_failed", "error_type": type(exc).__name__,
            "required": False,
        })
        return 0, 0


def initialize():
    environment = os.getenv("AI_STUDIO_ENV", "development").strip().lower()
    _validate_runtime_security()
    default_require_migrations = "1" if environment == "production" else "0"
    require_migrations = os.getenv("AI_STUDIO_REQUIRE_MIGRATIONS", default_require_migrations).lower() in {"1", "true", "yes", "on"}
    _validate_migration_policy(environment, require_migrations)
    data_dir = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")).expanduser()
    try:
        ensure_private_directory(data_dir)
    except OSError as exc:
        logger.error("storage.initialize.failed", extra={
            "event": "storage.initialize.failed", "component": "storage",
            "error_type": type(exc).__name__,
        })
        raise RuntimeError("数据目录不可用") from exc
    logger.info("storage.initialized", extra={
        "event": "storage.initialized", "component": "storage",
    })
    # Local one-command development may bootstrap a brand-new SQLite file.
    # Production refuses implicit DDL and relies on the explicit Alembic step
    # in the container/startup procedure instead.
    if not require_migrations:
        Base.metadata.create_all(engine)
    from . import ai_engine

    with SessionLocal() as db:
        expected_head = os.getenv("AI_STUDIO_MIGRATION_HEAD", "20261032_case_timestamps")
        try:
            migration_head = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except SQLAlchemyError as exc:
            migration_head = None
            if require_migrations:
                raise RuntimeError("数据库迁移版本表不可用，请先执行 alembic upgrade head") from exc
            logger.warning("database.migrations.unavailable", extra={
                "event": "database.migrations.unavailable", "component": "migrations",
                "error_type": type(exc).__name__, "required": False,
            })
        else:
            logger.info("database.migrations.checked", extra={
                "event": "database.migrations.checked", "component": "migrations",
                "revision": migration_head or "unversioned", "required": require_migrations,
                "database_kind": database_kind,
            })
        if require_migrations and migration_head != expected_head:
            logger.error("database.migrations.version_mismatch", extra={
                "event": "database.migrations.version_mismatch", "component": "migrations",
                "error_type": "MigrationVersionMismatch", "revision": migration_head or "unversioned",
                "expected_revision": expected_head, "database_kind": database_kind,
            })
            raise RuntimeError(f"数据库迁移版本不匹配：当前 {migration_head or '空'}，需要 {expected_head}")
        if not db.scalar(select(User.id).limit(1)):
            password = os.getenv("AI_STUDIO_ADMIN_PASSWORD", "").strip()
            # Local one-click startup may leave the prompt empty; keep a
            # predictable six-character bootstrap password for that case.
            if not password:
                if os.getenv("AI_STUDIO_ENV", "development").strip().lower() == "production":
                    raise RuntimeError("Set AI_STUDIO_ADMIN_PASSWORD (at least 6 characters) before first startup")
                password = "123456"
            if len(password) < 6:
                raise RuntimeError("AI_STUDIO_ADMIN_PASSWORD must contain at least 6 characters")
            admin_username = (os.getenv("AI_STUDIO_ADMIN_USER", "admin").strip() or "admin")
            db.add(User(username=admin_username, password_hash=hasher.hash(password), role="admin"))
            try:
                db.commit()
            except IntegrityError:
                # Multiple API replicas can observe an empty database at the
                # same time. The first insert wins; the other replica rolls
                # back and uses the row that was committed concurrently.
                db.rollback()
                existing_admin = db.scalar(select(User.id).where(User.username == admin_username))
                if existing_admin is None:
                    raise
                logger.info("database.bootstrap.user_race", extra={
                    "event": "database.bootstrap.user_race", "component": "bootstrap",
                })
        settings = db.get(AiSettings, 1)
        settings_seeded = False
        legacy_settings_seeded = False
        if not settings:
            # Seed a brand-new database from environment defaults so a
            # headless deployment can configure its provider before the first
            # browser login. Later UI edits remain authoritative.
            settings = AiSettings(
                id=1,
                local_url="",
                local_model="",
                remote_enabled=os.getenv("AI_STUDIO_REMOTE_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
                remote_url=os.getenv("AI_STUDIO_REMOTE_URL", "").rstrip("/"),
                remote_model=os.getenv("AI_STUDIO_REMOTE_MODEL", ""),
                codex_enabled=os.getenv("AI_STUDIO_CODEX_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
                codex_command=os.getenv("AI_STUDIO_CODEX_COMMAND", "codex") or "codex",
                codex_model=os.getenv("AI_STUDIO_CODEX_MODEL", ""),
                claude_enabled=os.getenv("AI_STUDIO_CLAUDE_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
                claude_command=os.getenv("AI_STUDIO_CLAUDE_COMMAND", "claude") or "claude",
                claude_model=os.getenv("AI_STUDIO_CLAUDE_MODEL", ""),
            )
            remote_key = os.getenv("AI_STUDIO_REMOTE_API_KEY", "").strip()
            if remote_key:
                settings.remote_api_key = ai_engine.encrypt_secret(remote_key)
            db.add(settings)
            try:
                db.commit()
            except IntegrityError:
                # AiSettings is a singleton. Preserve the settings committed
                # by another replica rather than overwriting its values.
                db.rollback()
                settings = db.get(AiSettings, 1)
                if not settings:
                    raise
                logger.info("database.bootstrap.settings_race", extra={
                    "event": "database.bootstrap.settings_race", "component": "bootstrap",
                })
            else:
                settings_seeded = True
        elif _environment_provider_overrides() and _is_legacy_bootstrap_settings(settings):
            # Alembic creates a bootstrap singleton before the API starts. If
            # it is still untouched, honor deployment environment values just
            # as we do for a brand-new database. A later UI save is kept
            # authoritative because it no longer matches this exact shape.
            _seed_existing_bootstrap_settings(settings, ai_engine)
            legacy_settings_seeded = True
        # Cloud-only mode: remove legacy Ollama catalog entries and clear the
        # old singleton fields. Keeping a disabled local row would make it
        # possible for a stale worker or UI to select on-device inference.
        db.execute(delete(AiModel).where(AiModel.scope == "local"))
        try:
            catalog = json.loads(settings.providers_json or "[]")
        except (TypeError, ValueError):
            catalog = []
        cloud_catalog = [item for item in catalog if isinstance(item, dict) and item.get("kind") == "remote"]
        settings.local_url = ""
        settings.local_model = ""
        settings.providers_json = json.dumps(cloud_catalog, ensure_ascii=False)
        # Existing databases created before CLI provider support have empty
        # command columns; normalize them before loading the runtime config.
        changed = False
        if not settings.codex_command:
            settings.codex_command = "codex"
            changed = True
        if not settings.claude_command:
            settings.claude_command = "claude"
            changed = True
        if changed:
            db.commit()
        # Keep the normalized tool table populated for development databases
        # that bootstrap via ``create_all`` instead of running Alembic.
        tool_defaults = (("codex_cli", "Codex CLI", settings.codex_enabled, settings.codex_command, settings.codex_model),
                         ("claude_code", "Claude Code", settings.claude_enabled, settings.claude_command, settings.claude_model))
        tools_changed = False
        for tool_id, name, enabled, command, model in tool_defaults:
            tool = db.scalar(select(AiTool).where(AiTool.tool_id == tool_id))
            if tool is None:
                db.add(AiTool(tool_id=tool_id, name=name, enabled=bool(enabled), command=command or name.lower(), model=model or ""))
                tools_changed = True
            elif legacy_settings_seeded:
                # Model/tool rows are the runtime source of truth after the
                # catalog migration, so keep them in sync with the bootstrap
                # environment values as well.
                tool.enabled = bool(enabled)
                tool.command = command or name.lower()
                tool.model = model or ""
                tool.updated_at = utcnow()
                tools_changed = True
        if tools_changed:
            db.commit()
        # ``create_all`` development databases also need built-in model rows;
        # production migrations perform the equivalent backfill.
        model_defaults = (
            ("remote", "云端模型", "remote", settings.remote_url, settings.remote_model, bool(settings.remote_enabled)),
        )
        models_changed = False
        for provider_id, name, scope, endpoint, model, enabled in model_defaults:
            model_row = db.scalar(select(AiModel).where(AiModel.provider_id == provider_id))
            if model_row is None:
                db.add(AiModel(provider_id=provider_id, name=name, scope=scope, endpoint=endpoint or "",
                               model=model or "", enabled=enabled,
                               api_key_encrypted=settings.remote_api_key if provider_id == "remote" else ""))
                models_changed = True
            elif legacy_settings_seeded and provider_id == "remote":
                model_row.endpoint = endpoint or ""
                model_row.model = model or ""
                model_row.enabled = bool(enabled)
                if provider_id == "remote" and settings.remote_api_key:
                    model_row.api_key_encrypted = settings.remote_api_key
                model_row.updated_at = utcnow()
                models_changed = True
        if models_changed:
            db.commit()
        # Normalized model/tool rows are authoritative after migration. Keep
        # legacy singleton fields as a fallback for old/unmigrated databases.
        model_rows = {row.provider_id: row for row in db.scalars(select(AiModel)).all()}
        tool_rows = {row.tool_id: row for row in db.scalars(select(AiTool)).all()}
        remote_row = model_rows.get("remote")
        codex_row = tool_rows.get("codex_cli")
        claude_row = tool_rows.get("claude_code")
        local_url = ""
        local_model = ""
        local_enabled = False
        remote_enabled = remote_row.enabled if remote_row else settings.remote_enabled
        remote_url = remote_row.endpoint if remote_row else settings.remote_url
        remote_model = remote_row.model if remote_row else settings.remote_model
        # The singleton keeps the canonical remote secret; row value is a
        # migration fallback for databases created before that field existed.
        remote_secret = settings.remote_api_key or (remote_row.api_key_encrypted if remote_row else "")
        codex_enabled = codex_row.enabled if codex_row else settings.codex_enabled
        codex_command = codex_row.command if codex_row else settings.codex_command
        codex_model = codex_row.model if codex_row else settings.codex_model
        claude_enabled = claude_row.enabled if claude_row else settings.claude_enabled
        claude_command = claude_row.command if claude_row else settings.claude_command
        claude_model = claude_row.model if claude_row else settings.claude_model
        # Keep the operator-facing control-plane snapshot on the durable data
        # volume. Skill entries intentionally contain metadata and paths only.
        try:
            config_file.persist_skills(discover_skills(), root=str(skill_root()))
        except (SkillRunnerError, OSError) as exc:
            logger.warning("config.file.skills_snapshot_failed", extra={
                "event": "config.file.skills_snapshot_failed", "component": "config-file",
                "error_type": type(exc).__name__,
            })
        try:
            config_file.persist_model_settings(settings)
        except OSError as exc:
            logger.error("config.file.write_failed", extra={
                "event": "config.file.write_failed", "component": "config-file",
                "error_type": type(exc).__name__,
            })
            raise RuntimeError("核心配置文件不可写，请检查数据目录权限") from exc
        # Reconcile the singleton admission counter from durable job flags on
        # every API start. This repairs a crash between a job finalization and
        # the reservation counter update without trusting stale process state.
        active_jobs, released_reservations = _reconcile_queue_capacity_or_raise(db, environment=environment)
        db.commit()
        metrics.set_ai_queue(active_jobs, env_int("AI_STUDIO_MAX_SUBMITTED_JOBS", 100, 1))
        logger.info("queue.capacity.reconciled", extra={
            "event": "queue.capacity.reconciled", "component": "queue",
            "active_jobs": active_jobs,
            "released_reservations": released_reservations,
        })
        try:
            # A disabled provider may retain an old encrypted key so an admin
            # can re-enable it later. Only an enabled provider should block
            # startup when a session-secret rotation makes that key unreadable.
            remote_api_key = (
                ai_engine.decrypt_secret_checked(remote_secret)
                if remote_enabled else ai_engine.decrypt_secret(remote_secret)
            )
        except RuntimeError:
            logger.error("ai.settings.secret_unavailable", extra={
                "event": "ai.settings.secret_unavailable", "component": "ai-settings",
                "error_type": "SecretDecryptionError",
            })
            raise
        ai_engine.apply_config({"local_url": local_url, "local_model": local_model,
                                "local_enabled": local_enabled,
                                "remote_enabled": remote_enabled, "remote_url": remote_url,
                                "remote_model": remote_model,
                                "remote_api_key": remote_api_key,
                                "codex_enabled": codex_enabled, "codex_command": codex_command,
                                "codex_model": codex_model, "claude_enabled": claude_enabled,
                                "claude_command": claude_command, "claude_model": claude_model})
        if settings_seeded or legacy_settings_seeded:
            logger.info("ai.settings.seeded", extra={
                "event": "ai.settings.seeded", "component": "ai-settings", "source": "environment",
            })
        ai_engine.log_cli_availability("api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _ready_for_traffic
    started = monotonic()
    try:
        # Uvicorn applies its own logging config after module import; refresh
        # the application sinks here so request and lifecycle logs stay JSON.
        configure_logging()
        initialize()
        from . import ai_jobs
        from .suggestion_jobs import recover_jobs as recover_suggestion_jobs
        ai_jobs.start_workers()
        ai_jobs.recover_jobs()
        recover_suggestion_jobs()
        _ready_for_traffic = True
    except Exception as exc:
        logger.exception("application.start_failed", extra={
            "event": "application.start_failed", "error_type": type(exc).__name__, "process_pid": os.getpid(),
        })
        try:
            from . import ai_jobs
            timeout = max(0.0, float(os.getenv("AI_STUDIO_SHUTDOWN_TIMEOUT", "25")))
            remaining = ai_jobs.shutdown_workers(timeout)
            logger.warning("application.start_cleanup", extra={
                "event": "application.start_cleanup", "component": "ai-worker",
                "active_jobs": remaining, "shutdown_timeout": timeout,
            })
        except Exception as cleanup_error:
            logger.error("application.start_cleanup.failed", extra={
                "event": "application.start_cleanup.failed", "component": "ai-worker",
                "error_type": type(cleanup_error).__name__,
            })
        raise
    logger.info("application.started", extra={"event": "application.started", "duration_ms": round((monotonic() - started) * 1000, 2),
                                                "process_pid": os.getpid(), "queue_limit": ai_jobs.MAX_SUBMITTED_JOBS,
                                                "session_max_age": SESSION_MAX_AGE, "max_request_bytes": MAX_REQUEST_BYTES,
                                                "component": "external-worker" if ai_jobs.EXTERNAL_WORKER else "embedded-worker",
                                                "database_kind": database_kind})
    try:
        yield
    finally:
        _ready_for_traffic = False
        from . import ai_jobs
        timeout = max(0.0, float(os.getenv("AI_STUDIO_SHUTDOWN_TIMEOUT", "25")))
        remaining = ai_jobs.shutdown_workers(timeout)
        logger.info("application.stopping", extra={"event": "application.stopping", "process_pid": os.getpid(),
                                                    "active_jobs": remaining, "shutdown_timeout": timeout})
        logger.info("application.stopped", extra={"event": "application.stopped", "process_pid": os.getpid(),
                                                   "active_jobs": remaining})


app = FastAPI(title="Test Atelier", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict",
                   max_age=SESSION_MAX_AGE, https_only=os.getenv("COOKIE_SECURE") == "1")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Return a stable error envelope while keeping traceback out of clients."""
    correlation_id = getattr(request.state, "request_id", "") or new_request_id(request.headers.get("x-request-id"))
    token = set_request_id(correlation_id)
    try:
        logger.exception("request.unhandled", extra={
            "event": "request.unhandled", "method": request.method,
            "path": _log_path(request), "error_type": type(exc).__name__, "process_pid": os.getpid(),
        })
    finally:
        reset_request_id(token)
    response = JSONResponse(status_code=500, content={"detail": "服务器内部错误", "request_id": correlation_id})
    _set_response_headers(response, request, correlation_id)
    return response


def _set_response_headers(response, request: Request, correlation_id: str) -> None:
    """Apply correlation and baseline security headers to every API response."""
    response.headers["X-Request-ID"] = correlation_id
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Correlate and time requests without recording body or query contents."""
    correlation_id = new_request_id(request.headers.get("x-request-id"))
    request.state.request_id = correlation_id
    token = set_request_id(correlation_id)
    started = monotonic()
    metrics.begin_request()
    response = None
    error_type = None
    try:
        # Content-Length is easy to spoof or omit with chunked transfer. Wrap
        # the ASGI receive channel as a second line of defense so large
        # streamed JSON bodies cannot bypass the global request limit.
        original_receive = request._receive
        # Keep the larger limit scoped to document uploads; all other routes
        # retain the conservative global request ceiling.
        request_limit = (MAX_DOCUMENT_REQUEST_BYTES
                         if request.method in {"POST", "PUT", "PATCH"}
                         and request.url.path.rstrip("/").endswith("/documents")
                         else MAX_REQUEST_BYTES)
        received_bytes = 0
        declared_bytes = None

        async def limited_receive():
            nonlocal received_bytes
            message = await original_receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body") or b"")
                if received_bytes > request_limit:
                    raise RequestBodyTooLarge()
            return message

        request._receive = limited_receive
        content_length = request.headers.get("content-length", "").strip()
        if content_length:
            try:
                declared_bytes = int(content_length)
                too_large = declared_bytes > request_limit
            except ValueError:
                too_large = False
            if too_large:
                response = JSONResponse(status_code=413, content={
                    "detail": "请求体超过大小限制", "request_id": correlation_id,
                })
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        return response
    except RequestBodyTooLarge:
        error_type = "RequestBodyTooLarge"
        response = JSONResponse(status_code=413, content={
            "detail": "请求体超过大小限制", "request_id": correlation_id,
        })
        return response
    except Exception as exc:
        error_type = type(exc).__name__
        logger.exception("request.failed", extra={
            "event": "request.failed", "method": request.method,
            "path": _log_path(request), "error_type": error_type, "process_pid": os.getpid(),
        })
        raise
    finally:
        duration_ms = round((monotonic() - started) * 1000, 2)
        status = response.status_code if response is not None else 500
        if response is not None:
            _set_response_headers(response, request, correlation_id)
        fields = {
            "event": "request.completed", "method": request.method,
            "path": _log_path(request), "route": _route_template(request),
            "status": status, "duration_ms": duration_ms, "process_pid": os.getpid(),
        }
        if status == 413:
            # Record only the byte count, never the rejected request body.
            fields["request_bytes"] = received_bytes
            if declared_bytes is not None:
                fields["declared_bytes"] = declared_bytes
        if error_type:
            fields["error_type"] = error_type
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        logger.log(level, "request.completed", extra=fields)
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            audit_fields = {
                "event": "request.audit", "method": request.method,
                "route": _route_template(request), "status": status,
                "duration_ms": duration_ms, "process_pid": os.getpid(), "component": "audit",
            }
            actor_id = request.scope.get("session", {}).get("uid")
            if actor_id is not None:
                audit_fields["actor_id"] = actor_id
            logger.info("request.audit", extra=audit_fields)
        metrics.observe_request(request.method, _route_template(request), status, duration_ms)
        reset_request_id(token)


def get_db():
    with SessionLocal() as db:
        yield db


@app.get("/api/v1/health", include_in_schema=False)
@app.get("/api/v1/health/live", include_in_schema=False)
def health_live():
    payload = {"status": "ok", "service": "test-atelier", "version": app.version}
    release = deployment_release()
    if release:
        payload["release"] = release
    return payload


@app.get("/api/v1/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    if not _ready_for_traffic:
        raise HTTPException(503, "服务正在启动或停止")
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        _log_readiness_failure("readiness.failed", error_type=type(exc).__name__)
        raise HTTPException(503, "数据库暂不可用") from exc
    configured_skill_root = os.getenv("TEST_ATELIER_SKILL_ROOT", "").strip()
    if configured_skill_root and not os.path.isdir(configured_skill_root):
        _log_readiness_failure("readiness.failed", component="skill_root", error_type="MissingDirectory")
        raise HTTPException(503, "技能仓目录不可用")
    missing_skill_files = _configured_skill_root_missing()
    if missing_skill_files:
        _log_readiness_failure("readiness.failed", component="skill_root", error_type="MissingWorkflowFiles",
                               required=len(missing_skill_files))
        raise HTTPException(503, "技能仓工作流文件不可用")
    data_dir = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")).expanduser()
    if data_dir.is_symlink() or not data_dir.is_dir() or not os.access(data_dir, os.W_OK):
        _log_readiness_failure("readiness.failed", component="storage",
                               error_type="StorageSymlink" if data_dir.is_symlink() else "StorageUnavailable")
        raise HTTPException(503, "数据目录不可写")
    payload = {"status": "ready", "service": "test-atelier", "version": app.version}
    release = deployment_release()
    if release:
        payload["release"] = release
    return payload


@app.get("/api/v1/health/worker", include_in_schema=False)
def health_worker():
    """Report the durable worker heartbeat without making API readiness circular."""
    from . import ai_jobs
    if not ai_jobs.EXTERNAL_WORKER:
        metrics.set_worker_health(False)
        _log_readiness_failure("worker.readiness.disabled", level=logging.WARNING, component="external-worker")
        payload = {"status": "disabled", "service": "test-atelier", "component": "external-worker"}
        release = deployment_release()
        if release:
            payload["release"] = release
        return payload
    heartbeat = _worker_heartbeat_path()
    threshold = max(30.0, env_float("AI_STUDIO_WORKER_HEARTBEAT_SECONDS", 5, 1.0) * 3)
    metrics.set_worker_heartbeat_threshold(threshold)
    try:
        age = _heartbeat_age(heartbeat)
    except OSError as exc:
        metrics.set_worker_health(False)
        _log_readiness_failure("worker.readiness.failed", component="external-worker",
                               error_type=type(exc).__name__)
        raise HTTPException(503, "AI worker 心跳不可用") from exc
    if age > threshold:
        metrics.set_worker_health(False, age)
        _log_readiness_failure("worker.readiness.stale", component="external-worker",
                               worker_age_seconds=round(age, 3))
        raise HTTPException(503, "AI worker 心跳已过期")
    metrics.set_worker_health(True, age)
    payload = {"status": "ready", "service": "test-atelier", "component": "external-worker",
               "heartbeat_age_seconds": round(age, 3)}
    try:
        worker_release = _heartbeat_release(heartbeat)
    except OSError as exc:
        # The heartbeat can be replaced during a restart. Treat a vanished or
        # unsafe metadata file as the same unavailable dependency as a stale
        # mtime instead of leaking a generic 500 from the health endpoint.
        metrics.set_worker_health(False, age)
        _log_readiness_failure("worker.readiness.failed", component="external-worker",
                               error_type=type(exc).__name__)
        raise HTTPException(503, "AI worker 心跳元数据不可用") from exc
    if worker_release:
        payload["worker_release"] = worker_release
    release = deployment_release()
    if release:
        payload["release"] = release
        if worker_release and worker_release != release:
            _log_readiness_failure(
                "worker.readiness.version_mismatch",
                level=logging.WARNING,
                component="external-worker",
                error_type="ReleaseMismatch",
                worker_release=worker_release,
            )
        elif not worker_release:
            # A legacy worker may still update the heartbeat without recording
            # its build. Keep readiness green for backwards compatibility,
            # but surface the unverifiable rollout state to operators.
            _log_readiness_failure(
                "worker.readiness.version_unknown",
                level=logging.WARNING,
                component="external-worker",
                error_type="MissingWorkerRelease",
            )
    return payload


def _refresh_database_pool_metrics() -> None:
    """Read SQLAlchemy pool gauges without making a diagnostic DB request."""
    pool = getattr(engine, "pool", None)
    if pool is None:
        return
    try:
        def value(name: str, default: int = 0) -> int:
            attribute = getattr(pool, name, None)
            return int(attribute() if callable(attribute) else default)

        metrics.set_database_pool(
            value("size"), value("checkedout"), value("overflow"),
        )
    except Exception:
        # Pool implementations differ between SQLite and PostgreSQL. Metrics
        # must never turn a healthy diagnostics endpoint into a 500 response.
        metrics.set_database_pool(0, 0, 0)


@app.get("/api/v1/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics_endpoint(request: Request):
    """Expose low-cardinality Prometheus metrics for internal scrapers."""
    if not metrics.enabled():
        raise HTTPException(404, "Not found")
    configured_token = os.getenv("METRICS_TOKEN", "").strip()
    if configured_token:
        supplied = request.headers.get("x-metrics-token", "")
        authorization = request.headers.get("authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        # Authenticate before touching the database or worker heartbeat so an
        # unauthorised scrape cannot turn this diagnostic endpoint into a
        # repeated database/IO probe.
        if not secrets.compare_digest(supplied, configured_token):
            logger.warning("metrics.auth.failed", extra={
                "event": "metrics.auth.failed", "component": "metrics",
                "error_type": "InvalidMetricsToken",
            })
            raise HTTPException(401, "Metrics authentication required")
    from . import ai_jobs
    if not ai_jobs.EXTERNAL_WORKER:
        metrics.set_worker_health(False)
    else:
        threshold = max(30.0, env_float("AI_STUDIO_WORKER_HEARTBEAT_SECONDS", 5, 1.0) * 3)
        metrics.set_worker_heartbeat_threshold(threshold)
        # Queue reservations are finalized in the external worker process,
        # so refresh this gauge from the durable singleton row before serving
        # metrics instead of exposing a stale API-process snapshot.
        try:
            with SessionLocal() as db:
                active_jobs = db.scalar(select(AiQueueState.active_jobs).where(AiQueueState.id == 1)) or 0
            metrics.set_ai_queue(active_jobs, env_int("AI_STUDIO_MAX_SUBMITTED_JOBS", 100, 1))
        except SQLAlchemyError as exc:
            logger.warning("metrics.queue_refresh_failed", extra={
                "event": "metrics.queue_refresh_failed", "component": "external-queue",
                "error_type": type(exc).__name__,
            })
        heartbeat = _worker_heartbeat_path()
        try:
            age = _heartbeat_age(heartbeat)
            metrics.set_worker_health(age <= threshold, age)
        except OSError:
            metrics.set_worker_health(False)
    _refresh_database_pool_metrics()
    return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


def current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("uid")
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def csrf(request: Request):
    expected = request.session.get("csrf")
    submitted = request.headers.get("x-csrf-token", "")
    if not expected or not secrets.compare_digest(expected, submitted):
        raise HTTPException(403, "CSRF 校验失败")


def project_access(project_id: int, user: User, db: Session):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if user.role != "admin" and not db.get(Membership, (project_id, user.id)):
        raise HTTPException(403, "无项目访问权限")
    return project


def project_role(project_id: int, user: User, db: Session) -> str:
    project_access(project_id, user, db)
    if user.role == "admin":
        return "admin"
    return db.get(Membership, (project_id, user.id)).role


def writable(user: User, project_id: int, db: Session):
    project = project_access(project_id, user, db)
    if project.archived_at:
        raise HTTPException(409, "项目已归档，请先恢复后再修改")
    if project_role(project_id, user, db) == "reader":
        raise HTTPException(403, "只读账号不能修改")


def project_manager(user: User, project_id: int, db: Session):
    project = project_access(project_id, user, db)
    if project.archived_at:
        raise HTTPException(409, "项目已归档，请先恢复后再修改")
    if project_role(project_id, user, db) not in ("admin", "manager"):
        raise HTTPException(403, "仅项目管理员可管理成员")


def ensure_project_skills(project_id: int, db: Session) -> list[ProjectSkill]:
    """Materialize the source skill registry for a project on first access."""
    try:
        specs = discover_skills()
    except SkillRunnerError as exc:
        logger.warning("project.skills.unavailable", extra={
            "event": "project.skills.unavailable",
            "error_type": type(exc).__name__, "component": "skill-runner",
        })
        raise HTTPException(503, "项目技能仓不可用") from exc
    try:
        config_file.persist_skills(specs)
    except OSError as exc:
        logger.error("config.file.write_failed", extra={
            "event": "config.file.write_failed", "component": "config-file",
            "error_type": type(exc).__name__,
        })
        raise HTTPException(503, "核心配置文件不可写") from exc
    existing = {row.skill_id: row for row in db.scalars(select(ProjectSkill).where(ProjectSkill.project_id == project_id)).all()}
    changed = False
    for spec in specs:
        if spec.skill_id not in existing:
            row = ProjectSkill(project_id=project_id, skill_id=spec.skill_id, name=spec.name,
                               label=spec.label, source_path=spec.path, description=spec.description,
                               enabled=spec.enabled_by_default)
            db.add(row); existing[spec.skill_id] = row; changed = True
        else:
            row = existing[spec.skill_id]
            row.name, row.label, row.source_path, row.description = spec.name, spec.label, spec.path, spec.description
    if changed:
        db.commit()
    return list(existing.values())


def release_assignments(project_id: int, member_id: int, actor_id: int, db: Session):
    case_ids = db.scalars(update(Case).where(Case.project_id == project_id, Case.assignee_id == member_id)
                          .values(assignee_id=None, version=Case.version + 1).returning(Case.id)).all()
    for case_id in case_ids:
        db.add(ReviewEvent(case_id=case_id, user_id=actor_id, action="assignment",
                           before={"assignee_id": member_id}, after={"assignee_id": None},
                           reason="成员权限变更，负责人已清除"))


def _require_project_manager(project_id: int, user: User, db: Session) -> Project:
    """Return a project when the actor can administer its lifecycle."""
    project = project_access(project_id, user, db)
    if user.role != "admin":
        membership = db.get(Membership, (project_id, user.id))
        if not membership or membership.role != "manager":
            raise HTTPException(403, "仅项目管理员可管理项目")
    return project


def _delete_project_data(project_id: int, db: Session) -> None:
    """Delete project-owned rows in dependency order.

    The baseline schema predates explicit ORM relationships and most foreign
    keys do not have ``ON DELETE CASCADE``.  Keep deletion explicit so this
    endpoint behaves consistently on SQLite and PostgreSQL and never leaves
    orphaned review, AI, or execution records behind.
    """
    # A running worker may still hold a project job.  Reject deletion rather
    # than racing a worker that could recreate references after this cleanup.
    active_ai = db.scalar(select(func.count()).select_from(AiJob).where(
        AiJob.project_id == project_id, AiJob.status.in_(("queued", "running")))) or 0
    active_suggestions = db.scalar(select(func.count()).select_from(SuggestionJob).where(
        SuggestionJob.project_id == project_id, SuggestionJob.status.in_(("queued", "running")))) or 0
    if active_ai or active_suggestions:
        raise HTTPException(409, "项目存在运行中的任务，请稍后再删除")

    # Keep personal board entries while detaching them from a deleted project.
    # The board is user-owned and should not lose unrelated task history.
    db.execute(update(WorkItem).where(WorkItem.project_id == project_id).values(project_id=None, updated_at=utcnow()))

    # Remove uploaded source documents and generated conversion files before
    # deleting their history rows. Paths are derived from durable IDs and are
    # kept under their respective private storage roots.
    try:
        from . import ai_routes
        document_dir = ai_routes.DOCUMENT_DIR / str(project_id)
        for document in db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id)).all():
            (document_dir / f"{document.id}.bin").unlink(missing_ok=True)
        document_dir.rmdir()
    except (OSError, ValueError):
        logger.warning("project.cleanup_document_files_failed", extra={
            "event": "project.cleanup_document_files_failed", "project_id": project_id,
            "component": "project", "error_type": "OSError",
        })
    try:
        from .conversion_routes import DATA_DIR, output_path
        for record in db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id)).all():
            output_path(record).unlink(missing_ok=True)
        conversion_dir = DATA_DIR / str(project_id)
        if conversion_dir.exists():
            conversion_dir.rmdir()
    except OSError:
        # Database deletion remains safe even if an old output file is already
        # gone or the configured data directory is read-only.
        logger.warning("project.cleanup_file_failed", extra={
            "event": "project.cleanup_file_failed", "project_id": project_id,
            "component": "project", "error_type": "OSError",
        })

    def ids(model, column):
        return list(db.scalars(select(model.id).where(column == project_id)).all())

    suite_ids = ids(SmokeSuite, SmokeSuite.project_id)
    run_ids = ids(ExecutionRun, ExecutionRun.project_id)
    case_ids = ids(Case, Case.project_id)
    analysis_ids = ids(AnalysisRun, AnalysisRun.project_id)
    suggestion_job_ids = ids(SuggestionJob, SuggestionJob.project_id)

    if run_ids:
        db.execute(delete(ExecutionEvent).where(ExecutionEvent.run_case_id.in_(
            select(RunCase.id).where(RunCase.run_id.in_(run_ids)))))
        db.execute(delete(RunCase).where(RunCase.run_id.in_(run_ids)))
    if suite_ids:
        db.execute(delete(SmokeItem).where(SmokeItem.suite_id.in_(suite_ids)))
    if run_ids:
        db.execute(delete(ExecutionRun).where(ExecutionRun.id.in_(run_ids)))
    if suite_ids:
        db.execute(delete(SmokeSuite).where(SmokeSuite.id.in_(suite_ids)))

    if case_ids:
        db.execute(delete(ReviewEvent).where(ReviewEvent.case_id.in_(case_ids)))
        db.execute(delete(CaseComment).where(CaseComment.case_id.in_(case_ids)))
        db.execute(delete(CaseSuggestion).where(CaseSuggestion.case_id.in_(case_ids)))
        db.execute(delete(CaseDraft).where(CaseDraft.case_id.in_(case_ids)))
    if suggestion_job_ids:
        db.execute(delete(CaseSuggestion).where(CaseSuggestion.job_id.in_(suggestion_job_ids)))
    if analysis_ids:
        db.execute(delete(AiJob).where(AiJob.analysis_id.in_(analysis_ids)))
        db.execute(delete(CaseDraft).where(CaseDraft.analysis_id.in_(analysis_ids)))
    db.execute(delete(CaseSuggestion).where(CaseSuggestion.project_id == project_id))
    db.execute(delete(CaseDraft).where(CaseDraft.project_id == project_id))
    db.execute(delete(AiJob).where(AiJob.project_id == project_id))
    db.execute(delete(SuggestionJob).where(SuggestionJob.project_id == project_id))
    db.execute(delete(Case).where(Case.project_id == project_id))
    db.execute(delete(ImportBatch).where(ImportBatch.project_id == project_id))
    db.execute(delete(AnalysisRun).where(AnalysisRun.project_id == project_id))
    db.execute(delete(SourceDocument).where(SourceDocument.project_id == project_id))
    db.execute(delete(ConversionRecord).where(ConversionRecord.project_id == project_id))
    db.execute(delete(ConversionSettings).where(ConversionSettings.project_id == project_id))
    db.execute(delete(TaxonomyVersion).where(TaxonomyVersion.project_id == project_id))
    db.execute(delete(ProjectSkill).where(ProjectSkill.project_id == project_id))
    codex_sessions = db.scalars(select(CodexSession).where(CodexSession.project_id == project_id)).all()
    codex_session_ids = select(CodexSession.id).where(CodexSession.project_id == project_id)
    db.execute(delete(CodexMessage).where(CodexMessage.session_id.in_(codex_session_ids)))
    db.execute(delete(CodexSession).where(CodexSession.project_id == project_id))
    session_root = (Path(tempfile.gettempdir()) / "test-atelier-codex-sessions").resolve()
    for session in codex_sessions:
        workspace = Path(session.working_directory).resolve()
        if workspace.parent == session_root and workspace.name == session.id:
            shutil.rmtree(workspace, ignore_errors=True)
    db.execute(delete(MembershipEvent).where(MembershipEvent.project_id == project_id))
    db.execute(delete(Membership).where(Membership.project_id == project_id))


class Login(BaseModel):
    username: str
    password: str


class Named(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class ProjectDuplicate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=256)
    role: str


class PasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=256)


class MembershipCreate(BaseModel):
    user_id: int | None = None
    username: str | None = None
    role: Literal["manager", "reviewer", "reader"] | None = None


class MembershipRoleUpdate(BaseModel):
    role: Literal["manager", "reviewer", "reader"]


class ProjectMemberCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=256)
    role: Literal["manager", "reviewer", "reader"] = "reviewer"


class WorkItemCreate(BaseModel):
    kind: str = Field(default="other", min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=10000)
    status: str = Field(default="todo", min_length=1, max_length=24)
    start_at: str | None = Field(default=None, max_length=40)
    due_at: str | None = Field(default=None, max_length=40)
    project_id: int | None = None


class WorkItemUpdate(BaseModel):
    kind: str | None = Field(default=None, min_length=1, max_length=32)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, min_length=1, max_length=24)
    start_at: str | None = Field(default=None, max_length=40)
    due_at: str | None = Field(default=None, max_length=40)
    project_id: int | None = None


class SkillUpdate(BaseModel):
    enabled: bool


class Review(BaseModel):
    version: int
    final_level: str
    final_smoke: str
    reason: str = Field(min_length=1, max_length=1000)


class Assignment(BaseModel):
    version: int
    assignee_id: int | None = None


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class BatchReviewItem(BaseModel):
    id: int
    version: int
    final_level: str
    final_smoke: str


class BatchReview(BaseModel):
    items: list[BatchReviewItem] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class BatchDelete(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=100)


@app.get("/api/v1/auth/csrf")
def csrf_token(request: Request):
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    return {"token": request.session["csrf"]}


@app.post("/api/v1/auth/login", dependencies=[Depends(csrf)])
def login(payload: Login, request: Request, db: Session = Depends(get_db)):
    failure_keys = _login_failure_keys(request, payload.username)
    if _login_failures.blocked(failure_keys):
        retry_after = _login_failures.retry_after(failure_keys)
        logger.warning("auth.login.rate_limited", extra={
            "event": "auth.login.rate_limited", "error_type": "too_many_attempts",
            "retry_after_seconds": retry_after,
        })
        raise HTTPException(429, "登录失败次数过多，请稍后重试",
                            headers={"Retry-After": str(retry_after)})
    user = db.scalar(select(User).where(User.username == payload.username))
    try:
        valid = bool(user and hasher.verify(user.password_hash, payload.password))
    except Exception:
        valid = False
    if not valid:
        _login_failures.record(failure_keys)
        logger.warning("auth.login.failed", extra={"event": "auth.login.failed", "error_type": "invalid_credentials"})
        raise HTTPException(401, "用户名或密码错误")
    _login_failures.clear(failure_keys)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["csrf"] = secrets.token_urlsafe(32)
    logger.info("auth.login.succeeded", extra={"event": "auth.login.succeeded", "actor_id": user.id, "role": user.role})
    return {"id": user.id, "username": user.username, "role": user.role, "csrf": request.session["csrf"]}


@app.post("/api/v1/auth/logout", dependencies=[Depends(csrf)])
def logout(request: Request):
    actor_id = request.session.get("uid")
    request.session.clear()
    if actor_id is not None:
        logger.info("auth.logout.succeeded", extra={"event": "auth.logout.succeeded", "actor_id": actor_id})
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}


def _work_item_payload(item: WorkItem, project_name: str | None = None) -> dict:
    return {
        "id": item.id, "user_id": item.user_id, "project_id": item.project_id,
        "kind": item.kind, "title": item.title, "description": item.description,
        "status": item.status, "start_at": item.start_at, "due_at": item.due_at, "completed_at": item.completed_at,
        "created_at": item.created_at, "updated_at": item.updated_at,
        "project_name": project_name,
    }


def _work_item_projects(user: User, db: Session):
    """Return the project scope visible to a personal board query."""
    query = select(Project.id, Project.name)
    if user.role != "admin":
        query = query.join(Membership, Membership.project_id == Project.id).where(Membership.user_id == user.id)
    return {project_id: name for project_id, name in db.execute(query).all()}


@app.get("/api/v1/workboard")
def get_workboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    projects = _work_item_projects(user, db)
    query = select(WorkItem).where(WorkItem.user_id == user.id)
    rows = db.scalars(query.order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())).all()
    # Items linked to a project the user no longer belongs to are not exposed.
    rows = [item for item in rows if item.project_id is None or item.project_id in projects]
    items = [_work_item_payload(item, projects.get(item.project_id)) for item in rows]
    now = datetime.now(timezone.utc)
    overdue = 0
    for item in rows:
        if item.status not in ("done", "completed") and item.due_at:
            try:
                due = datetime.fromisoformat(item.due_at.replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                overdue += due < now
            except ValueError:
                pass
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in rows:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        by_status[item.status] = by_status.get(item.status, 0) + 1
    # Published drafts and imported case rows are the durable output signals
    # already present in the project workflow. Keep them scoped to projects
    # visible to this user while the board entries remain personally owned.
    project_ids = tuple(projects)
    published_drafts = db.scalar(select(func.count()).select_from(CaseDraft).where(
        CaseDraft.status == "published", CaseDraft.project_id.in_(project_ids))) if project_ids else 0
    imported_case_rows = db.scalars(select(ImportBatch.rows).where(
        ImportBatch.status == "done", ImportBatch.project_id.in_(project_ids))).all() if project_ids else []
    output_count = int(published_drafts or 0) + sum(len(rows_value or []) for rows_value in imported_case_rows)
    return {"summary": {"total": len(rows), "active": sum(item.status not in ("done", "completed") for item in rows),
                         "completed": sum(item.status in ("done", "completed") for item in rows),
                         "overdue": overdue, "followed_requirements": by_kind.get("requirement", 0),
                         "tested_requirements": by_kind.get("tested_requirement", 0),
                         "test_plans": by_kind.get("test_plan", 0), "test_case_outputs": output_count,
                         "project_count": len(projects), "by_kind": by_kind, "by_status": by_status}, "items": items}


@app.post("/api/v1/workboard", dependencies=[Depends(csrf)])
def create_work_item(payload: WorkItemCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    title = payload.title.strip()
    if not title:
        raise HTTPException(422, "任务标题不能为空")
    project_name = None
    if payload.project_id is not None:
        project = project_access(payload.project_id, user, db)
        project_name = project.name
    kind = payload.kind.strip()
    status = payload.status.strip()
    if not kind or not status:
        raise HTTPException(422, "事项类型和状态不能为空")
    if kind not in {"requirement", "tested_requirement", "task", "test_plan", "other"}:
        raise HTTPException(422, "不支持的工作项类型")
    if status not in {"todo", "in_progress", "done"}:
        raise HTTPException(422, "不支持的工作项状态")
    completed = status == "done"
    timestamp = utcnow()
    item = WorkItem(user_id=user.id, project_id=payload.project_id, kind=kind,
                    title=title, description=payload.description, status=status,
                    start_at=payload.start_at, due_at=payload.due_at, completed_at=timestamp if completed else None,
                    created_at=timestamp, updated_at=timestamp)
    db.add(item); db.commit(); db.refresh(item)
    return _work_item_payload(item, project_name)


@app.patch("/api/v1/workboard/{item_id}", dependencies=[Depends(csrf)])
def update_work_item(item_id: int, payload: WorkItemUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(WorkItem).where(WorkItem.id == item_id, WorkItem.user_id == user.id))
    if not item:
        raise HTTPException(404, "工作项不存在")
    values = payload.model_dump(exclude_unset=True)
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise HTTPException(422, "任务标题不能为空")
    for key in ("kind", "status"):
        if key in values and isinstance(values[key], str):
            values[key] = values[key].strip()
            if not values[key]:
                raise HTTPException(422, "字段不能为空")
            if key == "kind" and values[key] not in {"requirement", "tested_requirement", "task", "test_plan", "other"}:
                raise HTTPException(422, "不支持的工作项类型")
            if key == "status" and values[key] not in {"todo", "in_progress", "done"}:
                raise HTTPException(422, "不支持的工作项状态")
    project_name = None
    if "project_id" in values:
        if values["project_id"] is not None:
            project_name = project_access(values["project_id"], user, db).name
    elif item.project_id is not None:
        project_name = project_access(item.project_id, user, db).name
    for key, value in values.items():
        setattr(item, key, value)
    if "status" in values:
        item.completed_at = utcnow() if values["status"] == "done" else None
    item.updated_at = utcnow()
    db.commit(); db.refresh(item)
    return _work_item_payload(item, project_name)


@app.delete("/api/v1/workboard/{item_id}", dependencies=[Depends(csrf)])
def delete_work_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(WorkItem).where(WorkItem.id == item_id, WorkItem.user_id == user.id))
    if not item:
        raise HTTPException(404, "工作项不存在")
    db.delete(item); db.commit()
    return {"ok": True, "id": item_id}


@app.get("/api/v1/projects")
def projects(include_archived: bool = Query(False), user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Project).order_by(Project.id.desc())
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    if user.role == "admin":
        rows = ((item, "admin") for item in db.scalars(query))
    else:
        rows = db.execute(select(Project, Membership.role).join(Membership).where(
            Membership.user_id == user.id,
            *([Project.archived_at.is_(None)] if not include_archived else []),
        ).order_by(Project.id.desc()))
    return [{"id": project.id, "name": project.name, "role": role,
             "created_at": project.created_at, "archived": project.archived_at is not None,
             "archived_at": project.archived_at} for project, role in rows]


@app.post("/api/v1/projects", dependencies=[Depends(csrf)])
def create_project(payload: Named, user: User = Depends(current_user), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "项目名称不能为空")
    project = Project(name=name)
    db.add(project)
    db.flush()
    db.add(Membership(project_id=project.id, user_id=user.id, role="manager"))
    db.add(MembershipEvent(project_id=project.id, user_id=user.id, actor_id=user.id, action="added", after_role="manager"))
    try:
        specs = discover_skills()
        try:
            config_file.persist_skills(specs)
        except OSError as exc:
            logger.error("config.file.write_failed", extra={
                "event": "config.file.write_failed", "component": "config-file",
                "error_type": type(exc).__name__, "actor_id": user.id,
            })
            raise HTTPException(503, "核心配置文件不可写") from exc
        for spec in specs:
            db.add(ProjectSkill(project_id=project.id, skill_id=spec.skill_id, name=spec.name,
                                label=spec.label, source_path=spec.path, description=spec.description,
                                enabled=spec.enabled_by_default))
    except SkillRunnerError as exc:
        logger.warning("project.skills.unavailable", extra={
            "event": "project.skills.unavailable", "actor_id": user.id,
            "error_type": type(exc).__name__, "component": "skill-runner",
        })
        raise HTTPException(503, "项目技能仓不可用") from exc
    db.commit()
    logger.info("project.created", extra={
        "event": "project.created", "project_id": project.id, "actor_id": user.id,
        "component": "project",
    })
    return {"id": project.id, "name": project.name, "role": "admin" if user.role == "admin" else "manager",
            "created_at": project.created_at, "archived": False, "archived_at": None}


@app.patch("/api/v1/projects/{project_id}", dependencies=[Depends(csrf)])
def update_project(project_id: int, payload: ProjectUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = _require_project_manager(project_id, user, db)
    if payload.name is None and payload.archived is None:
        raise HTTPException(422, "至少提供项目名称或归档状态")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "项目名称不能为空")
        project.name = name
    if payload.archived is not None:
        project.archived_at = utcnow() if payload.archived else None
    db.commit()
    return {"id": project.id, "name": project.name,
            "role": "admin" if user.role == "admin" else "manager",
            "created_at": project.created_at, "archived": project.archived_at is not None,
            "archived_at": project.archived_at}


@app.post("/api/v1/projects/{project_id}/archive", dependencies=[Depends(csrf)])
def archive_project(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return update_project(project_id, ProjectUpdate(archived=True), user, db)


@app.post("/api/v1/projects/{project_id}/restore", dependencies=[Depends(csrf)])
def restore_project(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return update_project(project_id, ProjectUpdate(archived=False), user, db)


@app.post("/api/v1/projects/{project_id}/duplicate", dependencies=[Depends(csrf)])
def duplicate_project(project_id: int, payload: ProjectDuplicate | None = None,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    source = _require_project_manager(project_id, user, db)
    name = (payload.name if payload else None) or f"{source.name} 副本"
    name = name.strip()
    if not name:
        raise HTTPException(422, "项目名称不能为空")
    project = Project(name=name)
    db.add(project)
    db.flush()
    db.add(Membership(project_id=project.id, user_id=user.id, role="manager"))
    db.add(MembershipEvent(project_id=project.id, user_id=user.id, actor_id=user.id,
                           action="added", after_role="manager"))
    # Copy project-local settings, but intentionally do not copy members or
    # test data; this prevents accidental cross-team data disclosure.
    for skill in db.scalars(select(ProjectSkill).where(ProjectSkill.project_id == project_id)).all():
        db.add(ProjectSkill(project_id=project.id, skill_id=skill.skill_id, name=skill.name,
                            label=skill.label, source_path=skill.source_path,
                            description=skill.description, enabled=skill.enabled))
    settings = db.get(ConversionSettings, project_id)
    if settings:
        db.add(ConversionSettings(project_id=project.id, default_prefix_path=settings.default_prefix_path,
                                   default_case_type=settings.default_case_type,
                                   default_case_status=settings.default_case_status,
                                   history_retention_days=settings.history_retention_days))
    db.commit()
    return {"id": project.id, "name": project.name,
            "role": "admin" if user.role == "admin" else "manager",
            "created_at": project.created_at, "archived": False, "archived_at": None}


@app.delete("/api/v1/projects/{project_id}", dependencies=[Depends(csrf)])
def delete_project(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = _require_project_manager(project_id, user, db)
    _delete_project_data(project_id, db)
    db.delete(project)
    db.commit()
    logger.info("project.deleted", extra={"event": "project.deleted", "project_id": project_id,
                                           "actor_id": user.id, "component": "project"})
    return {"ok": True, "id": project_id}


@app.get("/api/v1/projects/{project_id}/skills")
def list_project_skills(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    rows = ensure_project_skills(project_id, db)
    return [{"skill_id": row.skill_id, "name": row.name, "label": row.label,
             "source_path": row.source_path, "description": row.description,
             "enabled": row.enabled, "updated_at": row.updated_at} for row in rows]


@app.patch("/api/v1/projects/{project_id}/skills/{skill_id}", dependencies=[Depends(csrf)])
def update_project_skill(project_id: int, skill_id: str, payload: SkillUpdate,
                         user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    rows = ensure_project_skills(project_id, db)
    row = next((item for item in rows if item.skill_id == skill_id), None)
    if row is None:
        raise HTTPException(404, "技能不存在")
    row.enabled = payload.enabled
    row.updated_at = utcnow()
    db.commit()
    return {"skill_id": row.skill_id, "enabled": row.enabled, "updated_at": row.updated_at}


@app.get("/api/v1/projects/{project_id}/overview")
def project_overview(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_access(project_id, user, db)
    total = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id)) or 0
    pending = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id, Case.review_status == "pending")) or 0
    my_pending = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id,
                           Case.assignee_id == user.id, Case.review_status == "pending")) or 0
    pending_suggestions = db.scalar(select(func.count(func.distinct(CaseSuggestion.case_id)))
        .join(Case, CaseSuggestion.case_id == Case.id)
        .where(CaseSuggestion.project_id == project_id, CaseSuggestion.status == "pending",
               CaseSuggestion.case_version == Case.version)) or 0
    analysis_count = db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.project_id == project_id)) or 0
    draft_count = db.scalar(select(func.count()).select_from(CaseDraft).where(CaseDraft.project_id == project_id, CaseDraft.status == "draft")) or 0
    conversion_count = db.scalar(select(func.count()).select_from(ConversionRecord).where(ConversionRecord.project_id == project_id)) or 0
    document_count = db.scalar(select(func.count()).select_from(SourceDocument).where(SourceDocument.project_id == project_id)) or 0
    import_count = db.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.status == "done")) or 0
    smoke_suite_count = db.scalar(select(func.count()).select_from(SmokeSuite).where(SmokeSuite.project_id == project_id)) or 0
    active_run_count = db.scalar(select(func.count()).select_from(ExecutionRun).where(ExecutionRun.project_id == project_id,
                                  ExecutionRun.status == "active")) or 0
    imports = db.scalars(select(ImportBatch).where(ImportBatch.project_id == project_id).order_by(ImportBatch.id.desc()).limit(5)).all()
    analyses = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project_id).order_by(AnalysisRun.created_at.desc()).limit(5)).all()
    analysis_documents = db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id)).all()
    analysis_source_names = {document.id: document.filename for document in analysis_documents}
    drafts = db.scalars(select(CaseDraft).where(CaseDraft.project_id == project_id).order_by(CaseDraft.created_at.desc()).limit(5)).all()
    conversions = db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id).order_by(ConversionRecord.created_at.desc()).limit(5)).all()
    documents = db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id).order_by(SourceDocument.created_at.desc()).limit(5)).all()
    runs = db.scalars(select(ExecutionRun).where(ExecutionRun.project_id == project_id).order_by(ExecutionRun.id.desc()).limit(5)).all()
    activity = (
        [{"id": f"import-{item.id}", "kind": "import", "title": item.filename, "status": "已导入" if item.status == "done" else "待确认", "created_at": item.created_at, "view": "imports"} for item in imports]
        + [{"id": f"analysis-{item.id}", "kind": "analysis",
            "title": "、".join(analysis_source_names.get(source_id, source_id) for source_id in item.source_ids) or "需求分析",
            "status": {"ready": "已完成", "failed": "失败", "queued": "排队中", "running": "处理中"}.get(item.status, item.status),
            "created_at": item.created_at, "view": "requirements"} for item in analyses]
        + [{"id": f"draft-{item.id}", "kind": "draft", "title": item.payload.get("title", "用例草稿"), "status": "已发布" if item.status == "published" else "草稿", "created_at": item.created_at, "view": "authoring"} for item in drafts]
        + [{"id": f"conversion-{item.id}", "kind": "conversion", "title": item.output_filename, "status": "已导出", "created_at": item.created_at, "view": "conversion"} for item in conversions]
        + [{"id": f"document-{item.id}", "kind": "document", "title": item.filename, "status": "已上传", "created_at": item.created_at, "view": "requirements"} for item in documents]
        + [{"id": f"run-{item.id}", "kind": "run", "title": f"{item.title} · {item.target_version}", "status": "执行中" if item.status == "active" else "已完成", "created_at": item.started_at, "view": "smoke"} for item in runs]
    )

    def activity_time(item: dict) -> datetime:
        try:
            value = datetime.fromisoformat(item["created_at"])
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    return {"project": {"id": project.id, "name": project.name}, "case_count": total,
            "pending_count": pending, "my_pending_count": my_pending, "pending_suggestion_count": pending_suggestions,
            "analysis_count": analysis_count, "draft_count": draft_count,
            "conversion_count": conversion_count, "document_count": document_count,
            "smoke_suite_count": smoke_suite_count, "active_run_count": active_run_count,
            "import_count": import_count, "recent_activity": sorted(activity, key=activity_time, reverse=True)[:6],
            "recent_imports": [
                {"id": item.id, "filename": item.filename, "count": len(item.rows), "status": item.status, "created_at": item.created_at}
                for item in imports]}


@app.post("/api/v1/users", dependencies=[Depends(csrf)])
def create_user(payload: UserCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin" or payload.role not in ("admin", "reviewer", "reader"):
        raise HTTPException(403, "无权创建该账号")
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(409, "用户名已存在")
    created = User(username=payload.username, password_hash=hasher.hash(payload.password), role=payload.role)
    db.add(created)
    db.commit()
    logger.info("user.created", extra={
        "event": "user.created", "actor_id": user.id, "role": payload.role,
        "component": "identity",
    })
    return {"id": created.id, "username": created.username, "role": created.role}


@app.patch("/api/v1/users/{user_id}/password", dependencies=[Depends(csrf)])
def update_user_password(user_id: int, payload: PasswordUpdate,
                         user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Allow a global administrator to rotate an account password in the UI."""
    if user.role != "admin":
        raise HTTPException(403, "仅全局管理员可修改账号密码")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "账号不存在")
    target.password_hash = hasher.hash(payload.password)
    db.commit()
    logger.info("user.password_updated", extra={
        "event": "user.password_updated", "actor_id": user.id, "target_user_id": target.id,
        "component": "identity",
    })
    return {"id": target.id, "username": target.username, "role": target.role}


@app.post("/api/v1/projects/{project_id}/members", dependencies=[Depends(csrf)])
def add_member(project_id: int, payload: MembershipCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    if (payload.user_id is None) == (payload.username is None):
        raise HTTPException(422, "请提供账号 ID 或用户名")
    target = db.get(User, payload.user_id) if payload.user_id is not None else db.scalar(select(User).where(User.username == payload.username.strip()))
    if not target:
        raise HTTPException(404, "账号不存在")
    if target.role == "admin" and user.role != "admin":
        raise HTTPException(403, "无法管理全局管理员")
    role = payload.role or ("manager" if target.role == "admin" else target.role)
    if role == "manager" and user.role != "admin":
        raise HTTPException(403, "仅全局管理员可授予项目管理员权限")
    existing = db.get(Membership, (project_id, target.id))
    if not existing:
        db.add(Membership(project_id=project_id, user_id=target.id, role=role))
        db.add(MembershipEvent(project_id=project_id, user_id=target.id, actor_id=user.id, action="added", after_role=role))
        db.commit()
    return {"id": target.id, "username": target.username, "role": "admin" if target.role == "admin" else (existing.role if existing else role)}


@app.post("/api/v1/projects/{project_id}/members/new", dependencies=[Depends(csrf)])
def create_project_member(project_id: int, payload: ProjectMemberCreate,
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    if user.role != "admin":
        raise HTTPException(403, "仅全局管理员可创建账号")
    username = payload.username.strip()
    if len(username) < 2:
        raise HTTPException(422, "用户名至少 2 个字符")
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "用户名已存在")
    member = User(username=username, password_hash=hasher.hash(payload.password),
                  role="reader" if payload.role == "reader" else "reviewer")
    db.add(member)
    db.flush()
    db.add(Membership(project_id=project_id, user_id=member.id, role=payload.role))
    db.add(MembershipEvent(project_id=project_id, user_id=member.id, actor_id=user.id,
                           action="added", after_role=payload.role))
    db.commit()
    return {"id": member.id, "username": member.username, "role": payload.role}


@app.get("/api/v1/projects/{project_id}/members")
def list_members(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    rows = db.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id).where(Membership.project_id == project_id).order_by(User.username))
    return [{"id": member.id, "username": member.username, "role": "admin" if member.role == "admin" else role} for member, role in rows]


@app.patch("/api/v1/projects/{project_id}/members/{member_id}", dependencies=[Depends(csrf)])
def change_member_role(project_id: int, member_id: int, payload: MembershipRoleUpdate,
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    member = db.get(Membership, (project_id, member_id))
    target = db.get(User, member_id)
    if not member or not target:
        raise HTTPException(404, "项目成员不存在")
    if target.role == "admin" or (user.role != "admin" and (member.role == "manager" or payload.role == "manager")):
        raise HTTPException(403, "无法调整该成员的权限")
    if member.role != payload.role:
        before = member.role
        member.role = payload.role
        if payload.role == "reader":
            release_assignments(project_id, member_id, user.id, db)
        db.add(MembershipEvent(project_id=project_id, user_id=member_id, actor_id=user.id,
                               action="role_changed", before_role=before, after_role=payload.role))
        db.commit()
    return {"id": target.id, "username": target.username, "role": member.role}


@app.delete("/api/v1/projects/{project_id}/members/{member_id}", dependencies=[Depends(csrf)])
def remove_member(project_id: int, member_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    member = db.get(Membership, (project_id, member_id))
    target = db.get(User, member_id)
    if not member or not target:
        raise HTTPException(404, "项目成员不存在")
    if target.role == "admin" or (user.role != "admin" and member.role == "manager"):
        raise HTTPException(403, "无法移除该成员")
    db.add(MembershipEvent(project_id=project_id, user_id=member_id, actor_id=user.id,
                           action="removed", before_role=member.role))
    release_assignments(project_id, member_id, user.id, db)
    db.delete(member)
    db.commit()
    return {"ok": True}


@app.get("/api/v1/projects/{project_id}/member-events")
def member_events(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    events = db.scalars(select(MembershipEvent).where(MembershipEvent.project_id == project_id).order_by(MembershipEvent.id.desc()).limit(30)).all()
    user_ids = {uid for event in events for uid in (event.user_id, event.actor_id)}
    users = {item.id: item.username for item in db.scalars(select(User).where(User.id.in_(user_ids)))}
    return [{"id": event.id, "member": users.get(event.user_id, "未知账号"), "actor": users.get(event.actor_id, "未知账号"),
             "action": event.action, "before_role": event.before_role, "after_role": event.after_role,
             "created_at": event.created_at} for event in events]


@app.post("/api/v1/projects/{project_id}/imports/preview", dependencies=[Depends(csrf)])
async def preview(project_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    writable(user, project_id, db)
    if not file.filename or file.filename.lower().rsplit(".", 1)[-1] not in ("xls", "xlsx", "xmind"):
        raise HTTPException(400, "文件类型不支持")
    data = await file.read(10 * 1024 * 1024 + 1)
    preview_started = monotonic()
    try:
        headers, rows, warnings = parse(file.filename, data)
    except ImportErrorDetail as exc:
        logger.warning("import.preview.failed", extra={
            "event": "import.preview.failed", "project_id": project_id,
            "actor_id": user.id, "error_type": type(exc).__name__,
            "source": Path(file.filename).suffix.lower().lstrip("."),
            "duration_ms": round((monotonic() - preview_started) * 1000, 2),
        })
        raise HTTPException(400, str(exc)) from exc
    if not rows:
        raise HTTPException(400, "没有可导入的用例")
    batch = ImportBatch(project_id=project_id, filename=file.filename[:255], headers=headers, rows=rows, warnings=warnings)
    db.add(batch)
    db.commit()
    logger.info("import.preview.completed", extra={
        "event": "import.preview.completed", "project_id": project_id,
        "actor_id": user.id, "source": Path(file.filename).suffix.lower().lstrip("."),
        "file_size": len(data), "row_count": len(rows), "warning_count": len(warnings),
        "duration_ms": round((monotonic() - preview_started) * 1000, 2),
    })
    return {"id": batch.id, "filename": batch.filename, "headers": headers, "count": len(rows), "sample": rows[:5], "warnings": warnings}


@app.post("/api/v1/projects/{project_id}/imports/{batch_id}/confirm", dependencies=[Depends(csrf)])
def confirm(project_id: int, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    writable(user, project_id, db)
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(404, "导入记录不存在")
    if batch.status == "done":
        return {"count": len(batch.rows), "status": "done"}
    headers = batch.headers
    known = set(db.scalars(select(Case.original_id).where(Case.project_id == project_id)))
    incoming = {row[headers.index("测试用例ID")] for row in batch.rows}
    if known & incoming:
        raise HTTPException(409, "项目中已有相同用例 ID，未导入任何行")
    def field(row, name):
        return row[headers.index(name)] or "" if name in headers else ""
    for row in batch.rows:
        source = dict(zip(headers, row))
        created_at, updated_at = source_case_timestamps(source, batch.created_at)
        db.add(Case(project_id=project_id, import_id=batch.id, original_id=field(row, "测试用例ID"),
                    title=field(row, "用例名称"), directory=field(row, "用例目录"),
                    level=field(row, "用例等级"), smoke=field(row, "是否冒烟用例"), original=row,
                    created_at=created_at, updated_at=updated_at))
    batch.status = "done"
    db.commit()
    logger.info("import.confirmed", extra={
        "event": "import.confirmed", "project_id": project_id,
        "actor_id": user.id, "row_count": len(batch.rows),
    })
    return {"count": len(batch.rows), "status": "done"}


@app.get("/api/v1/projects/{project_id}/imports")
def imports(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    return [{"id": item.id, "filename": item.filename, "count": len(item.rows), "status": item.status, "created_at": item.created_at}
            for item in db.scalars(select(ImportBatch).where(ImportBatch.project_id == project_id).order_by(ImportBatch.id.desc()))]


@app.delete("/api/v1/projects/{project_id}/imports/{batch_id}", dependencies=[Depends(csrf)])
def delete_import(project_id: int, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete an import record together with the cases it created."""
    writable(user, project_id, db)
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(404, "导入记录不存在")
    case_ids = db.scalars(select(Case.id).where(Case.project_id == project_id, Case.import_id == batch_id)).all()
    # Governance snapshots point at the batch and must not survive its removal.
    group_ids = db.scalars(select(GovernanceDuplicateGroup.id).where(
        GovernanceDuplicateGroup.project_id == project_id, GovernanceDuplicateGroup.batch_id == batch_id)).all()
    if group_ids:
        db.execute(delete(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id.in_(group_ids)))
        db.execute(delete(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.id.in_(group_ids)))
    db.execute(delete(GovernanceMetricSnapshot).where(
        GovernanceMetricSnapshot.project_id == project_id, GovernanceMetricSnapshot.batch_id == batch_id))
    delete_case_rows(case_ids, db)
    # A later governance snapshot may refer to this batch as its parent.
    db.execute(update(ImportBatch).where(ImportBatch.parent_batch_id == batch_id).values(parent_batch_id=None))
    db.delete(batch)
    db.commit()
    return {"id": batch_id, "deleted": 1, "cases_deleted": len(case_ids)}


def case_dict(item: Case, assignee: str | None = None, pending_suggestion_count: int = 0):
    return {"id": item.id, "original_id": item.original_id, "title": item.title, "directory": item.directory,
            "level": item.level, "smoke": item.smoke, "original": item.original, "final_level": item.final_level,
            "final_smoke": item.final_smoke, "review_status": item.review_status, "version": item.version,
            "reviewer": item.reviewer, "reviewed_at": item.reviewed_at, "import_id": item.import_id,
            "assignee_id": item.assignee_id, "assignee": assignee,
            "final_categories": item.final_categories or {},
            "final_taxonomy_version_id": item.final_taxonomy_version_id,
            "governance_status": item.governance_status,
            "merged_into": item.merged_into,
            "pending_suggestion_count": pending_suggestion_count,
            "created_at": item.created_at, "updated_at": item.updated_at}


def case_assignee(item: Case, db: Session) -> str | None:
    if item.assignee_id is None:
        return None
    member = db.get(User, item.assignee_id)
    return member.username if member else None


def apply_review(project_id: int, item_id: int, version: int, level: str, smoke: str,
                 reason: str, user: User, role: str, db: Session) -> tuple[str, dict | None]:
    if level not in ("1", "2", "3", "4") or smoke not in ("", "是", "否"):
        return "invalid", None
    item = db.get(Case, item_id)
    if not item or item.project_id != project_id:
        return "not_found", None
    if role == "reviewer" and item.assignee_id not in (None, user.id):
        return "forbidden", None
    if item.version != version:
        return "conflict", None
    before = {"level": item.final_level, "smoke": item.final_smoke, "status": item.review_status}
    after = {"level": level, "smoke": smoke, "status": "reviewed"}
    if before == after:
        return "unchanged", case_dict(item, case_assignee(item, db))
    changed_at = utcnow()
    result = db.execute(update(Case).where(Case.id == item_id, Case.project_id == project_id, Case.version == version)
                        .values(final_level=level, final_smoke=smoke, review_status="reviewed",
                                version=Case.version + 1, reviewer=user.username, reviewed_at=changed_at,
                                updated_at=changed_at)
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        return "conflict", None
    db.add(ReviewEvent(case_id=item_id, user_id=user.id, action="review", before=before, after=after, reason=reason.strip()))
    db.commit()
    db.refresh(item)
    return "saved", case_dict(item, case_assignee(item, db))


@app.get("/api/v1/projects/{project_id}/cases")
def cases(project_id: int, q: str = "", page: int = 1, size: int = 30, assignee: str = "all", status: str = "all", suggestion: str = "all",
          user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    if page < 1 or size < 1 or size > 100 or assignee not in ("all", "me", "unassigned") or status not in ("all", "pending", "reviewed") or suggestion not in ("all", "pending"):
        raise HTTPException(400, "筛选或分页参数无效")
    query = select(Case).where(Case.project_id == project_id)
    if q:
        query = query.where(Case.title.contains(q) | Case.original_id.contains(q) | Case.directory.contains(q))
    if assignee == "me":
        query = query.where(Case.assignee_id == user.id)
    elif assignee == "unassigned":
        query = query.where(Case.assignee_id.is_(None))
    if status != "all":
        query = query.where(Case.review_status == status)
    if suggestion == "pending":
        query = query.where(exists(select(CaseSuggestion.id).where(CaseSuggestion.case_id == Case.id,
                  CaseSuggestion.status == "pending", CaseSuggestion.case_version == Case.version)))
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Case.id).offset((page - 1) * size).limit(size)).all()
    assignee_ids = {item.assignee_id for item in items if item.assignee_id is not None}
    names = {member.id: member.username for member in db.scalars(select(User).where(User.id.in_(assignee_ids)))}
    ids = [item.id for item in items]
    active = dict(db.execute(select(CaseSuggestion.case_id, func.count(CaseSuggestion.id)).join(Case, CaseSuggestion.case_id == Case.id)
        .where(CaseSuggestion.case_id.in_(ids), CaseSuggestion.status == "pending",
               CaseSuggestion.case_version == Case.version).group_by(CaseSuggestion.case_id)).all()) if ids else {}
    return {"total": count, "items": [case_dict(item, names.get(item.assignee_id), active.get(item.id, 0)) for item in items]}


def delete_case_rows(case_ids: list[int], db: Session) -> None:
    """Remove a case and all review/smoke snapshots that reference it."""
    if not case_ids:
        return
    run_case_ids = db.scalars(select(RunCase.id).where(RunCase.case_id.in_(case_ids))).all()
    if run_case_ids:
        db.execute(delete(ExecutionEvent).where(ExecutionEvent.run_case_id.in_(run_case_ids)))
    db.execute(delete(RunCase).where(RunCase.case_id.in_(case_ids)))
    db.execute(delete(SmokeItem).where(SmokeItem.case_id.in_(case_ids)))
    db.execute(delete(CaseSuggestion).where(CaseSuggestion.case_id.in_(case_ids)))
    db.execute(delete(CaseComment).where(CaseComment.case_id.in_(case_ids)))
    db.execute(delete(ReviewEvent).where(ReviewEvent.case_id.in_(case_ids)))
    db.execute(delete(CaseDraft).where(CaseDraft.case_id.in_(case_ids)))
    db.execute(delete(GovernanceDuplicateMember).where(GovernanceDuplicateMember.case_id.in_(case_ids)))
    db.execute(delete(Case).where(Case.id.in_(case_ids)))


@app.delete("/api/v1/projects/{project_id}/cases/batch-delete", dependencies=[Depends(csrf)])
def batch_delete_cases(project_id: int, payload: BatchDelete, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    seen: set[int] = set()
    results: list[dict] = []
    deletable: list[int] = []
    for case_id in payload.case_ids:
        if case_id in seen:
            results.append({"id": case_id, "status": "invalid", "detail": "重复的用例 ID"})
            continue
        seen.add(case_id)
        item = db.get(Case, case_id)
        if not item or item.project_id != project_id:
            results.append({"id": case_id, "status": "not_found"})
            continue
        deletable.append(case_id)
        results.append({"id": case_id, "status": "deleted"})
    delete_case_rows(deletable, db)
    db.commit()
    return {"results": results, "deleted": len(deletable)}


@app.delete("/api/v1/projects/{project_id}/cases/all", dependencies=[Depends(csrf)])
@app.delete("/api/v1/projects/{project_id}/cases/delete-all", dependencies=[Depends(csrf)])
def delete_all_cases(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete every case in the current project and its dependent review data."""
    writable(user, project_id, db)
    case_ids = db.scalars(select(Case.id).where(Case.project_id == project_id)).all()
    db.execute(delete(GovernanceMetricSnapshot).where(GovernanceMetricSnapshot.project_id == project_id))
    db.execute(delete(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.project_id == project_id))
    delete_case_rows(case_ids, db)
    db.commit()
    return {"deleted": len(case_ids)}


@app.delete("/api/v1/projects/{project_id}/cases/{case_id}", dependencies=[Depends(csrf)])
def delete_case(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    delete_case_rows([case_id], db)
    db.commit()
    return {"id": case_id, "deleted": 1}


@app.put("/api/v1/projects/{project_id}/cases/batch-review", dependencies=[Depends(csrf)])
def batch_review(project_id: int, payload: BatchReview, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    if not payload.reason.strip():
        raise HTTPException(422, "请填写审核理由")
    role = project_role(project_id, user, db)
    seen = set()
    results = []
    for entry in payload.items:
        if entry.id in seen:
            results.append({"id": entry.id, "status": "invalid", "detail": "重复的用例 ID"})
            continue
        seen.add(entry.id)
        outcome, value = apply_review(project_id, entry.id, entry.version, entry.final_level,
                                      entry.final_smoke, payload.reason, user, role, db)
        results.append({"id": entry.id, "status": outcome, "case": value})
    return {"results": results, "saved": sum(item["status"] == "saved" for item in results),
            "conflicts": sum(item["status"] == "conflict" for item in results)}


@app.get("/api/v1/projects/{project_id}/cases/{case_id}")
def case_detail(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    batch = db.get(ImportBatch, item.import_id)
    return {**case_dict(item, case_assignee(item, db)), "headers": batch.headers}


@app.put("/api/v1/projects/{project_id}/cases/{case_id}/assignment", dependencies=[Depends(csrf)])
def assign_case(project_id: int, case_id: int, payload: Assignment,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    role = project_role(project_id, user, db)
    if role == "reviewer" and (item.assignee_id not in (None, user.id) or payload.assignee_id not in (None, user.id)):
        raise HTTPException(403, "只能认领或释放自己的用例")
    target = None
    if payload.assignee_id is not None:
        target = db.get(User, payload.assignee_id)
        membership = db.get(Membership, (project_id, payload.assignee_id))
        if not target or not membership or membership.role not in ("manager", "reviewer"):
            raise HTTPException(422, "负责人必须是当前项目的可写成员")
    if item.version != payload.version:
        raise HTTPException(409, "用例已被他人修改，请刷新")
    if item.assignee_id == payload.assignee_id:
        return case_dict(item, case_assignee(item, db))
    before_id = item.assignee_id
    changed = db.execute(update(Case).where(Case.id == case_id, Case.project_id == project_id, Case.version == payload.version)
                         .values(assignee_id=payload.assignee_id, version=Case.version + 1, updated_at=utcnow())
                         .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "用例已被他人修改，请刷新")
    db.add(ReviewEvent(case_id=case_id, user_id=user.id, action="assignment",
                       before={"assignee_id": before_id}, after={"assignee_id": payload.assignee_id},
                       reason=f"负责人调整为 {target.username if target else '未分派'}"))
    db.commit()
    db.refresh(item)
    return case_dict(item, target.username if target else None)


@app.get("/api/v1/projects/{project_id}/cases/{case_id}/activity")
def case_activity(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    events = db.scalars(select(ReviewEvent).where(ReviewEvent.case_id == case_id).order_by(ReviewEvent.id.desc()).limit(50)).all()
    comments = db.scalars(select(CaseComment).where(CaseComment.case_id == case_id).order_by(CaseComment.id.desc()).limit(100)).all()
    ids = {author_id for entry in [*events, *comments] for author_id in (entry.user_id,)}
    names = {member.id: member.username for member in db.scalars(select(User).where(User.id.in_(ids)))}
    return {"events": [{"id": entry.id, "action": entry.action, "actor": names.get(entry.user_id, "未知账号"),
                        "before": entry.before, "after": entry.after, "reason": entry.reason, "created_at": entry.created_at}
                       for entry in events],
            "comments": [{"id": entry.id, "actor": names.get(entry.user_id, "未知账号"), "text": entry.text,
                          "created_at": entry.created_at} for entry in comments]}


@app.post("/api/v1/projects/{project_id}/cases/{case_id}/comments", dependencies=[Depends(csrf)])
def add_case_comment(project_id: int, case_id: int, payload: CommentCreate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "讨论内容不能为空")
    entry = CaseComment(case_id=case_id, user_id=user.id, text=text)
    db.add(entry)
    db.commit()
    return {"id": entry.id, "actor": user.username, "text": entry.text, "created_at": entry.created_at}


@app.put("/api/v1/projects/{project_id}/cases/{case_id}/review", dependencies=[Depends(csrf)])
def review(project_id: int, case_id: int, payload: Review, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    if not payload.reason.strip():
        raise HTTPException(422, "请填写审核理由")
    status, value = apply_review(project_id, case_id, payload.version, payload.final_level,
                                 payload.final_smoke, payload.reason, user, project_role(project_id, user, db), db)
    if status == "not_found":
        raise HTTPException(404, "用例不存在")
    if status == "forbidden":
        raise HTTPException(403, "用例已分派给其他成员")
    if status == "conflict":
        raise HTTPException(409, "用例已被他人修改，请刷新")
    if status == "invalid":
        raise HTTPException(400, "等级或冒烟值无效")
    return value


@app.get("/api/v1/projects/{project_id}/export")
def export(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    items = db.scalars(select(Case).where(Case.project_id == project_id).order_by(Case.id)).all()
    book = Workbook()
    sheet = book.active
    sheet.title = "用例"
    headers = []
    for item in items:
        for name in db.get(ImportBatch, item.import_id).headers:
            if name not in headers:
                headers.append(name)
    appended = ["最终等级", "最终冒烟", "审核状态", "审核人", "审核时间", "负责人",
                "建议来源", "建议等级", "建议冒烟", "建议依据", "最终业务模块", "最终业务场景", "最终测试维度", "最终端类型", "创建时间", "最后修改时间"]
    sheet.append(headers + appended)
    def safe(value):
        if value is None:
            return None
        text = str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text
    suggestion_rows = db.scalars(select(CaseSuggestion).where(CaseSuggestion.project_id == project_id)
                                 .order_by(CaseSuggestion.id.desc())).all()
    latest = {}
    for proposal in suggestion_rows:
        if proposal.case_id not in latest:
            latest[proposal.case_id] = proposal
    versions = {item.final_taxonomy_version_id for item in items if item.final_taxonomy_version_id is not None}
    definitions = {version.id: version.definitions for version in db.scalars(select(TaxonomyVersion).where(TaxonomyVersion.id.in_(versions)))}
    for item in items:
        source = dict(zip(db.get(ImportBatch, item.import_id).headers, item.original))
        proposal = latest.get(item.id)
        options = definitions.get(item.final_taxonomy_version_id) or {}
        def category_label(key):
            identifier = (item.final_categories or {}).get(key)
            return next((entry["name"] for entry in options.get(key, []) if entry["id"] == identifier), None)
        sheet.append([safe(source.get(name)) for name in headers] + [
            item.final_level, item.final_smoke if item.review_status == "reviewed" else None,
            "已审核" if item.review_status == "reviewed" else "待审核", item.reviewer, item.reviewed_at, case_assignee(item, db),
            proposal.source if proposal else None, proposal.level if proposal else None, proposal.smoke if proposal else None,
            safe(proposal.rationale) if proposal else None,
            *(safe(category_label(key)) for key in ("business_module", "business_scenario", "test_dimension", "endpoint_type")),
            item.created_at, item.updated_at])
    stats = book.create_sheet("统计")
    stats.append(["指标", "数量"])
    stats.append(["用例总数", len(items)])
    stats.append(["待审核", sum(item.review_status == "pending" for item in items)])
    stats.append(["原始冒烟空值", sum(not item.smoke for item in items)])
    for level in ("1", "2", "3", "4"):
        stats.append([f"原始等级 {level}", sum(item.level == level for item in items)])
    out = io.BytesIO()
    book.save(out)
    out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="project-{project_id}-cases.xlsx"'})


from .conversion_routes import build_router  # noqa: E402
from .ai_routes import build_router as build_ai_router  # noqa: E402
from .suggestion_routes import build_router as build_suggestion_router  # noqa: E402
from .smoke_routes import build_router as build_smoke_router  # noqa: E402
from .governance_routes import build_router as build_governance_router  # noqa: E402

app.include_router(build_router(get_db, current_user, csrf, project_access, writable))
app.include_router(build_ai_router(get_db, current_user, csrf, project_access, writable))
app.include_router(build_suggestion_router(get_db, current_user, csrf, project_access, writable,
                                           project_manager, project_role, case_dict, case_assignee))
app.include_router(build_smoke_router(get_db, current_user, csrf, project_access, writable))
app.include_router(build_governance_router(get_db, current_user, csrf, project_access, writable, project_role))
