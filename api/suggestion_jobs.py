"""Restartable per-case model suggestion tasks, separate from analysis jobs."""

import logging
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from . import ai_jobs, classification
from .db import Case, CaseSuggestion, ImportBatch, SessionLocal, SuggestionJob, TaxonomyVersion

logger = logging.getLogger(__name__)


def enqueue(job_id: str) -> None:
    ai_jobs.executor.submit(process_job, job_id)


def process_job(job_id: str) -> None:
    try:
        _process_job(job_id)
    except Exception as exc:
        logger.error("Suggestion job %s aborted: %s", job_id, type(exc).__name__)
        try:
            with SessionLocal() as db:
                job = db.get(SuggestionJob, job_id)
                if job and job.status in ("queued", "running"):
                    job.status = "failed"
                    job.errors = [*job.errors, {"case_id": None, "message": "任务中断，请重试"}]
                    db.commit()
        except Exception as persistence_error:
            logger.error("Suggestion job %s status update failed: %s", job_id, type(persistence_error).__name__)


def _process_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(SuggestionJob, job_id)
        if not job or job.status not in ("queued", "running"):
            return
        job.status = "running"; job.completed = 0; job.errors = []
        case_ids = list(job.case_ids)
        project_id, provider, creator = job.project_id, job.provider, job.created_by
        version_id = job.taxonomy_version_id
        definitions = db.get(TaxonomyVersion, version_id).definitions if version_id else None
        db.commit()

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
                with SessionLocal() as db:
                    already_saved = db.scalar(select(CaseSuggestion.id).where(CaseSuggestion.job_id == job_id,
                                                                            CaseSuggestion.case_id == case_id).limit(1))
                    if not already_saved:
                        db.add(CaseSuggestion(project_id=project_id, case_id=case_id, taxonomy_version_id=version_id,
                                              job_id=job_id, source="model", status="pending", case_version=version,
                                              created_by=creator, **candidate))
                        db.commit()
        except Exception as exc:
            logger.warning("Suggestion job %s case %s failed: %s", job_id, case_id, type(exc).__name__)
            if isinstance(exc, httpx.HTTPStatusError):
                message = f"模型服务返回 {exc.response.status_code}，请检查模型配置"
            elif type(exc) in (RuntimeError, ValueError):
                message = str(exc)[:180]
            else:
                message = "模型输出或服务异常，请重试"
            error = {"case_id": case_id, "message": message}
        with SessionLocal() as db:
            job = db.get(SuggestionJob, job_id)
            job.completed += 1
            if error:
                job.errors = [*job.errors, error]
            db.commit()

    with SessionLocal() as db:
        job = db.get(SuggestionJob, job_id)
        job.status = "done" if not job.errors else "failed" if len(job.errors) == job.total else "partial"
        db.commit()


def recover_jobs() -> None:
    with SessionLocal() as db:
        ids = [item.id for item in db.scalars(select(SuggestionJob).where(SuggestionJob.status.in_(["queued", "running"]))).all()]
        for job_id in ids:
            db.get(SuggestionJob, job_id).status = "queued"
        db.commit()
    for job_id in ids:
        enqueue(job_id)
