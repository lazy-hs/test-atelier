"""Curated, versioned knowledge referencing project-owned artifacts."""

import hashlib
import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .db import (AnalysisRun, Case, ConversionRecord, ExecutionDefect, HarnessTask,
                 KnowledgeEntry, KnowledgeRevision, SourceDocument, User, utcnow)
from .execution_defect_routes import _snapshot
from .harness_contracts import (validate_requirement_analysis, validate_skill_execution,
                                validate_test_cases)


SourceType = Literal["document", "analysis", "harness_task", "case", "conversion", "execution_report", "defect"]
Status = Literal["active", "archived"]
TASK_RESULT_SCHEMAS = {"requirement-analysis": "requirement-analysis.v1",
                       "test-cases": "test-cases.v1", "skill-execution": "skill-execution.v1"}


class KnowledgeCreate(BaseModel):
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)


class KnowledgeUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)
    status: Status


def _numeric_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise HTTPException(422, "来源 ID 必须为正整数")
    return int(value)


def _snapshot_digest(result: dict) -> str:
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(409, "来源结果无法建立摘要") from exc
    return hashlib.sha256(encoded).hexdigest()


def _source(project_id: int, kind: SourceType, value: str, db: Session) -> tuple[str, dict]:
    """Resolve authorization through the requested project, never another project's object."""
    if kind == "execution_report":
        identifier = _numeric_id(value)
        report = _snapshot(project_id, identifier, db)
        source = report.payload["source"]
        return str(identifier), {"title": source["title"], "version": source["suite_revision"],
                                 "run_id": identifier, "sha256": report.payload_sha256,
                                 "completeness": report.completeness}
    if kind in ("case", "defect"):
        identifier = _numeric_id(value)
        row = db.get(Case if kind == "case" else ExecutionDefect, identifier)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "项目内来源产物不存在")
        if kind == "case":
            return str(identifier), {"title": row.title[:160], "version": row.version,
                                     "original_id": row.original_id}
        report = _snapshot(project_id, row.run_id, db)
        return str(identifier), {"title": row.title, "version": row.version,
                                 "run_id": row.run_id, "run_case_id": row.run_case_id,
                                 "report_sha256": report.payload_sha256}
    model = {"document": SourceDocument, "analysis": AnalysisRun,
             "harness_task": HarnessTask, "conversion": ConversionRecord}[kind]
    row = db.get(model, value)
    if not row or row.project_id != project_id:
        raise HTTPException(404, "项目内来源产物不存在")
    if kind == "document":
        snapshot = {"title": row.filename, "version": 1, "source_integrity": "unrecorded"}
        if row.source_sha256 is not None or row.source_size is not None:
            if (not isinstance(row.source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", row.source_sha256)
                    or type(row.source_size) is not int or row.source_size < 0):
                raise HTTPException(409, "需求材料原件标识不完整")
            snapshot.update(source_integrity="recorded", source_sha256=row.source_sha256,
                            source_size=row.source_size)
        return value, snapshot
    if kind == "conversion":
        snapshot = {"title": row.output_filename, "version": 1,
                    "source_filename": row.source_filename, "output_integrity": "unrecorded"}
        if row.output_sha256 is not None:
            if (not isinstance(row.output_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", row.output_sha256)
                    or type(row.file_size) is not int or row.file_size < 0):
                raise HTTPException(409, "转换产物原件标识不完整")
            snapshot.update(output_integrity="recorded", output_sha256=row.output_sha256,
                            output_size=row.file_size)
        return value, snapshot
    if row.status != ("ready" if kind == "analysis" else "completed"):
        raise HTTPException(409, "仅已完成的分析或 Harness 任务可用于知识条目")
    if kind == "analysis":
        result = row.result
        if (not isinstance(result, dict) or not isinstance(row.source_ids, list)
                or any(not isinstance(source, str) for source in row.source_ids)):
            raise HTTPException(409, "需求分析结果缺失或来源无效")
        raw = result.get("harness_result")
        if row.provider == "harness":
            if (not isinstance(raw, dict) or raw.get("schema") != "requirement-analysis.v1"
                    or not isinstance(raw.get("task_id"), str) or not raw["task_id"]
                    or not isinstance(raw.get("data"), dict)
                    or not isinstance(raw.get("source_document_ids"), list)
                    or len(raw["source_document_ids"]) != len(row.source_ids)
                    or any(not isinstance(source, str) for source in raw["source_document_ids"])
                    or set(raw["source_document_ids"]) != set(row.source_ids)
                    or result.get("report_markdown") != raw.get("report_markdown")):
                raise HTTPException(409, "Harness 分析缺少可追溯的原始结果")
            task = db.get(HarnessTask, raw["task_id"])
            if (not task or task.project_id != project_id or task.analysis_run_id != row.id
                    or task.task_type != "requirement-analysis" or task.status != "completed"
                    or task.skill_id != "requirement-analysis"
                    or task.task_version != "v1"
                    or task.result_schema != "requirement-analysis.v1"
                    or not isinstance(raw.get("result_id"), str) or not raw["result_id"]
                    or not isinstance(raw.get("session_id"), str) or not raw["session_id"]
                    or raw["session_id"] == f"ta-{task.id}"
                    or not isinstance(task.session_ids, list)
                    or any(not isinstance(session, str) for session in task.session_ids)
                    or raw["session_id"] not in [task.session_id, *task.session_ids]
                    or not isinstance(task.source_document_ids, list)
                    or len(task.source_document_ids) != len(row.source_ids)
                    or any(not isinstance(source, str) for source in task.source_document_ids)
                    or set(task.source_document_ids) != set(row.source_ids)
                    or task.result != raw):
                raise HTTPException(409, "Harness 分析原始结果与任务归属不一致")
            try:
                validate_requirement_analysis(raw, set(row.source_ids))
            except (ValueError, TypeError) as exc:
                raise HTTPException(409, "Harness 分析原始结果不符合交付 Schema") from exc
            content = raw
        else:
            content = result
        requirements, ambiguities = content.get("requirements"), content.get("ambiguities")
        if row.provider == "harness":
            requirements = content["data"].get("requirements")
            ambiguities = content["data"].get("ambiguities")
        report = content.get("report_markdown")
        if (not isinstance(requirements, list) or not requirements) and (
                not isinstance(ambiguities, list) or not ambiguities) and (
                not isinstance(report, str) or not report.strip()):
            raise HTTPException(409, "需求分析结果没有可引用的内容")
        snapshot = {"title": "需求分析", "version": row.version, "source_document_ids": row.source_ids,
                    "provider": row.provider, "analysis_result_snapshot_sha256": _snapshot_digest(result)}
        if row.provider == "harness":
            snapshot["harness_task_id"] = raw["task_id"]
        return value, snapshot
    result = row.result
    sources = result.get("source_document_ids") if isinstance(result, dict) else None
    expected_sources = row.source_document_ids or []
    sessions = row.session_ids or []
    if (not isinstance(result, dict) or not isinstance(result.get("result_id"), str)
            or not 0 < len(result["result_id"]) <= 128 or result.get("task_id") != row.id
            or row.task_version != "v1"
            or row.result_schema != TASK_RESULT_SCHEMAS.get(row.task_type)
            or result.get("schema") != row.result_schema
            or not isinstance(result.get("session_id"), str) or not result["session_id"]
            or result["session_id"] == f"ta-{row.id}"
            or not isinstance(sessions, list) or any(not isinstance(session, str) for session in sessions)
            or result["session_id"] not in [row.session_id, *sessions]
            or not isinstance(expected_sources, list)
            or any(not isinstance(source, str) for source in expected_sources)
            or not isinstance(sources, list) or len(sources) != len(expected_sources)
            or any(not isinstance(source, str) for source in sources)
            or set(sources) != set(expected_sources)
            or not isinstance(result.get("data"), dict)
            or row.task_type == "skill-execution" and result.get("skill_id") != row.skill_id
            or row.task_type == "test-cases" and result.get("analysis_run_id") != row.analysis_run_id):
        raise HTTPException(409, "Harness 原始结果缺失或与任务不匹配")
    try:
        source_ids = set(expected_sources)
        if row.task_type == "requirement-analysis":
            validate_requirement_analysis(result, source_ids)
            analysis = db.get(AnalysisRun, row.analysis_run_id)
            if (not analysis or analysis.project_id != project_id or analysis.provider != "harness"
                    or analysis.status != "ready" or not isinstance(analysis.result, dict)
                    or analysis.result.get("harness_result") != result):
                raise HTTPException(409, "Harness 分析任务与业务分析归属不一致")
        elif row.task_type == "skill-execution":
            validate_skill_execution(result, source_ids, row.skill_id)
        else:
            analysis = db.get(AnalysisRun, row.analysis_run_id)
            if (not analysis or analysis.project_id != project_id or analysis.provider != "harness"
                    or not isinstance(analysis.source_ids, list)
                    or len(analysis.source_ids) != len(expected_sources)
                    or set(analysis.source_ids) != source_ids
                    or not isinstance(analysis.result, dict)
                    or not isinstance(analysis.result.get("harness_result"), dict)):
                raise HTTPException(409, "用例来源分析缺失或与任务不匹配")
            original = analysis.result["harness_result"]
            validate_requirement_analysis(original, source_ids)
            requirements = {item["id"] for item in original["data"]["requirements"]}
            criteria = {criterion["id"]: item["id"]
                        for item in original["data"]["requirements"]
                        for criterion in item.get("acceptance_criteria", [])}
            validate_test_cases(result, source_ids, requirements, criteria)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(409, "Harness 原始结果不符合交付 Schema 或来源追溯") from exc
    snapshot = {"title": row.skill_id, "version": row.task_version,
                "task_type": row.task_type, "result_schema": row.result_schema,
                "session_id": result["session_id"], "result_id": result["result_id"],
                "result_snapshot_sha256": _snapshot_digest(result)}
    if row.task_type == "test-cases":
        snapshot.update(analysis_run_id=row.analysis_run_id,
                        source_analysis_snapshot_sha256=_snapshot_digest(original))
    return value, snapshot


def _item(row: KnowledgeEntry) -> dict:
    return {"id": row.id, "project_id": row.project_id, "source_type": row.source_type,
            "source_id": row.source_id, "source_snapshot": row.source_snapshot,
            "title": row.title, "body": row.body, "status": row.status, "version": row.version,
            "created_by_id": row.created_by, "updated_by_id": row.updated_by,
            "created_at": row.created_at, "updated_at": row.updated_at}


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/knowledge", tags=["knowledge"])

    @router.get("")
    def list_entries(project_id: int, status: Literal["all", "active", "archived"] = "active",
                     before_id: int | None = Query(default=None, ge=1),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        query = select(KnowledgeEntry).where(KnowledgeEntry.project_id == project_id)
        if status != "all":
            query = query.where(KnowledgeEntry.status == status)
        if before_id is not None:
            query = query.where(KnowledgeEntry.id < before_id)
        rows = db.scalars(query.order_by(KnowledgeEntry.id.desc()).limit(51)).all()
        return {"items": [_item(row) for row in rows[:50]],
                "next_before_id": rows[49].id if len(rows) > 50 else None}

    @router.get("/{entry_id}")
    def get_entry(project_id: int, entry_id: int,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        row = db.get(KnowledgeEntry, entry_id)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "项目内知识条目不存在")
        return _item(row)

    @router.get("/{entry_id}/revisions")
    def revisions(project_id: int, entry_id: int,
                  before_version: int | None = Query(default=None, ge=1),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        row = db.get(KnowledgeEntry, entry_id)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "项目内知识条目不存在")
        query = select(KnowledgeRevision).where(KnowledgeRevision.entry_id == entry_id)
        if before_version is not None:
            query = query.where(KnowledgeRevision.version < before_version)
        history = db.scalars(query.order_by(KnowledgeRevision.version.desc()).limit(51)).all()
        return {"items": [{"version": item.version, "actor_id": item.actor_id, "title": item.title,
                           "body": item.body, "status": item.status, "created_at": item.created_at}
                          for item in history[:50]],
                "next_before_version": history[49].version if len(history) > 50 else None}

    @router.post("", dependencies=[Depends(csrf)])
    def create_entry(project_id: int, payload: KnowledgeCreate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db, fence=True)
        title, body = payload.title.strip(), payload.body.strip()
        if not title or not body:
            raise HTTPException(422, "标题和正文不能为空")
        if (db.scalar(select(func.count(KnowledgeEntry.id)).where(KnowledgeEntry.project_id == project_id)) or 0) >= 5000:
            raise HTTPException(409, "项目知识条目数量已达上限")
        source_id, snapshot = _source(project_id, payload.source_type, payload.source_id.strip(), db)
        now = utcnow()
        row = KnowledgeEntry(project_id=project_id, source_type=payload.source_type, source_id=source_id,
                             source_snapshot=snapshot, title=title, body=body, status="active", version=1,
                             created_by=user.id, updated_by=user.id, created_at=now, updated_at=now)
        db.add(row); db.flush()
        db.add(KnowledgeRevision(entry_id=row.id, version=1, actor_id=user.id,
                                 title=title, body=body, status="active", created_at=now))
        db.commit(); db.refresh(row)
        return _item(row)

    @router.patch("/{entry_id}", dependencies=[Depends(csrf)])
    def update_entry(project_id: int, entry_id: int, payload: KnowledgeUpdate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db, fence=True)
        row = db.get(KnowledgeEntry, entry_id)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "项目内知识条目不存在")
        title, body = payload.title.strip(), payload.body.strip()
        if not title or not body:
            raise HTTPException(422, "标题和正文不能为空")
        if row.version != payload.version:
            raise HTTPException(409, "知识条目已被其他成员修改，请重新加载")
        if (row.title, row.body, row.status) == (title, body, payload.status):
            return _item(row)
        now = utcnow()
        changed = db.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == entry_id,
                                                           KnowledgeEntry.project_id == project_id,
                                                           KnowledgeEntry.version == payload.version)
                             .values(title=title, body=body, status=payload.status,
                                     version=KnowledgeEntry.version + 1,
                                     updated_by=user.id, updated_at=now)
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "知识条目已被其他成员修改，请重新加载")
        db.add(KnowledgeRevision(entry_id=entry_id, version=payload.version + 1, actor_id=user.id,
                                 title=title, body=body, status=payload.status, created_at=now))
        db.commit(); db.refresh(row)
        return _item(row)

    return router
