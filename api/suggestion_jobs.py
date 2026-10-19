"""Restartable per-case model suggestion tasks, separate from analysis jobs."""

import logging
import time
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from . import ai_engine, ai_jobs, classification
from .db import Case, CaseSuggestion, ImportBatch, SessionLocal, SuggestionJob, TaxonomyVersion
from .observability import monotonic, redact_sensitive_text

logger = logging.getLogger(__name__)


def enqueue(job_id: str, correlation_id: str = "") -> bool:
    if ai_jobs.EXTERNAL_WORKER:
        if not ai_jobs.reserve_external_slot(job_id, SuggestionJob):
            with SessionLocal() as db:
                job = db.get(SuggestionJob, job_id)
                if job and job.status == "queued":
                    job.status = "failed"
                    job.errors = [{"case_id": None, "message": "AI 任务队列已满，请稍后重试"}]
                    job.lease_owner = ""
                    job.lease_until = 0.0
                    job.lease_heartbeat = 0.0
                    ai_jobs.release_external_slot_in_session(db, job)
                    db.commit()
            return False
        logger.info("suggestion_job.deferred", extra={
            "event": "suggestion_job.deferred", "job_id": job_id,
        })
        return True
    accepted = ai_jobs.submit_worker(job_id, process_job, correlation_id)
    if not accepted:
        with SessionLocal() as db:
            job = db.get(SuggestionJob, job_id)
            if job and job.status == "queued":
                job.status = "failed"
                job.errors = [{"case_id": None, "message": "AI 任务队列已满，请稍后重试"}]
                job.lease_owner = ""
                job.lease_until = 0.0
                job.lease_heartbeat = 0.0
                db.commit()
        logger.warning("suggestion_job.queue_full", extra={
            "event": "suggestion_job.queue_full", "job_id": job_id,
            "error_type": "QueueFull", "total": ai_jobs.MAX_SUBMITTED_JOBS,
        })
    return accepted


def process_job(job_id: str, _correlation_id: str = "", lease_owner: str = "") -> None:
    started = monotonic()
    try:
        _process_job(job_id, lease_owner)
    except ai_engine.CliCancelled:
        # Keep the lease so a restarted worker can reclaim the durable job;
        # shutdown cancellation is not a provider failure.
        logger.info("suggestion_job.cancelled_for_shutdown", extra={
            "event": "suggestion_job.cancelled_for_shutdown", "job_id": job_id,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        return
    except Exception as exc:
        logger.error("suggestion_job.aborted", extra={
            "event": "suggestion_job.aborted", "job_id": job_id,
            "error_type": type(exc).__name__,
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })
        try:
            with SessionLocal() as db:
                job = db.get(SuggestionJob, job_id)
                if job and job.status in ("queued", "running") and (not lease_owner or job.lease_owner == lease_owner):
                    job.status = "failed"
                    job.errors = [*job.errors, {"case_id": None, "message": "任务中断，请重试"}]
                    job.lease_owner = ""
                    job.lease_until = 0.0
                    job.lease_heartbeat = 0.0
                    ai_jobs.release_external_slot_in_session(db, job)
                    db.commit()
        except Exception as persistence_error:
            logger.error("suggestion_job.status_persist_failed", extra={
                "event": "suggestion_job.status_persist_failed", "job_id": job_id,
                "error_type": type(persistence_error).__name__,
                "duration_ms": round((monotonic() - started) * 1000, 2),
            })


def _process_job(job_id: str, lease_owner: str = "") -> None:
    started = monotonic()
    cancel_event = ai_engine.get_cancel_event()
    if cancel_event is not None and cancel_event.is_set():
        raise ai_engine.CliCancelled("worker stop requested")
    with SessionLocal() as db:
        job = db.get(SuggestionJob, job_id)
        if not job or job.status not in ("queued", "running"):
            return
        if lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time()):
            logger.error("suggestion_job.lease_lost", extra={
                "event": "suggestion_job.lease_lost", "job_id": job_id,
                "project_id": job.project_id,
            })
            return
        job.status = "running"; job.completed = 0; job.errors = []
        case_ids = list(job.case_ids)
        project_id, provider, creator = job.project_id, job.provider, job.created_by
        version_id = job.taxonomy_version_id
        definitions = db.get(TaxonomyVersion, version_id).definitions if version_id else None
        db.commit()
    logger.info("suggestion_job.started", extra={
        "event": "suggestion_job.started", "job_id": job_id,
        "project_id": project_id, "provider": provider, "total": len(case_ids),
    })

    for case_id in case_ids:
        error = None
        try:
            with SessionLocal() as db:
                exists = db.scalar(select(CaseSuggestion.id).where(CaseSuggestion.job_id == job_id,
                                                                     CaseSuggestion.case_id == case_id).limit(1))
                if not exists:
                    case = db.get(Case, case_id)
                    if not case or case.project_id != project_id:
                        raise ValueError("用例不存在于当前项目")
                    headers = db.get(ImportBatch, case.import_id).headers
                    version = case.version
                    snapshot = SimpleNamespace(original=list(case.original), directory=case.directory,
                                               level=case.level, smoke=case.smoke)
                    rule = classification.rule_candidate(snapshot, headers, definitions)
            if not exists:
                candidate = classification.model_candidate(snapshot, headers, definitions, rule, provider)
                cancel_event = ai_engine.get_cancel_event()
                if cancel_event is not None and cancel_event.is_set():
                    raise ai_engine.CliCancelled("worker stop requested")
                with SessionLocal() as db:
                    # Re-check ownership while holding the durable job row
                    # lock. A provider turn can outlive its lease; without
                    # this guard an expired worker could persist a suggestion
                    # after another worker had already reclaimed the job.
                    owner = db.get(SuggestionJob, job_id, with_for_update=True)
                    if not owner or (lease_owner and (
                            owner.lease_owner != lease_owner or owner.lease_until <= time.time())):
                        logger.error("suggestion_job.lease_lost", extra={
                            "event": "suggestion_job.lease_lost", "job_id": job_id,
                            "project_id": project_id,
                        })
                        db.rollback()
                        return
                    already_saved = db.scalar(select(CaseSuggestion.id).where(CaseSuggestion.job_id == job_id,
                                                                            CaseSuggestion.case_id == case_id).limit(1))
                    if not already_saved:
                        db.add(CaseSuggestion(project_id=project_id, case_id=case_id, taxonomy_version_id=version_id,
                                              job_id=job_id, source="model", status="pending", case_version=version,
                                              created_by=creator, **candidate))
                        db.commit()
        except ai_engine.CliCancelled:
            # Preserve the running lease so a restarted worker can reclaim
            # this durable suggestion job instead of recording a fake model
            # error during graceful shutdown.
            raise
        except Exception as exc:
            logger.warning("suggestion_case.failed", extra={
                "event": "suggestion_case.failed", "job_id": job_id,
                "case_id": case_id, "error_type": type(exc).__name__,
            })
            if isinstance(exc, httpx.HTTPStatusError):
                message = f"模型服务返回 {exc.response.status_code}，请检查模型配置"
            elif type(exc) in (RuntimeError, ValueError):
                message = redact_sensitive_text(str(exc), 180)
            else:
                message = "模型输出或服务异常，请重试"
            error = {"case_id": case_id, "message": message}
        with SessionLocal() as db:
            cancel_event = ai_engine.get_cancel_event()
            if cancel_event is not None and cancel_event.is_set():
                raise ai_engine.CliCancelled("worker stop requested")
            job = db.get(SuggestionJob, job_id)
            if not job or (lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time())):
                logger.error("suggestion_job.lease_lost", extra={
                    "event": "suggestion_job.lease_lost", "job_id": job_id,
                    "project_id": project_id,
                })
                return
            job.completed += 1
            if error:
                job.errors = [*job.errors, error]
            db.commit()

    cancel_event = ai_engine.get_cancel_event()
    if cancel_event is not None and cancel_event.is_set():
        raise ai_engine.CliCancelled("worker stop requested")
    with SessionLocal() as db:
        job = db.get(SuggestionJob, job_id)
        if not job or (lease_owner and (job.lease_owner != lease_owner or job.lease_until <= time.time())):
            logger.error("suggestion_job.lease_lost", extra={
                "event": "suggestion_job.lease_lost", "job_id": job_id,
                "project_id": project_id,
            })
            return
        job.status = "done" if not job.errors else "failed" if len(job.errors) == job.total else "partial"
        job.lease_owner = ""
        job.lease_until = 0.0
        job.lease_heartbeat = 0.0
        ai_jobs.release_external_slot_in_session(db, job)
        db.commit()
        logger.info("suggestion_job.finished", extra={
            "event": "suggestion_job.finished", "job_id": job_id,
            "project_id": job.project_id, "provider": job.provider,
            "status": job.status, "completed": job.completed,
            "total": job.total, "error_count": len(job.errors or []),
            "duration_ms": round((monotonic() - started) * 1000, 2),
        })


def recover_jobs() -> None:
    if ai_jobs.EXTERNAL_WORKER:
        return
    with SessionLocal() as db:
        jobs = [(item.id, item.request_id or "") for item in db.scalars(
            select(SuggestionJob).where(SuggestionJob.status.in_(["queued", "running"]))
        ).all()]
        ids = [job_id for job_id, _ in jobs]
        for job_id, _ in jobs:
            job = db.get(SuggestionJob, job_id)
            job.status = "queued"
            job.lease_owner = ""
            job.lease_until = 0.0
            job.lease_heartbeat = 0.0
            ai_jobs.release_external_slot_in_session(db, job)
        db.commit()
    for job_id, correlation_id in jobs:
        enqueue(job_id, correlation_id)
    if ids:
        logger.info("suggestion_jobs.recovered", extra={"event": "suggestion_jobs.recovered", "completed": len(ids)})
