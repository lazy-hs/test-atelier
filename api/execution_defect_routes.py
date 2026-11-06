"""Audited human defects linked to immutable execution report case IDs."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .db import ExecutionDefect, ExecutionDefectEvent, ExecutionReport, User, utcnow
from .execution_report_core import report_integrity_ok


class DefectCreate(BaseModel):
    run_id: int = Field(ge=1)
    run_case_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=5000)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class DefectUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=5000)
    severity: Literal["low", "medium", "high", "critical"]
    status: Literal["open", "in_progress", "resolved", "closed"]
    resolution: str = Field(default="", max_length=3000)


def _snapshot(project_id: int, run_id: int, db: Session) -> ExecutionReport:
    report = db.get(ExecutionReport, run_id)
    if not report or report.project_id != project_id:
        raise HTTPException(404, "已完成的执行报告不存在")
    if not report_integrity_ok(report):
        raise HTTPException(503, "执行报告完整性校验失败")
    return report


def _case(report: ExecutionReport, run_case_id: int) -> dict:
    for item in report.payload["cases"]:
        if item["run_case_id"] == run_case_id:
            return item
    raise HTTPException(422, "执行用例不在该报告的冻结快照中")


def _fields(defect: ExecutionDefect) -> dict:
    return {"title": defect.title, "description": defect.description, "severity": defect.severity,
            "status": defect.status, "resolution": defect.resolution}


def _item(defect: ExecutionDefect, report: ExecutionReport) -> dict:
    source = _case(report, defect.run_case_id)
    return {"id": defect.id, "project_id": defect.project_id, "run_id": defect.run_id,
            "run_case_id": defect.run_case_id, "source_original_id": source["original_id"],
            "source_title": source["title"], "source_result": source["result"],
            "source_case_version": source["case_version"], "report_completeness": report.completeness,
            **_fields(defect), "version": defect.version, "created_by_id": defect.created_by,
            "updated_by_id": defect.updated_by, "created_at": defect.created_at,
            "updated_at": defect.updated_at}


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["execution-defects"])

    @router.get("/execution-defects/candidates")
    def candidates(project_id: int, run_id: int = Query(ge=1),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        report = _snapshot(project_id, run_id, db)
        return {"report_completeness": report.completeness,
                "items": [{"run_case_id": item["run_case_id"], "original_id": item["original_id"],
                           "title": item["title"], "result": item["result"],
                           "case_version": item["case_version"]}
                          for item in report.payload["cases"] if item["result"] in ("failed", "blocked")]}

    @router.get("/execution-defects")
    def list_defects(project_id: int, run_id: int = Query(ge=1),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        report = _snapshot(project_id, run_id, db)
        rows = db.scalars(select(ExecutionDefect).where(ExecutionDefect.project_id == project_id,
                                                        ExecutionDefect.run_id == run_id)
                          .order_by(ExecutionDefect.id.desc())).all()
        return [_item(row, report) for row in rows]

    @router.post("/execution-defects", dependencies=[Depends(csrf)])
    def create_defect(project_id: int, payload: DefectCreate,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db, fence=True)
        report = _snapshot(project_id, payload.run_id, db)
        source = _case(report, payload.run_case_id)
        if source["result"] not in ("failed", "blocked"):
            raise HTTPException(422, "仅失败或阻塞的执行用例可关联缺陷")
        title = payload.title.strip()
        if not title:
            raise HTTPException(422, "缺陷标题不能为空")
        if (db.scalar(select(func.count(ExecutionDefect.id)).where(ExecutionDefect.run_id == payload.run_id)) or 0) >= 500:
            raise HTTPException(409, "单次执行的缺陷数量已达上限")
        defect = ExecutionDefect(project_id=project_id, run_id=payload.run_id, run_case_id=payload.run_case_id,
                                 title=title, description=payload.description.strip(), severity=payload.severity,
                                 status="open", resolution="", version=1,
                                 created_by=user.id, updated_by=user.id)
        db.add(defect); db.flush()
        db.add(ExecutionDefectEvent(defect_id=defect.id, actor_id=user.id, before={}, after=_fields(defect)))
        db.commit(); db.refresh(defect)
        return _item(defect, report)

    @router.patch("/execution-defects/{defect_id}", dependencies=[Depends(csrf)])
    def update_defect(project_id: int, defect_id: int, payload: DefectUpdate,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db, fence=True)
        defect = db.get(ExecutionDefect, defect_id)
        if not defect or defect.project_id != project_id:
            raise HTTPException(404, "缺陷不存在")
        report = _snapshot(project_id, defect.run_id, db)
        title = payload.title.strip()
        resolution = payload.resolution.strip()
        if not title or (payload.status in ("resolved", "closed") and not resolution):
            raise HTTPException(422, "缺陷标题及已解决缺陷的处理结论不能为空")
        if defect.version != payload.version:
            raise HTTPException(409, "缺陷已被其他成员修改，请刷新")
        before = _fields(defect)
        after = {"title": title, "description": payload.description.strip(), "severity": payload.severity,
                 "status": payload.status, "resolution": resolution}
        if before == after:
            return _item(defect, report)
        changed = db.execute(update(ExecutionDefect).where(ExecutionDefect.id == defect_id,
                                                           ExecutionDefect.project_id == project_id,
                                                           ExecutionDefect.version == payload.version)
                             .values(**after, version=ExecutionDefect.version + 1,
                                     updated_by=user.id, updated_at=utcnow())
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback(); raise HTTPException(409, "缺陷已被其他成员修改，请刷新")
        db.add(ExecutionDefectEvent(defect_id=defect_id, actor_id=user.id, before=before, after=after))
        db.commit(); db.refresh(defect)
        return _item(defect, report)

    @router.get("/execution-defects/{defect_id}/events")
    def events(project_id: int, defect_id: int,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        defect = db.get(ExecutionDefect, defect_id)
        if not defect or defect.project_id != project_id:
            raise HTTPException(404, "缺陷不存在")
        _snapshot(project_id, defect.run_id, db)
        rows = db.scalars(select(ExecutionDefectEvent).where(ExecutionDefectEvent.defect_id == defect_id)
                          .order_by(ExecutionDefectEvent.id)).all()
        return [{"id": row.id, "actor_id": row.actor_id, "before": row.before, "after": row.after,
                 "created_at": row.created_at} for row in rows]

    return router
