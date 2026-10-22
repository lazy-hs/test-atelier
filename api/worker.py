"""Durable AI task worker for production deployments.

The API writes queued rows and returns.  This process claims one row at a
time with a conditional update, then delegates to the same guarded task
implementations used by local development. Jobs carry an expiring database
lease, so a second worker can safely recover work after a crash.
"""

from __future__ import annotations

import logging
import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import select, text, update

from . import ai_engine, ai_jobs, suggestion_jobs
from .db import AiJob, AiModel, AiSettings, AiTool, SessionLocal, SuggestionJob, database_kind
from .observability import (configure_logging, deployment_release, env_float, monotonic,
                            reset_request_id, set_request_id)
from .storage import ensure_private_directory, write_private_atomic


logger = configure_logging()
POLL_SECONDS = env_float("AI_STUDIO_WORKER_POLL_SECONDS", 1, 0.1)
HEARTBEAT_SECONDS = env_float("AI_STUDIO_WORKER_HEARTBEAT_SECONDS", 5, 1.0)
LEASE_SECONDS = env_float("AI_STUDIO_WORKER_LEASE_SECONDS", 900, 30.0)
# A healthy worker should recover an expired lease without requiring a process
# restart. Keep the scan bounded so a large queue does not dominate polling.
RECOVERY_INTERVAL_SECONDS = max(5.0, min(LEASE_SECONDS / 3, 60.0))
HEARTBEAT_PATH = Path(os.getenv("AI_STUDIO_WORKER_HEARTBEAT", "").strip() or
                      (Path(os.getenv("AI_STUDIO_DATA_DIR", "data")) / ".worker-heartbeat"))
WORKER_ID = os.getenv("AI_STUDIO_WORKER_ID", f"worker-{uuid.uuid4().hex}")[:128]
stop_event = threading.Event()
# A fresh file heartbeat must not mask a database outage that prevents the
# worker from claiming jobs. The claim loop clears this after recovery.
database_degraded = threading.Event()
_worker_error_log_lock = threading.Lock()
_worker_error_log_last: dict[str, tuple[float, int]] = {}
_WORKER_ERROR_LOG_INTERVAL_SECONDS = env_float("AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS", 30, 1.0)


def _log_worker_error(event: str, *, level: int = logging.ERROR, **fields) -> None:
    """Rate-limit repeated infrastructure failures without hiding recovery.

    Database outages and heartbeat permission errors can be observed every
    poll interval. Emit the first event immediately, then one summary after
    the short window instead of flooding the collector with identical rows.
    The key intentionally excludes job ids so a queue-wide outage is bounded
    even when many jobs are being renewed concurrently.
    """
    key = ":".join(str(fields.get(name, "")) for name in ("component", "kind", "error_type"))
    now = monotonic()
    with _worker_error_log_lock:
        previous = _worker_error_log_last.get(event + ":" + key)
        if previous is not None:
            last, suppressed = previous
            if now - last < _WORKER_ERROR_LOG_INTERVAL_SECONDS:
                _worker_error_log_last[event + ":" + key] = (last, suppressed + 1)
                return
            fields.setdefault("count", suppressed)
        _worker_error_log_last[event + ":" + key] = (now, 0)
    logger.log(level, event, extra={"event": event, **fields})


class _JobCancelEvent:
    """Expose process stop and lease loss as one CLI cancellation signal."""

    def __init__(self, lease_lost: threading.Event):
        self.lease_lost = lease_lost

    def is_set(self) -> bool:
        return stop_event.is_set() or self.lease_lost.is_set()


def _load_ai_config(*, log_availability: bool = True) -> None:
    """Load encrypted provider settings in the worker process."""
    with SessionLocal() as db:
        settings = db.get(AiSettings, 1)
        if settings is None:
            return
        model_rows = {row.provider_id: row for row in db.scalars(select(AiModel)).all()}
        tool_rows = {row.tool_id: row for row in db.scalars(select(AiTool)).all()}
        local_row, remote_row = model_rows.get("local"), model_rows.get("remote")
        codex_row, claude_row = tool_rows.get("codex_cli"), tool_rows.get("claude_code")
        local_url = local_row.endpoint if local_row else settings.local_url
        local_model = local_row.model if local_row else settings.local_model
        local_enabled = local_row.enabled if local_row else ai_engine.catalog_provider_enabled(settings.providers_json, "local")
        remote_enabled = remote_row.enabled if remote_row else settings.remote_enabled
        remote_url = remote_row.endpoint if remote_row else settings.remote_url
        remote_model = remote_row.model if remote_row else settings.remote_model
        remote_secret = settings.remote_api_key or (remote_row.api_key_encrypted if remote_row else "")
        codex_enabled = codex_row.enabled if codex_row else settings.codex_enabled
        codex_command = codex_row.command if codex_row else settings.codex_command
        codex_model = codex_row.model if codex_row else settings.codex_model
        claude_enabled = claude_row.enabled if claude_row else settings.claude_enabled
        claude_command = claude_row.command if claude_row else settings.claude_command
        claude_model = claude_row.model if claude_row else settings.claude_model
        try:
            remote_api_key = (
                ai_engine.decrypt_secret_checked(remote_secret)
                if remote_enabled else ai_engine.decrypt_secret(remote_secret)
            )
        except RuntimeError:
            logger.error("ai.settings.secret_unavailable", extra={
                "event": "ai.settings.secret_unavailable", "component": "ai-settings",
                "error_type": "SecretDecryptionError",
                "worker_id": WORKER_ID,
            })
            raise
        ai_engine.apply_config({
            "local_url": local_url,
            "local_model": local_model,
            "local_enabled": local_enabled,
            "remote_enabled": remote_enabled,
            "remote_url": remote_url,
            "remote_model": remote_model,
            "remote_api_key": remote_api_key,
            "codex_enabled": codex_enabled,
            "codex_command": codex_command,
            "codex_model": codex_model,
            "claude_enabled": claude_enabled,
            "claude_command": claude_command,
            "claude_model": claude_model,
        })
        if log_availability:
            ai_engine.log_cli_availability("external-worker")


def _check_migrations() -> None:
    expected = os.getenv("AI_STUDIO_MIGRATION_HEAD", "20261032_case_timestamps")
    with SessionLocal() as db:
        actual = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if actual != expected:
        logger.error("worker.migrations.version_mismatch", extra={
            "event": "worker.migrations.version_mismatch", "component": "external-worker",
            "error_type": "MigrationVersionMismatch", "revision": actual or "unversioned",
            "expected_revision": expected, "worker_id": WORKER_ID,
        })
        raise RuntimeError(f"数据库迁移版本不匹配：当前 {actual or '空'}，需要 {expected}")
    logger.info("worker.migrations.checked", extra={
        "event": "worker.migrations.checked", "component": "external-worker",
        "revision": actual, "worker_id": WORKER_ID, "database_kind": database_kind,
    })


def _production_preflight() -> None:
    """Validate container configuration before the worker starts polling."""
    if os.getenv("AI_STUDIO_PREFLIGHT_COMPLETED", "0") == "1":
        return
    environment = os.getenv("AI_STUDIO_ENV", "development").strip().lower()
    required = os.getenv("AI_STUDIO_REQUIRE_PREFLIGHT", "0").lower() in {"1", "true", "yes", "on"}
    if environment != "production" and not required:
        return
    try:
        from scripts.preflight_production import run_preflight
        checks = run_preflight(dict(os.environ), compose=False, require_admin_password=False,
                                require_http_security=False)
    except Exception as exc:
        logger.error("worker.preflight.failed", extra={
            "event": "worker.preflight.failed", "component": "external-worker",
            "worker_id": WORKER_ID, "error_type": type(exc).__name__, "failure_count": 1,
        })
        raise RuntimeError("生产前置检查无法执行") from exc
    failures = [check.name for check in checks if check.status == "fail"]
    if failures:
        logger.error("worker.preflight.failed", extra={
            "event": "worker.preflight.failed", "component": "external-worker",
            "worker_id": WORKER_ID, "failure_count": len(failures), "failed_checks": failures[:32],
        })
        raise RuntimeError("生产前置检查失败")
    logger.info("worker.preflight.completed", extra={
        "event": "worker.preflight.completed", "component": "external-worker",
        "worker_id": WORKER_ID, "warning_count": sum(check.status == "warn" for check in checks),
    })


def _recover_running() -> int:
    """Requeue only jobs whose owner lease has expired."""
    now = time.time()
    with SessionLocal() as db:
        ai_count = db.execute(
            update(AiJob).where(AiJob.status == "running", AiJob.lease_until <= now).values(
                status="queued", lease_owner="", lease_until=0.0, lease_heartbeat=0.0)
        ).rowcount or 0
        suggestion_count = db.execute(
            update(SuggestionJob).where(SuggestionJob.status == "running", SuggestionJob.lease_until <= now).values(
                status="queued", lease_owner="", lease_until=0.0, lease_heartbeat=0.0)
        ).rowcount or 0
        db.commit()
    return ai_count + suggestion_count


def _claim(table, kind: str) -> tuple[str, str] | None:
    """Claim the oldest queued row, tolerating a concurrent worker race."""
    with SessionLocal() as db:
        item = db.scalar(select(table).where(table.status == "queued").order_by(table.created_at).limit(1))
        if item is None:
            return None
        now = time.time()
        changed = db.execute(update(table).where(
            table.id == item.id, table.status == "queued",
        ).values(status="running", lease_owner=WORKER_ID,
                 lease_until=now + LEASE_SECONDS, lease_heartbeat=now))
        if changed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return kind, item.id


def claim_next() -> tuple[str, str] | None:
    # Requirement analysis and case generation are usually more expensive;
    # give them first choice while still serving classification suggestions.
    return _claim(AiJob, "ai") or _claim(SuggestionJob, "suggestion")


def _heartbeat() -> None:
    ensure_private_directory(HEARTBEAT_PATH.parent)
    # Include the worker build in the heartbeat so the API can distinguish a
    # live but stale-version consumer during a rolling deployment. The file's
    # mtime remains the liveness signal; metadata is bounded and never carries
    # credentials or task payloads.
    payload = {
        "timestamp": time.time(),
        "release": deployment_release(),
        "worker_id": WORKER_ID,
    }
    write_private_atomic(
        HEARTBEAT_PATH,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        root=HEARTBEAT_PATH.parent,
    )


def _renew_lease(kind: str, job_id: str) -> bool:
    """Extend a job lease only while this worker still owns it."""
    table = AiJob if kind == "ai" else SuggestionJob
    now = time.time()
    with SessionLocal() as db:
        changed = db.execute(update(table).where(
            table.id == job_id, table.status == "running", table.lease_owner == WORKER_ID,
        ).values(lease_until=now + LEASE_SECONDS, lease_heartbeat=now))
        db.commit()
        return changed.rowcount == 1


def _requeue_claimed(kind: str, job_id: str) -> bool:
    """Return a claimed row to the durable queue when config reload fails."""
    table = AiJob if kind == "ai" else SuggestionJob
    with SessionLocal() as db:
        changed = db.execute(update(table).where(
            table.id == job_id, table.status == "running", table.lease_owner == WORKER_ID,
        ).values(status="queued", lease_owner="", lease_until=0.0, lease_heartbeat=0.0))
        db.commit()
        return changed.rowcount == 1


def _lease_loop(kind: str, job_id: str, stop: threading.Event,
                lease_lost: threading.Event | None = None,
                correlation_id: str = "") -> None:
    """Renew a lease in a context carrying the job's request correlation id."""
    token = set_request_id(correlation_id) if correlation_id else None
    try:
        interval = max(1.0, min(HEARTBEAT_SECONDS, LEASE_SECONDS / 3))
        while not stop.wait(interval):
            try:
                if not _renew_lease(kind, job_id):
                    if lease_lost is not None:
                        lease_lost.set()
                    _log_worker_error("worker.lease_lost", job_id=job_id,
                                      kind=kind, component="external-worker", worker_id=WORKER_ID,
                                      error_type="LeaseOwnershipLost")
                    return
            except Exception as exc:
                # A failed renewal means ownership can no longer be proven. Stop
                # provider work immediately so another worker cannot reclaim the
                # expired lease and write a duplicate result concurrently.
                if lease_lost is not None:
                    lease_lost.set()
                database_degraded.set()
                _log_worker_error("worker.lease_renew_failed", job_id=job_id,
                                  kind=kind, component="external-worker", worker_id=WORKER_ID,
                                  error_type=type(exc).__name__)
                return
    finally:
        if token is not None:
            reset_request_id(token)


def _heartbeat_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if not database_degraded.is_set():
                _heartbeat()
        except Exception as exc:
            _log_worker_error("worker.heartbeat_failed", component="external-worker",
                              worker_id=WORKER_ID, error_type=type(exc).__name__)
        stop.wait(HEARTBEAT_SECONDS)


def _run_claimed(kind: str, job_id: str) -> None:
    started = monotonic()
    with SessionLocal() as db:
        job = db.get(AiJob if kind == "ai" else SuggestionJob, job_id)
        correlation_id = (job.request_id if job else "") or ""
    token = set_request_id(correlation_id) if correlation_id else None
    lease_stop = threading.Event()
    lease_lost = threading.Event()
    previous_cancel_event = ai_engine.get_cancel_event()
    ai_engine.set_cancel_event(_JobCancelEvent(lease_lost))
    lease_thread = threading.Thread(target=_lease_loop,
                                     args=(kind, job_id, lease_stop, lease_lost, correlation_id),
                                     name=f"lease-{job_id[:8]}", daemon=True)
    lease_thread.start()
    try:
        # Settings are edited through the API while this process stays alive;
        # reload before each claimed job so new work uses the latest provider
        # configuration without requiring a worker restart.
        try:
            _load_ai_config(log_availability=False)
        except Exception as exc:
            _log_worker_error("worker.config.reload_failed", job_id=job_id,
                              kind=kind, component="external-worker", worker_id=WORKER_ID,
                              error_type=type(exc).__name__)
            # Do not execute with stale provider credentials or a disabled
            # provider when the database could not be read. Requeue while the
            # lease is still owned; a later poll can retry without waiting for
            # the full lease timeout.
            try:
                requeued = _requeue_claimed(kind, job_id)
                if requeued:
                    logger.warning("worker.job.requeued", extra={
                        "event": "worker.job.requeued", "job_id": job_id,
                        "kind": kind, "component": "external-worker", "worker_id": WORKER_ID,
                        "reason": "config-reload-failed",
                    })
            except Exception as requeue_error:
                _log_worker_error("worker.config.requeue_failed", job_id=job_id,
                                  kind=kind, component="external-worker", worker_id=WORKER_ID,
                                  error_type=type(requeue_error).__name__)
            return
        logger.info("worker.job.started", extra={
            "event": "worker.job.started", "job_id": job_id,
            "kind": kind, "component": "external-worker", "worker_id": WORKER_ID,
        })
        if kind == "ai":
            ai_jobs.process_job(job_id, lease_owner=WORKER_ID)
        else:
            suggestion_jobs.process_job(job_id, lease_owner=WORKER_ID)
    finally:
        ai_engine.set_cancel_event(previous_cancel_event)
        lease_stop.set()
        lease_thread.join(timeout=1)
        status = "unknown"
        try:
            with SessionLocal() as db:
                job = db.get(AiJob if kind == "ai" else SuggestionJob, job_id)
                status = job.status if job else "missing"
        except Exception as exc:
            _log_worker_error("worker.job.status_lookup_failed", level=logging.WARNING,
                              job_id=job_id, kind=kind, component="external-worker",
                              worker_id=WORKER_ID, error_type=type(exc).__name__)
        logger.info("worker.job.finished", extra={
            "event": "worker.job.finished", "job_id": job_id,
            "kind": kind, "status": status, "component": "external-worker", "worker_id": WORKER_ID,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        if token is not None:
            reset_request_id(token)


def _stop(signum, _frame) -> None:
    logger.info("worker.stop_requested", extra={
        "event": "worker.stop_requested", "component": "external-worker", "worker_id": WORKER_ID,
    })
    stop_event.set()


def _startup() -> int:
    """Validate the worker environment and recover leases before polling."""
    _production_preflight()
    _check_migrations()
    _load_ai_config()
    return _recover_running()


def run() -> None:
    if not ai_jobs.EXTERNAL_WORKER:
        error = RuntimeError("api.worker 需要设置 AI_STUDIO_EXTERNAL_WORKER=1")
        logger.error("worker.start_failed", extra={
            "event": "worker.start_failed", "component": "external-worker",
            "worker_id": WORKER_ID, "error_type": type(error).__name__,
        })
        raise error
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # Let a running Codex/Claude child terminate with the worker instead of
    # holding shutdown open until the full CLI timeout elapses.
    previous_cancel_event = ai_engine.get_cancel_event()
    ai_engine.set_cancel_event(stop_event)
    try:
        recovered = _startup()
    except Exception as exc:
        # Startup failures happen before the polling loop and otherwise only
        # produce an unstructured interpreter traceback in container logs.
        logger.exception("worker.start_failed", extra={
            "event": "worker.start_failed", "component": "external-worker",
            "worker_id": WORKER_ID, "error_type": type(exc).__name__,
        })
        ai_engine.set_cancel_event(previous_cancel_event)
        raise
    logger.info("worker.started", extra={
        "event": "worker.started", "component": "external-worker",
        "recovered_jobs": recovered, "worker_id": WORKER_ID, "lease_seconds": LEASE_SECONDS,
        "error_log_interval_seconds": _WORKER_ERROR_LOG_INTERVAL_SECONDS,
    })
    if recovered:
        logger.warning("worker.jobs.recovered", extra={
            "event": "worker.jobs.recovered", "component": "external-worker",
            "recovered_jobs": recovered, "worker_id": WORKER_ID,
        })
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(heartbeat_stop,),
                                         name="worker-heartbeat", daemon=True)
    heartbeat_thread.start()
    next_recovery = 0.0
    try:
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                if now >= next_recovery:
                    recovered = _recover_running()
                    next_recovery = now + RECOVERY_INTERVAL_SECONDS
                    if recovered:
                        logger.warning("worker.jobs.recovered", extra={
                            "event": "worker.jobs.recovered", "recovered_jobs": recovered,
                            "worker_id": WORKER_ID, "reason": "lease-expired",
                        })
                claimed = claim_next()
            except Exception as exc:
                database_degraded.set()
                # A transient database outage should be visible and should not
                # permanently kill the worker; retry after the normal poll
                # interval while the container healthcheck continues to run.
                _log_worker_error("worker.claim.failed", component="external-worker",
                                  worker_id=WORKER_ID, error_type=type(exc).__name__)
                # Retry recovery immediately after a transient database error
                # rather than waiting for the previous interval to elapse.
                next_recovery = 0.0
                stop_event.wait(POLL_SECONDS)
                continue
            database_degraded.clear()
            if claimed is None:
                stop_event.wait(POLL_SECONDS)
                continue
            try:
                _run_claimed(*claimed)
            except Exception as exc:
                # Keep the worker alive when an unexpected adapter or database
                # error escapes a job handler; the lease will make the row
                # eligible for recovery after the next worker restart.
                logger.exception("worker.job.crashed", extra={
                    "event": "worker.job.crashed", "job_id": claimed[1],
                    "kind": claimed[0], "component": "external-worker",
                    "worker_id": WORKER_ID, "error_type": type(exc).__name__,
                })
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        # Leave the last heartbeat in place.  Multiple worker replicas share
        # this path; deleting it here could erase a healthy replica's fresh
        # heartbeat during a rolling restart.  Readiness already treats the
        # file as stale after the configured threshold, and the next worker
        # heartbeat atomically replaces it on startup.
        logger.info("worker.stopped", extra={
            "event": "worker.stopped", "component": "external-worker", "worker_id": WORKER_ID,
        })
        ai_engine.set_cancel_event(previous_cancel_event)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        # The structured startup event above is the operator-facing failure;
        # avoid appending a second, unstructured traceback to container logs.
        raise SystemExit(1)
