"""Project classification versions, suggestion tasks and human decisions."""

import copy
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import ai_engine, classification, suggestion_jobs
from .db import (Case, CaseSuggestion, ImportBatch, ReviewEvent, SuggestionJob,
                 TaxonomyVersion, User, utcnow)
from .observability import request_id as current_request_id

logger = logging.getLogger(__name__)


class TaxonomyUpdate(BaseModel):
    revision: int
    definitions: dict


class TaxonomyPublish(BaseModel):
    revision: int


class CaseIds(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=100)


class ModelRequest(CaseIds):
    provider: Literal["local", "remote", "codex", "codex_cli", "claude", "claude_code"] = "local"


class SuggestionChoice(BaseModel):
    categories: dict[str, str | None]
    level: str | None = None
    smoke: str | None = None


class SuggestionDecision(BaseModel):
    action: Literal["accept", "reject"]
    case_version: int
    reason: str = Field(min_length=1, max_length=1000)
    choice: SuggestionChoice | None = None


def version_dict(item: TaxonomyVersion | None) -> dict | None:
    if item is None:
        return None
    return {"id": item.id, "number": item.number, "status": item.status, "revision": item.revision,
            "definitions": item.definitions, "created_at": item.created_at, "published_at": item.published_at}


def job_dict(item: SuggestionJob) -> dict:
    return {"id": item.id, "project_id": item.project_id, "provider": item.provider, "status": item.status,
            "total": item.total, "completed": item.completed, "errors": item.errors,
            "taxonomy_version_id": item.taxonomy_version_id, "case_ids": item.case_ids,
            "request_id": item.request_id or "", "created_at": item.created_at}


def suggestion_dict(item: CaseSuggestion, case_version: int, definitions: dict | None) -> dict:
    options = definitions or classification.empty_definitions()
    labels = {key: next((entry["name"] for entry in options[key] if entry["id"] == item.categories.get(key)), None)
              for key in classification.DIMENSIONS}
    return {"id": item.id, "case_id": item.case_id, "source": item.source, "status": item.status,
            "case_version": item.case_version, "stale": item.status == "pending" and item.case_version != case_version,
            "taxonomy_version_id": item.taxonomy_version_id, "categories": item.categories,
            "category_labels": labels, "level": item.level, "smoke": item.smoke,
            "rationale": item.rationale, "questions": item.questions,
            "created_at": item.created_at, "decided_at": item.decided_at,
            "decision_reason": item.decision_reason}


def build_router(get_db, current_user, csrf, project_access, writable, project_manager, project_role,
                 case_dict, case_assignee) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["case-suggestions"])

    def find_case(project_id: int, case_id: int, db: Session) -> Case:
        item = db.get(Case, case_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "用例不存在")
        return item

    def published(project_id: int, db: Session) -> TaxonomyVersion | None:
        return db.scalar(select(TaxonomyVersion).where(TaxonomyVersion.project_id == project_id,
                                                      TaxonomyVersion.status == "published")
                         .order_by(TaxonomyVersion.number.desc()).limit(1))

    @router.get("/taxonomy")
    def taxonomy(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        role = project_role(project_id, user, db)
        versions = db.scalars(select(TaxonomyVersion).where(TaxonomyVersion.project_id == project_id)
                              .order_by(TaxonomyVersion.number.desc())).all()
        visible = versions if role in ("admin", "manager") else [item for item in versions if item.status == "published"]
        return {"published": version_dict(next((item for item in versions if item.status == "published"), None)),
                "draft": version_dict(next((item for item in versions if item.status == "draft"), None)) if role in ("admin", "manager") else None,
                "versions": [{"id": item.id, "number": item.number, "status": item.status, "created_at": item.created_at,
                              "published_at": item.published_at} for item in visible]}

    @router.get("/taxonomy/versions/{version_id}")
    def taxonomy_version(project_id: int, version_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        role = project_role(project_id, user, db)
        item = db.get(TaxonomyVersion, version_id)
        if not item or item.project_id != project_id or (item.status == "draft" and role not in ("admin", "manager")):
            raise HTTPException(404, "分类版本不存在")
        return version_dict(item)

    @router.post("/taxonomy/versions", dependencies=[Depends(csrf)])
    def create_version(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_manager(user, project_id, db)
        if db.scalar(select(TaxonomyVersion.id).where(TaxonomyVersion.project_id == project_id,
                                                      TaxonomyVersion.status == "draft").limit(1)):
            raise HTTPException(409, "已有未发布的分类草稿")
        current = published(project_id, db)
        item = TaxonomyVersion(project_id=project_id, number=(current.number + 1 if current else 1),
                               status="draft", revision=1,
                               definitions=copy.deepcopy(current.definitions if current else classification.empty_definitions()),
                               created_by=user.id)
        db.add(item); db.commit()
        logger.info("taxonomy.version.created", extra={
            "event": "taxonomy.version.created", "project_id": project_id,
            "actor_id": user.id, "version": item.number, "revision": item.revision,
            "component": "taxonomy",
        })
        return version_dict(item)

    @router.put("/taxonomy/versions/{version_id}", dependencies=[Depends(csrf)])
    def edit_version(project_id: int, version_id: int, payload: TaxonomyUpdate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_manager(user, project_id, db)
        item = db.get(TaxonomyVersion, version_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "分类版本不存在")
        if item.status != "draft" or item.revision != payload.revision:
            raise HTTPException(409, "分类版本已更新，请刷新")
        try:
            definitions = classification.normalize_definitions(payload.definitions, item.definitions)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        changed = db.execute(update(TaxonomyVersion).where(TaxonomyVersion.id == version_id,
                                                           TaxonomyVersion.revision == payload.revision,
                                                           TaxonomyVersion.status == "draft")
                             .values(definitions=definitions, revision=TaxonomyVersion.revision + 1)
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback(); raise HTTPException(409, "分类版本已更新，请刷新")
        db.commit(); db.refresh(item)
        logger.info("taxonomy.version.updated", extra={
            "event": "taxonomy.version.updated", "project_id": project_id,
            "actor_id": user.id, "taxonomy_version_id": item.id,
            "version": item.number, "revision": item.revision,
            "component": "taxonomy",
        })
        return version_dict(item)

    @router.post("/taxonomy/versions/{version_id}/publish", dependencies=[Depends(csrf)])
    def publish_version(project_id: int, version_id: int, payload: TaxonomyPublish,
                        user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_manager(user, project_id, db)
        item = db.get(TaxonomyVersion, version_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "分类版本不存在")
        if item.status != "draft" or item.revision != payload.revision:
            raise HTTPException(409, "分类版本已更新，请刷新")
        if any(not item.definitions.get(key) for key in classification.DIMENSIONS):
            raise HTTPException(422, "发布前请为四个维度各配置至少一个分类")
        changed = db.execute(update(TaxonomyVersion).where(TaxonomyVersion.id == version_id,
                                                           TaxonomyVersion.revision == payload.revision,
                                                           TaxonomyVersion.status == "draft")
                             .values(status="published", published_at=utcnow(), revision=TaxonomyVersion.revision + 1)
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback(); raise HTTPException(409, "分类版本已更新，请刷新")
        db.commit(); db.refresh(item)
        logger.info("taxonomy.version.published", extra={
            "event": "taxonomy.version.published", "project_id": project_id,
            "actor_id": user.id, "taxonomy_version_id": item.id,
            "version": item.number, "revision": item.revision,
            "component": "taxonomy",
        })
        return version_dict(item)

    @router.post("/suggestions/rules", dependencies=[Depends(csrf)])
    def generate_rules(project_id: int, payload: CaseIds, user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        if len(set(payload.case_ids)) != len(payload.case_ids):
            raise HTTPException(422, "用例不可重复选择")
        cases = [find_case(project_id, case_id, db) for case_id in payload.case_ids]
        version = published(project_id, db)
        rows = []
        for case in cases:
            previous = db.scalar(select(CaseSuggestion).where(CaseSuggestion.case_id == case.id,
                                                               CaseSuggestion.case_version == case.version,
                                                               CaseSuggestion.source == "rule",
                                                               CaseSuggestion.taxonomy_version_id == (version.id if version else None),
                                                               CaseSuggestion.status == "pending").limit(1))
            if previous:
                rows.append(previous)
                continue
            headers = db.get(ImportBatch, case.import_id).headers
            candidate = classification.rule_candidate(case, headers, version.definitions if version else None)
            item = CaseSuggestion(project_id=project_id, case_id=case.id, taxonomy_version_id=version.id if version else None,
                                  source="rule", status="pending", case_version=case.version, created_by=user.id, **candidate)
            db.add(item); rows.append(item)
        db.commit()
        logger.info("suggestions.rules.generated", extra={
            "event": "suggestions.rules.generated", "project_id": project_id,
            "actor_id": user.id, "taxonomy_version_id": version.id if version else None,
            "count": len(rows), "component": "suggestions",
        })
        return {"count": len(rows), "suggestions": [suggestion_dict(item, db.get(Case, item.case_id).version,
                 version.definitions if version else None) for item in rows]}

    @router.post("/suggestions/model", dependencies=[Depends(csrf)])
    def generate_model(project_id: int, payload: ModelRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        if payload.provider == "codex":
            payload.provider = "codex_cli"
        if payload.provider == "claude":
            payload.provider = "claude_code"
        if payload.provider == "local" and not ai_engine.LOCAL_ENABLED:
            raise HTTPException(403, "本地模型未由管理员启用")
        if payload.provider == "remote" and (user.role != "admin" or not ai_engine.REMOTE_ENABLED):
            raise HTTPException(403, "远程模型未由管理员启用")
        if payload.provider == "codex_cli" and not ai_engine.CODEX_ENABLED:
            raise HTTPException(403, "Codex CLI 未由管理员启用")
        if payload.provider == "claude_code" and not ai_engine.CLAUDE_ENABLED:
            raise HTTPException(403, "Claude Code 未由管理员启用")
        if len(set(payload.case_ids)) != len(payload.case_ids):
            raise HTTPException(422, "用例不可重复选择")
        for case_id in payload.case_ids:
            find_case(project_id, case_id, db)
        version = published(project_id, db)
        job = SuggestionJob(id=uuid.uuid4().hex, project_id=project_id, created_by=user.id,
                            taxonomy_version_id=version.id if version else None,
                            provider=payload.provider, status="queued", case_ids=payload.case_ids,
                            total=len(payload.case_ids), completed=0, errors=[], request_id=current_request_id())
        db.add(job); db.commit()
        if suggestion_jobs.enqueue(job.id, current_request_id()) is False:
            raise HTTPException(503, "AI 任务队列已满，请稍后重试", headers={"Retry-After": "5"})
        logger.info("suggestion_job.enqueued", extra={
            "event": "suggestion_job.enqueued", "job_id": job.id, "project_id": project_id,
            "kind": "suggestion", "provider": job.provider, "actor_id": user.id,
            "total": job.total,
        })
        return job_dict(job)

    @router.get("/suggestion-jobs")
    def list_jobs(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        items = db.scalars(select(SuggestionJob).where(SuggestionJob.project_id == project_id)
                           .order_by(SuggestionJob.created_at.desc()).limit(20)).all()
        return [job_dict(item) for item in items]

    @router.get("/suggestion-jobs/{job_id}")
    def get_job(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        item = db.get(SuggestionJob, job_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "建议任务不存在")
        return job_dict(item)

    @router.post("/suggestion-jobs/{job_id}/retry", dependencies=[Depends(csrf)])
    def retry_job(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        item = db.get(SuggestionJob, job_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "建议任务不存在")
        if item.status not in ("failed", "partial"):
            raise HTTPException(409, "只能重试失败或部分失败的任务")
        item.status = "queued"
        item.lease_owner = ""; item.lease_until = 0.0; item.lease_heartbeat = 0.0
        db.commit()
        if suggestion_jobs.enqueue(item.id, current_request_id()) is False:
            raise HTTPException(503, "AI 任务队列已满，请稍后重试", headers={"Retry-After": "5"})
        logger.info("suggestion_job.requeued", extra={
            "event": "suggestion_job.requeued", "job_id": item.id, "project_id": project_id,
            "kind": "suggestion", "provider": item.provider, "actor_id": user.id,
        })
        return job_dict(item)

    @router.get("/cases/{case_id}/suggestions")
    def case_suggestions(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        case = find_case(project_id, case_id, db)
        items = db.scalars(select(CaseSuggestion).where(CaseSuggestion.case_id == case_id)
                           .order_by(CaseSuggestion.id.desc()).limit(30)).all()
        versions = {item.taxonomy_version_id for item in items if item.taxonomy_version_id is not None}
        definitions = {version.id: version.definitions for version in db.scalars(select(TaxonomyVersion).where(TaxonomyVersion.id.in_(versions)))}
        return [suggestion_dict(item, case.version, definitions.get(item.taxonomy_version_id)) for item in items]

    @router.post("/suggestions/{suggestion_id}/decision", dependencies=[Depends(csrf)])
    def decide(project_id: int, suggestion_id: int, payload: SuggestionDecision,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        item = db.get(CaseSuggestion, suggestion_id)
        if not item or item.project_id != project_id:
            raise HTTPException(404, "建议不存在")
        case = find_case(project_id, item.case_id, db)
        role = project_role(project_id, user, db)
        if role == "reviewer" and case.assignee_id not in (None, user.id):
            raise HTTPException(403, "用例已分派给其他成员")
        if item.status != "pending" or case.version != item.case_version or payload.case_version != case.version:
            raise HTTPException(409, "建议或用例已更新，请刷新")
        reason = payload.reason.strip()
        if not reason:
            raise HTTPException(422, "请填写决策理由")
        now = utcnow()
        if payload.action == "accept":
            version = db.get(TaxonomyVersion, item.taxonomy_version_id) if item.taxonomy_version_id else None
            choice = payload.choice.model_dump() if payload.choice else {"categories": item.categories,
                                                                         "level": item.level, "smoke": item.smoke}
            try:
                categories = classification.validate_categories(choice["categories"], version.definitions if version else None)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if choice["level"] not in (None, "1", "2", "3", "4") or choice["smoke"] not in (None, "是", "否"):
                raise HTTPException(422, "等级或冒烟候选无效")
            if not any(identifier is not None for identifier in categories.values()) and choice["level"] is None and choice["smoke"] is None:
                raise HTTPException(422, "建议没有可采纳的内容")
            before = {"categories": case.final_categories or {}, "level": case.final_level,
                      "smoke": case.final_smoke, "status": case.review_status}
            final_level = choice["level"] if choice["level"] is not None else case.final_level
            final_smoke = choice["smoke"] if choice["smoke"] is not None else case.final_smoke
            final_status = "reviewed" if final_level in ("1", "2", "3", "4") else case.review_status
            changed = db.execute(update(Case).where(Case.id == case.id, Case.version == case.version,
                                                    Case.project_id == project_id)
                                 .values(final_categories=categories,
                                         final_taxonomy_version_id=version.id if version and any(categories.values()) else None,
                                         final_level=final_level, final_smoke=final_smoke,
                                         review_status=final_status,
                                         reviewer=user.username if final_status == "reviewed" else case.reviewer,
                                         reviewed_at=now if final_status == "reviewed" else case.reviewed_at,
                                         version=Case.version + 1)
                                 .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                db.rollback(); raise HTTPException(409, "用例已被他人修改，请刷新")
            after = {"categories": categories, "level": final_level, "smoke": final_smoke,
                     "status": final_status, "suggestion_id": suggestion_id}
            db.add(ReviewEvent(case_id=case.id, user_id=user.id, action="suggestion",
                               before=before, after=after, reason=reason))
        changed_suggestion = db.execute(update(CaseSuggestion).where(CaseSuggestion.id == suggestion_id,
                                                                     CaseSuggestion.status == "pending")
                                        .values(status="accepted" if payload.action == "accept" else "rejected",
                                                decided_by=user.id, decided_at=now, decision_reason=reason)
                                        .execution_options(synchronize_session=False))
        if changed_suggestion.rowcount != 1:
            db.rollback(); raise HTTPException(409, "建议已被处理，请刷新")
        db.commit()
        db.refresh(case)
        logger.info("suggestion.decided", extra={
            "event": "suggestion.decided", "project_id": project_id,
            "actor_id": user.id, "suggestion_id": suggestion_id,
            "case_id": case.id, "case_version": case.version,
            "action": payload.action, "taxonomy_version_id": item.taxonomy_version_id,
            "component": "suggestions",
        })
        return {"suggestion_id": suggestion_id, "status": "accepted" if payload.action == "accept" else "rejected",
                "case": case_dict(case, case_assignee(case, db))}

    return router
