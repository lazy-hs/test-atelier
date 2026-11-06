"""Independent durable projector for DeepSeek Harness task events.

The projector has no listener and does not import DeepSeek Harness internals.
It claims project-scoped ``HarnessTask`` rows, consumes the Host's durable SSE
log, and applies each event through :mod:`api.harness_projection`.  A short
database lease makes crashes recoverable and prevents two replicas from
consuming the same task concurrently.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update

from .runtime_config import install_runtime_config

install_runtime_config()

from . import harness_bridge
from .db import HarnessTask, SessionLocal, database_kind
from .harness_projection import (
    TERMINAL_TASK_STATUSES,
    project_event,
    project_invalid_result,
    projection_failure,
    remote_cursor,
    validate_remote_event,
)
from .observability import configure_logging, deployment_release, env_float


logger = configure_logging()
POLL_SECONDS = env_float("TEST_ATELIER_HARNESS_PROJECTOR_POLL_SECONDS", 1.0, 0.1)
LEASE_SECONDS = env_float("TEST_ATELIER_HARNESS_PROJECTOR_LEASE_SECONDS", 120.0, 10.0)
MAX_EVENTS_PER_TASK = max(1, min(int(os.getenv("TEST_ATELIER_HARNESS_PROJECTOR_MAX_EVENTS", "256") or 256), 2048))
WORKER_ID = os.getenv("TEST_ATELIER_HARNESS_PROJECTOR_ID", f"harness-projector-{uuid.uuid4().hex}")[:128]
HEARTBEAT_SECONDS = env_float("TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT_SECONDS", 5.0, 1.0)
HEARTBEAT_PATH = Path(os.getenv("TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT", "data/.harness-projector-heartbeat"))
stop_event = threading.Event()


def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    allowed = {
        "component", "worker_id", "task_id", "project_id", "status", "error_type",
        "attempts", "event_count", "duration_ms", "database_kind", "release",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    payload.setdefault("component", "harness-projector")
    payload.setdefault("worker_id", WORKER_ID)
    payload.setdefault("database_kind", database_kind)
    release = deployment_release()
    if release:
        payload["release"] = release
    logger.log(level, event, extra={"event": event, **payload})


def _recover_expired(now: float | None = None) -> int:
    """Clear only expired ownership; never alter a task's business status."""
    now = time.time() if now is None else now
    with SessionLocal() as db:
        changed = db.execute(update(HarnessTask).where(
            HarnessTask.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
            HarnessTask.projector_owner != "",
            HarnessTask.projector_lease_until <= now,
        ).values(projector_owner="", projector_lease_until=0.0, projector_lease_heartbeat=0.0))
        db.commit()
        return changed.rowcount or 0


def claim_next(owner: str = WORKER_ID, now: float | None = None) -> str | None:
    """Atomically claim an available task without starving newer work."""
    now = time.time() if now is None else now
    with SessionLocal() as db:
        candidate = db.scalar(select(HarnessTask).where(
            HarnessTask.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
            (HarnessTask.projector_lease_until <= now) | (HarnessTask.projector_owner == owner),
        # A task awaiting human confirmation can stay non-terminal for hours.
        # Rotate equal-priority tasks by completed projector ticks so it cannot
        # monopolize a single Projector and block unrelated new tasks.
        ).order_by(HarnessTask.projector_attempts, HarnessTask.created_at, HarnessTask.id).limit(1))
        if candidate is None:
            return None
        changed = db.execute(update(HarnessTask).where(
            HarnessTask.id == candidate.id,
            HarnessTask.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
            (HarnessTask.projector_lease_until <= now) | (HarnessTask.projector_owner == owner),
        ).values(
            projector_owner=owner,
            projector_lease_until=now + LEASE_SECONDS,
            projector_lease_heartbeat=now,
            projector_attempts=HarnessTask.projector_attempts + 1,
        ))
        if changed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return candidate.id


def renew(task_id: str, owner: str = WORKER_ID, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with SessionLocal() as db:
        changed = db.execute(update(HarnessTask).where(
            HarnessTask.id == task_id,
            HarnessTask.projector_owner == owner,
            HarnessTask.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
        ).values(projector_lease_until=now + LEASE_SECONDS, projector_lease_heartbeat=now))
        db.commit()
        return changed.rowcount == 1


def release(task_id: str, owner: str = WORKER_ID, *, error: str = "", terminal: bool = False) -> bool:
    """Release ownership after a tick; terminal tasks naturally stop retrying."""
    with SessionLocal() as db:
        values = {
            "projector_owner": "",
            "projector_lease_until": 0.0,
            "projector_lease_heartbeat": 0.0,
            # A successful tick clears an earlier transient diagnostic. Keep
            # only the latest bounded failure while ownership is released.
            "projector_error": error[:500] if error else "",
        }
        changed = db.execute(update(HarnessTask).where(
            HarnessTask.id == task_id,
            HarnessTask.projector_owner == owner,
        ).values(**values))
        db.commit()
        return changed.rowcount == 1


def _task_scope(task: HarnessTask) -> dict[str, Any]:
    # These claims are signed by the Test Atelier scope secret in the bridge;
    # they are not user-controlled request headers.
    return {
        "subject_user_id": task.user_id,
        "project_id": task.project_id,
        "task_id": task.id,
        "allowed_operation": "task.events",
    }


def _remote_events(bridge: Any, task: HarnessTask, cursor: int) -> Iterator[tuple[str, dict[str, Any]]]:
    request_id = f"harness-projector:{WORKER_ID[:32]}:{task.id[:32]}"
    return bridge.stream_events(task.id, cursor, request_id, _task_scope(task), idle_timeout_seconds=1.0)


def project_task(task_id: str, *, owner: str = WORKER_ID, bridge_factory: Callable[[], Any] | None = None,
                 max_events: int = MAX_EVENTS_PER_TASK) -> int:
    """Consume one claimed task. Transport errors are retried after release."""
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        if task is None or task.projector_owner != owner or task.status in TERMINAL_TASK_STATUSES:
            return 0
        task_snapshot = {
            "id": task.id,
            "project_id": task.project_id,
            "user_id": task.user_id,
            "status": task.status,
        }
        # A detached lightweight object is not needed: the stream only reads
        # these immutable scope fields and task_id.
    class _TaskScope:
        id = task_snapshot["id"]
        project_id = task_snapshot["project_id"]
        user_id = task_snapshot["user_id"]

    task_scope = _TaskScope()
    cursor = remote_cursor(task_id)
    bridge = bridge_factory() if bridge_factory else harness_bridge.HarnessBridge()
    projected = 0
    started = time.monotonic()
    try:
        try:
            events = _remote_events(bridge, task_scope, cursor)
        except TypeError as exc:
            # Rolling upgrades may still provide a bridge adapter without the
            # bounded timeout keyword. The built-in bridge always supports it.
            if "idle_timeout_seconds" not in str(exc):
                raise
            request_id = f"harness-projector:{WORKER_ID[:32]}:{task_id[:32]}"
            events = bridge.stream_events(task_id, cursor, request_id, _task_scope(task_scope))
        for event_name, payload in events:
            # Do not let an earlier valid result leak into the deterministic
            # protocol-error handler for a later malformed event.
            projected_payload = None
            remote_sequence = None
            if stop_event.is_set() or projected >= max_events:
                break
            remote_sequence = validate_remote_event(task_id, event_name, payload, cursor)
            if remote_sequence is None:
                continue
            projected_payload = {**payload, "_remote_sequence": remote_sequence}
            if event_name == "task.result":
                # project_event validates the result and writes the business
                # projection in the same transaction as the durable event.
                project_event(task_id, event_name, projected_payload)
            else:
                project_event(task_id, event_name, projected_payload)
            cursor = remote_sequence
            projected += 1
            if not renew(task_id, owner):
                raise RuntimeError("Harness projector lease lost")
        _log("harness.projector.tick", task_id=task_id, event_count=projected,
             duration_ms=round((time.monotonic() - started) * 1000, 2))
        # Keep the task available if the bounded batch stopped before a
        # terminal event. A later claim resumes from the persisted cursor.
        release(task_id, owner)
        return projected
    except (ValueError, TypeError) as exc:
        # Schema/protocol violations are deterministic and must not retry
        # forever. Consume the offending remote sequence as a local failure
        # event where possible; no source text is retained in the error.
        sequence = locals().get("remote_sequence")
        offending_payload = locals().get("projected_payload")
        failure = projection_failure(exc)
        if isinstance(offending_payload, dict) and isinstance(offending_payload.get("result"), dict):
            project_invalid_result(task_id, offending_payload, exc)
            # Persist only a fixed code: Pydantic text can contain the input.
            release(task_id, owner, error=failure["code"], terminal=True)
            _log("harness.projector.failed", logging.WARNING, task_id=task_id, error_type=type(exc).__name__)
            return projected
        failure_payload: dict[str, Any] = {"task_id": task_id, "error": failure}
        if isinstance(sequence, int):
            failure_payload["_remote_sequence"] = sequence
        project_event(task_id, "task.failed", failure_payload)
        release(task_id, owner, error=failure["code"], terminal=True)
        _log("harness.projector.failed", logging.WARNING, task_id=task_id, error_type=type(exc).__name__)
        return projected
    except harness_bridge.HarnessBridgeError as exc:
        release(task_id, owner, error=type(exc).__name__)
        _log("harness.projector.retry", logging.WARNING, task_id=task_id, error_type=type(exc).__name__)
        return projected
    except Exception as exc:
        release(task_id, owner, error=type(exc).__name__)
        _log("harness.projector.retry", logging.ERROR, task_id=task_id, error_type=type(exc).__name__)
        return projected


def _check_migrations() -> None:
    expected = os.getenv("AI_STUDIO_MIGRATION_HEAD", "20261047_auth_session_revocations")
    if expected != "20261047_auth_session_revocations":
        raise RuntimeError("迁移目标版本已过期，请将 AI_STUDIO_MIGRATION_HEAD 更新到当前版本")
    with SessionLocal() as db:
        actual = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if actual != expected:
            raise RuntimeError(f"数据库迁移版本不匹配：当前 {actual or '空'}，需要 {expected}")
        from .project_write_fence import assert_project_write_fence
        assert_project_write_fence(db)


def _stop(signum: int, _frame: Any) -> None:
    _log("harness.projector.stop_requested", logging.INFO)
    stop_event.set()


def _heartbeat_loop() -> None:
    """Touch a private liveness file; it contains no task or document data."""
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        try:
            HEARTBEAT_PATH.touch()
        except OSError:
            # Database/Host work remains authoritative; healthcheck will make
            # an unwritable heartbeat visible without killing the projector.
            pass
        stop_event.wait(HEARTBEAT_SECONDS)


def run() -> None:
    if os.getenv("TEST_ATELIER_HARNESS_PROJECTOR_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("Harness projector 需要设置 TEST_ATELIER_HARNESS_PROJECTOR_ENABLED=1")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _check_migrations()
    _recover_expired()
    _log("harness.projector.started")
    heartbeat = threading.Thread(target=_heartbeat_loop, name="harness-projector-heartbeat", daemon=True)
    heartbeat.start()
    while not stop_event.is_set():
        try:
            _recover_expired()
            task_id = claim_next()
            if task_id is None:
                stop_event.wait(POLL_SECONDS)
                continue
            project_task(task_id)
        except Exception as exc:
            _log("harness.projector.loop_error", logging.ERROR, error_type=type(exc).__name__)
            stop_event.wait(POLL_SECONDS)
    heartbeat.join(timeout=1)
    _log("harness.projector.stopped")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        _log("harness.projector.start_failed", logging.ERROR, error_type=type(exc).__name__)
        raise SystemExit(1)
