"""Persisted jobs with restart recovery; no model call runs in an HTTP request."""

import logging
import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
import httpx

from . import ai_engine, skill_runner
from . import metrics
from .db import AiJob, AiQueueState, AnalysisRun, CaseDraft, SessionLocal, SourceDocument, utcnow
from .observability import env_int, monotonic, redact_sensitive_text, reset_request_id, set_request_id

logger = logging.getLogger(__name__)
executor: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-studio")
MAX_SUBMITTED_JOBS = env_int("AI_STUDIO_MAX_SUBMITTED_JOBS", 100, 1)
EXTERNAL_WORKER = os.getenv("AI_STUDIO_EXTERNAL_WORKER", "0").lower() in {"1", "true", "yes", "on"}
_submission_slots = threading.BoundedSemaphore(MAX_SUBMITTED_JOBS)
_active_jobs: set[str] = set()
_active_jobs_lock = threading.Lock()
_futures: dict[str, object] = {}
_accepting = True
_shutdown_event = threading.Event()
metrics.set_ai_queue(0, MAX_SUBMITTED_JOBS)


class _PromptCapture:
    """Persist exact model prompts incrementally, including failed attempts."""

    def __init__(self, job_id: str, existing: list[str] | None = None):
        self.job_id = job_id
        self.prompts: list[str] = list(existing or [])

    def record(self, prompt: str) -> None:
        # The prompt is intentionally not logged.  It can contain source
        # requirements and other project-sensitive material.
        self.prompts.append(prompt)
        encoded = json.dumps(self.prompts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        with SessionLocal() as db:
            job = db.get(AiJob, self.job_id)
            if not job:
                return
            job.prompts = list(self.prompts)
            job.prompt_length = sum(len(item) for item in self.prompts)
            job.prompt_calls = len(self.prompts)
            job.prompt_sha256 = digest
            db.commit()
        logger.debug("ai_prompt.captured", extra={
            "event": "ai_prompt.captured", "job_id": self.job_id,
            "prompt_calls": len(self.prompts), "prompt_length": sum(len(item) for item in self.prompts),
        })


def submit_worker(job_id: str, worker, correlation_id: str = "") -> bool:
    """Submit a worker only while bounded queue capacity remains."""
    global executor
    # Recovery and a user retry can race with an already-submitted task.  A
    # process-local job guard prevents two workers from resetting the same
    # persisted status or writing duplicate drafts.
    def run_worker():
        token = set_request_id(correlation_id) if correlation_id else None
        try:
            return worker(job_id)
        finally:
            if token is not None:
                reset_request_id(token)

    # Register the future while holding the same lock used by shutdown.  This
    # prevents a shutdown from missing a just-submitted task and avoids a
    # completed future being inserted back into the registry after cleanup.
    with _active_jobs_lock:
        if not _accepting:
            return False
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-studio")
        if job_id in _active_jobs:
            return True
        if not _submission_slots.acquire(blocking=False):
            logger.warning("ai_job.queue_full", extra={
                "event": "ai_job.queue_full", "job_id": job_id,
                "error_type": "QueueFull", "total": MAX_SUBMITTED_JOBS,
            })
            return False
        _active_jobs.add(job_id)
        metrics.set_ai_queue(len(_active_jobs), MAX_SUBMITTED_JOBS)
        try:
            future = executor.submit(run_worker)
            _futures[job_id] = future
        except Exception:
            _active_jobs.discard(job_id)
            metrics.set_ai_queue(len(_active_jobs), MAX_SUBMITTED_JOBS)
            _submission_slots.release()
            raise

    def cleanup(_future):
        with _active_jobs_lock:
            _futures.pop(job_id, None)
            _active_jobs.discard(job_id)
            metrics.set_ai_queue(len(_active_jobs), MAX_SUBMITTED_JOBS)
            if _shutdown_event.is_set() and not _active_jobs:
                ai_engine.set_cancel_event(None)
        _submission_slots.release()

    future.add_done_callback(cleanup)
    return True


def _mark_queue_full(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(AiJob, job_id)
        if job and job.status == "queued":
            job.status = "failed"
            job.error = "AI 任务队列已满，请稍后重试"
            job.lease_owner = ""
            job.lease_until = 0.0
            job.lease_heartbeat = 0.0
            release_external_slot_in_session(db, job)
            run = db.get(AnalysisRun, job.analysis_id)
            if run and job.kind == "analysis":
                run.status = "failed"
                run.error = job.error
            db.commit()


def _ensure_queue_state(db) -> AiQueueState:
    state = db.get(AiQueueState, 1)
    if state is None:
        state = AiQueueState(id=1, active_jobs=0, updated_at=utcnow())
        try:
            # API replicas may create the singleton concurrently. A savepoint
            # lets the losing transaction roll back only this insert while
            # retaining the caller's job reservation transaction.
            with db.begin_nested():
                db.add(state)
                db.flush()
        except IntegrityError:
            state = db.get(AiQueueState, 1)
            if state is None:
                raise
    return state


def reserve_external_slot(job_id: str, table=AiJob) -> bool:
    """Atomically reserve one durable queue slot across API replicas."""
    with SessionLocal() as db:
        job = db.get(table, job_id)
        if not job or job.queue_reserved:
            return bool(job and job.queue_reserved)
        # Lock the job row first so duplicate HTTP retries cannot reserve two
        # capacity units for the same durable job.
        marked = db.execute(update(table).where(
            table.id == job_id, table.queue_reserved.is_(False), table.status == "queued",
        ).values(queue_reserved=True))
        if marked.rowcount != 1:
            db.rollback()
            current = db.get(table, job_id)
            return bool(current and current.queue_reserved)
        state = _ensure_queue_state(db)
        changed = db.execute(update(AiQueueState).where(
            AiQueueState.id == 1, AiQueueState.active_jobs < MAX_SUBMITTED_JOBS,
        ).values(active_jobs=AiQueueState.active_jobs + 1, updated_at=utcnow()))
        if changed.rowcount != 1:
            logger.warning("ai_queue.capacity_exhausted", extra={
                "event": "ai_queue.capacity_exhausted", "job_id": job_id,
                "active_jobs": state.active_jobs, "queue_limit": MAX_SUBMITTED_JOBS,
                "component": "external-queue",
            })
            db.rollback()
            return False
        db.commit()
        active_jobs = db.scalar(select(AiQueueState.active_jobs).where(AiQueueState.id == 1)) or 0
        metrics.set_ai_queue(active_jobs, MAX_SUBMITTED_JOBS)
        logger.info("ai_queue.slot_reserved", extra={
            "event": "ai_queue.slot_reserved", "job_id": job_id,
            "active_jobs": active_jobs, "queue_limit": MAX_SUBMITTED_JOBS,
            "component": "external-queue",
        })
        return True


def release_external_slot_in_session(db, job) -> None:
    """Release a reservation in the same transaction that finalizes a job."""
    if not getattr(job, "queue_reserved", False):
        return
    job.queue_reserved = False
    # Use an arithmetic UPDATE instead of read/modify/write.  Two workers can
    # finish different jobs at the same time; assigning a stale ORM value
    # would let the second commit overwrite the first decrement and leave the
    # durable admission counter too high or too low.
    _ensure_queue_state(db)
    db.execute(update(AiQueueState).where(
        AiQueueState.id == 1, AiQueueState.active_jobs > 0,
    ).values(active_jobs=AiQueueState.active_jobs - 1, updated_at=utcnow()))
    active_jobs = db.scalar(select(AiQueueState.active_jobs).where(AiQueueState.id == 1)) or 0
    metrics.set_ai_queue(active_jobs, MAX_SUBMITTED_JOBS)


def release_external_slot(job_id: str, table=AiJob) -> None:
    with SessionLocal() as db:
        job = db.get(table, job_id)
        if job is not None:
            release_external_slot_in_session(db, job)
            db.commit()


def external_queue_available() -> bool:
    """Report durable queue capacity without making an admission decision."""
    with SessionLocal() as db:
        _ensure_queue_state(db)
        db.commit()
        active = db.scalar(select(AiQueueState.active_jobs).where(AiQueueState.id == 1)) or 0
        return active < MAX_SUBMITTED_JOBS


def enqueue(job_id: str, correlation_id: str = "") -> bool:
    if EXTERNAL_WORKER:
        if not reserve_external_slot(job_id, AiJob):
            _mark_queue_full(job_id)
            return False
        # The durable row is the queue.  A separate ``api.worker`` process
        # claims it, so the HTTP process never owns model execution.
        logger.info("ai_job.deferred", extra={
            "event": "ai_job.deferred", "job_id": job_id,
        })
        return True
    accepted = submit_worker(job_id, process_job, correlation_id)
    if not accepted:
        _mark_queue_full(job_id)
    return accepted


def process_job(job_id: str, lease_owner: str = "") -> None:
    """Run a job and persist an unexpected worker failure instead of dropping it."""
    started = monotonic()
    try:
        _process_job(job_id, lease_owner)
    except ai_engine.CliCancelled:
        # Keep the durable row running with its lease intact.  A restarted
        # worker will reclaim it after the lease expires, preserving the
        # prompt history and avoiding a false provider failure on shutdown.
        logger.info("ai_job.cancelled_for_shutdown", extra={
            "event": "ai_job.cancelled_for_shutdown", "job_id": job_id,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        return
    except Exception as exc:
        logger.exception("ai_job.aborted", extra={
            "event": "ai_job.aborted", "job_id": job_id,
            "error_type": type(exc).__name__,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        try:
            with SessionLocal() as db:
                job = db.get(AiJob, job_id)
                if job and job.status in {"queued", "running"} and (not lease_owner or job.lease_owner == lease_owner):
                    job.status = "failed"
                    job.error = "AI 任务中断，请重试"
                    job.lease_owner = ""
                    job.lease_until = 0.0
                    job.lease_heartbeat = 0.0
                    run = db.get(AnalysisRun, job.analysis_id)
                    if run and job.kind == "analysis":
                        run.status = "failed"
                        run.error = job.error
                    release_external_slot_in_session(db, job)
                    db.commit()
        except Exception as persistence_error:
            logger.error("ai_job.failure_persist_failed", extra={
                "event": "ai_job.failure_persist_failed", "job_id": job_id,
                "error_type": type(persistence_error).__name__,
            })


def active_count() -> int:
    with _active_jobs_lock:
        return len(_active_jobs)


def start_workers() -> None:
    """Re-open the local executor when an embedded app restarts in-process."""
    global executor, _accepting
    if EXTERNAL_WORKER:
        return
    with _active_jobs_lock:
        _shutdown_event.clear()
        # Embedded CLI calls need the same cancellation path as the external
        # worker so API shutdown does not wait for the full model timeout.
        ai_engine.set_cancel_event(_shutdown_event)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-studio")
        _accepting = True
        metrics.set_ai_queue(len(_active_jobs), MAX_SUBMITTED_JOBS)


def shutdown_workers(timeout_seconds: float = 25.0) -> int:
    """Stop accepting work and cancel queued jobs during process shutdown."""
    global executor, _accepting
    if EXTERNAL_WORKER:
        return 0
    _shutdown_event.set()
    with _active_jobs_lock:
        _accepting = False
        current = executor
        futures = list(_futures.values())
    # ``Future.cancel()`` invokes done callbacks synchronously when a queued
    # task is cancelled.  Calling it while holding ``_active_jobs_lock``
    # deadlocks because the cleanup callback takes the same lock.
    for future in futures:
        future.cancel()
    if current is None:
        ai_engine.set_cancel_event(None)
        return 0
    _, _pending = wait(futures, timeout=max(0.0, timeout_seconds))
    current.shutdown(wait=False, cancel_futures=True)
    with _active_jobs_lock:
        # Running tasks may outlive the grace period, but a future startup can
        # use a fresh executor; the active-job guard prevents duplicate IDs.
        executor = None
        remaining = len(_active_jobs)
        metrics.set_ai_queue(remaining, MAX_SUBMITTED_JOBS)
        if remaining == 0:
            ai_engine.set_cancel_event(None)
    return remaining


def _process_job(job_id: str, lease_owner: str = "") -> None:
    started = monotonic()
    with SessionLocal() as db:
        job = db.get(AiJob, job_id)
        if not job or job.status not in {"queued", "running"}:
            return
        if lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time()):
            logger.error("ai_job.lease_lost", extra={
                "event": "ai_job.lease_lost", "job_id": job_id,
                "project_id": job.project_id, "kind": job.kind,
            })
            return
        run = db.get(AnalysisRun, job.analysis_id)
        job.status = "running"
        job.attempts += 1
        if lease_owner:
            job.lease_heartbeat = job.lease_until
        run.status = "running" if job.kind == "analysis" else run.status
        db.commit()
        kind, provider, result = job.kind, run.provider, run.result
        previous_prompts = list(job.prompts or [])
        documents = []
        if kind == "analysis":
            for document_id in run.source_ids:
                document = db.get(SourceDocument, document_id)
                documents.append({"id": document.id, "filename": document.filename, "text": document.extracted_text})
        logger.info("ai_job.started", extra={
            "event": "ai_job.started", "job_id": job_id, "project_id": job.project_id,
            "kind": kind, "provider": provider, "attempt": job.attempts,
        })
    # Keep prompts from earlier retries: they are still prompts sent by this
    # task and are useful when diagnosing an intermittent provider failure.
    capture = _PromptCapture(job_id, previous_prompts)

    def request_with_capture(prompt, schema, selected_provider):
        capture.record(prompt)
        return ai_engine.request_structured(prompt, schema, selected_provider)

    try:
        output = (skill_runner.analyze(documents, provider, request_with_capture)
                  if kind == "analysis" else skill_runner.generate_cases(result, provider, request_with_capture))
        # A provider can finish at the same moment a worker receives SIGTERM
        # or loses its lease. Do not turn that race into a successful durable
        # write; keep the row recoverable for the next worker instead.
        cancel_event = ai_engine.get_cancel_event()
        if cancel_event is not None and cancel_event.is_set():
            raise ai_engine.CliCancelled("worker stop requested")
        with SessionLocal() as db:
            job = db.get(AiJob, job_id)
            if not job:
                logger.error("ai_job.missing_during_finalize", extra={
                    "event": "ai_job.missing_during_finalize", "job_id": job_id,
                })
                db.rollback()
                return
            run = db.get(AnalysisRun, job.analysis_id)
            if kind == "analysis":
                run.result = output
                run.status = "ready"
                run.error = ""
                run.updated_at = utcnow()
                run.version += 1
            else:
                for payload in output:
                    db.add(CaseDraft(id=uuid.uuid4().hex, project_id=job.project_id, analysis_id=run.id, payload=payload))
            if lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time()):
                logger.error("ai_job.lease_lost", extra={
                    "event": "ai_job.lease_lost", "job_id": job_id,
                    "project_id": job.project_id, "kind": kind,
                })
                db.rollback()
                return
            job.status = "done"
            job.error = ""
            job.lease_owner = ""
            job.lease_until = 0.0
            job.lease_heartbeat = 0.0
            release_external_slot_in_session(db, job)
            db.commit()
            logger.info("ai_job.succeeded", extra={
                "event": "ai_job.succeeded", "job_id": job_id, "project_id": job.project_id,
                "kind": kind, "provider": provider, "attempt": job.attempts,
                "prompt_calls": job.prompt_calls or 0, "prompt_length": job.prompt_length or 0,
                "duration_ms": round((monotonic() - started) * 1000, 2),
            })
    except Exception as exc:
        logger.warning("ai_job.failed", extra={
            "event": "ai_job.failed", "job_id": job_id, "error_type": type(exc).__name__,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        if isinstance(exc, httpx.HTTPStatusError):
            message = f"模型服务返回 {exc.response.status_code}，请检查模型名称或服务配置"
        elif isinstance(exc, skill_runner.SkillRunnerError):
            # Skill diagnostics may contain host paths; keep those in the
            # structured server log rather than returning them to reviewers.
            message = "技能工作流不可用，请检查技能仓配置"
        elif type(exc) in {ValueError, RuntimeError}:
            # Provider diagnostics can include URLs or credential-shaped
            # values. Keep the persisted user-facing error useful without
            # turning the job record into a secret sink.
            message = redact_sensitive_text(str(exc), 500)
        else:
            message = "AI 任务失败，请检查模型输出与服务配置后重试"
        with SessionLocal() as db:
            job = db.get(AiJob, job_id)
            if not job:
                logger.error("ai_job.missing_during_failure", extra={
                    "event": "ai_job.missing_during_failure", "job_id": job_id,
                })
                db.rollback()
                return
            run = db.get(AnalysisRun, job.analysis_id)
            if lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time()):
                logger.error("ai_job.lease_lost", extra={
                    "event": "ai_job.lease_lost", "job_id": job_id,
                    "project_id": job.project_id, "kind": kind,
                })
                db.rollback()
                return
            job.status = "failed"
            job.error = message
            job.lease_owner = ""
            job.lease_until = 0.0
            job.lease_heartbeat = 0.0
            if kind == "analysis":
                run.status = "failed"
                run.error = message
            release_external_slot_in_session(db, job)
            db.commit()
            logger.warning("ai_job.failed.persisted", extra={
                "event": "ai_job.failed.persisted", "job_id": job_id,
                "project_id": job.project_id, "kind": kind, "provider": provider,
                "attempt": job.attempts, "prompt_calls": job.prompt_calls or 0,
                "prompt_length": job.prompt_length or 0,
                "duration_ms": round((monotonic() - started) * 1000, 2),
            })


def recover_jobs() -> None:
    if EXTERNAL_WORKER:
        return
    with SessionLocal() as db:
        jobs = [(item.id, item.request_id or "") for item in db.scalars(
            select(AiJob).where(AiJob.status.in_(["queued", "running"]))
        ).all()]
        ids = [job_id for job_id, _ in jobs]
        for job_id, _ in jobs:
            job = db.get(AiJob, job_id)
            job.status = "queued"
            job.lease_owner = ""
            job.lease_until = 0.0
            job.lease_heartbeat = 0.0
            release_external_slot_in_session(db, job)
        db.commit()
    for job_id, correlation_id in jobs:
        enqueue(job_id, correlation_id)
    if ids:
        logger.info("ai_jobs.recovered", extra={"event": "ai_jobs.recovered", "completed": len(ids)})
