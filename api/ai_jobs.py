"""Persisted jobs with restart recovery; no model call runs in an HTTP request."""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
import httpx

from . import ai_engine, skill_runner
from .db import AiJob, AnalysisRun, CaseDraft, SessionLocal, SourceDocument, utcnow

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-studio")


def enqueue(job_id: str) -> None:
    executor.submit(process_job, job_id)


def process_job(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(AiJob, job_id)
        if not job or job.status not in {"queued", "running"}:
            return
        run = db.get(AnalysisRun, job.analysis_id)
        job.status = "running"
        job.attempts += 1
        run.status = "running" if job.kind == "analysis" else run.status
        db.commit()
        kind, provider, result = job.kind, run.provider, run.result
        documents = []
        if kind == "analysis":
            for document_id in run.source_ids:
                document = db.get(SourceDocument, document_id)
                documents.append({"id": document.id, "filename": document.filename, "text": document.extracted_text})
    try:
        output = (skill_runner.analyze(documents, provider, ai_engine.request_structured)
                  if kind == "analysis" else skill_runner.generate_cases(result, provider, ai_engine.request_structured))
        with SessionLocal() as db:
            job = db.get(AiJob, job_id)
            run = db.get(AnalysisRun, job.analysis_id)
            if kind == "analysis":
                run.result = output
                run.status = "ready"
                run.updated_at = utcnow()
                run.version += 1
            else:
                for payload in output:
                    db.add(CaseDraft(id=uuid.uuid4().hex, project_id=job.project_id, analysis_id=run.id, payload=payload))
            job.status = "done"
            job.error = ""
            db.commit()
    except Exception as exc:
        logger.warning("AI job %s failed: %s", job_id, type(exc).__name__)
        if isinstance(exc, httpx.HTTPStatusError):
            message = f"模型服务返回 {exc.response.status_code}，请检查模型名称或服务配置"
        elif type(exc) in {ValueError, RuntimeError}:
            message = str(exc)[:500]
        else:
            message = "AI 任务失败，请检查模型输出与服务配置后重试"
        with SessionLocal() as db:
            job = db.get(AiJob, job_id)
            run = db.get(AnalysisRun, job.analysis_id)
            job.status = "failed"
            job.error = message
            if kind == "analysis":
                run.status = "failed"
                run.error = message
            db.commit()


def recover_jobs() -> None:
    with SessionLocal() as db:
        ids = [item.id for item in db.scalars(select(AiJob).where(AiJob.status.in_(["queued", "running"]))).all()]
        for job_id in ids:
            db.get(AiJob, job_id).status = "queued"
        db.commit()
    for job_id in ids:
        enqueue(job_id)
