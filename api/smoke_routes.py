"""Human-confirmed smoke suites and immutable execution snapshots."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from .db import (Case, ExecutionEvent, ExecutionRun, RunCase, SmokeItem,
                 SmokeSuite, User, utcnow)

BUDGET_SECONDS = 30 * 60
MAX_SECONDS = 4 * 60 * 60
RESULTS = ("not_run", "passed", "failed", "blocked", "skipped")


class SuiteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    target_version: str = Field(min_length=1, max_length=80)
    environment: str = Field(min_length=1, max_length=80)
    required_chains: list[str] = Field(default_factory=list, max_length=50)


class SuiteUpdate(SuiteCreate):
    revision: int


class ItemCreate(BaseModel):
    revision: int
    case_id: int
    chain_name: str = Field(min_length=1, max_length=120)
    estimated_seconds: int = Field(ge=1, le=MAX_SECONDS)


class ItemUpdate(BaseModel):
    revision: int
    chain_name: str = Field(min_length=1, max_length=120)
    estimated_seconds: int = Field(ge=1, le=MAX_SECONDS)


class RunStart(BaseModel):
    suite_revision: int


class ResultUpdate(BaseModel):
    version: int
    result: Literal["not_run", "passed", "failed", "blocked", "skipped"]
    actual_seconds: int | None = Field(default=None, ge=0, le=MAX_SECONDS)
    note: str = Field(default="", max_length=2000)


def normalize_chains(raw: list[str]) -> list[str]:
    cleaned = [name.strip() for name in raw]
    if any(not name or len(name) > 120 for name in cleaned):
        raise ValueError("核心链路名称不能为空或超过 120 个字符")
    if len({name.casefold() for name in cleaned}) != len(cleaned):
        raise ValueError("核心链路不能重复")
    return cleaned


def coverage(required: list[str], names: list[str]) -> dict:
    covered = [name for name in required if name in set(names)]
    return {"covered_chains": covered, "uncovered_chains": [name for name in required if name not in covered],
            "covered_count": len(covered), "required_count": len(required)}


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["smoke-execution"])

    def suite_for(project_id: int, suite_id: int, db: Session) -> SmokeSuite:
        suite = db.get(SmokeSuite, suite_id)
        if not suite or suite.project_id != project_id:
            raise HTTPException(404, "冒烟集不存在")
        return suite

    def run_for(project_id: int, run_id: int, db: Session) -> ExecutionRun:
        run = db.get(ExecutionRun, run_id)
        if not run or run.project_id != project_id:
            raise HTTPException(404, "执行记录不存在")
        return run

    def suite_dict(suite: SmokeSuite, db: Session) -> dict:
        rows = db.scalars(select(SmokeItem).where(SmokeItem.suite_id == suite.id).order_by(SmokeItem.id)).all()
        cases = {item.id: item for item in db.scalars(select(Case).where(Case.id.in_([row.case_id for row in rows])))}
        selected = []
        for row in rows:
            case = cases.get(row.case_id)
            selected.append({"id": row.id, "case_id": row.case_id, "case_version": row.case_version,
                             "original_id": case.original_id if case else "", "title": case.title if case else "用例已不存在",
                             "level": case.final_level if case else None,
                             "smoke": case.final_smoke if case else None,
                             "stale": not case or case.project_id != suite.project_id or case.version != row.case_version
                                      or case.review_status != "reviewed" or case.final_smoke != "是",
                             "chain_name": row.chain_name, "estimated_seconds": row.estimated_seconds,
                             "confirmed_at": row.confirmed_at})
        estimate = sum(item["estimated_seconds"] for item in selected)
        return {"id": suite.id, "project_id": suite.project_id, "title": suite.title,
                "target_version": suite.target_version, "environment": suite.environment,
                "required_chains": suite.required_chains, "revision": suite.revision,
                "budget_seconds": BUDGET_SECONDS, "estimated_seconds": estimate,
                "over_budget": estimate > BUDGET_SECONDS, "items": selected,
                "created_at": suite.created_at, "updated_at": suite.updated_at,
                **coverage(suite.required_chains, [item["chain_name"] for item in selected if not item["stale"]])}

    def bump_suite(suite: SmokeSuite, expected: int, db: Session, **values):
        changed = db.execute(update(SmokeSuite).where(SmokeSuite.id == suite.id, SmokeSuite.revision == expected)
                             .values(revision=SmokeSuite.revision + 1, updated_at=utcnow(), **values)
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "冒烟集已被其他成员修改，请刷新")

    @router.get("/smoke-suites")
    def list_suites(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        suites = db.scalars(select(SmokeSuite).where(SmokeSuite.project_id == project_id).order_by(SmokeSuite.id.desc())).all()
        return [suite_dict(suite, db) for suite in suites]

    @router.post("/smoke-suites", dependencies=[Depends(csrf)])
    def create_suite(project_id: int, payload: SuiteCreate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        try:
            chains = normalize_chains(payload.required_chains)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        title, version, environment = payload.title.strip(), payload.target_version.strip(), payload.environment.strip()
        if not all((title, version, environment)):
            raise HTTPException(422, "名称、版本和环境均不能为空")
        suite = SmokeSuite(project_id=project_id, title=title, target_version=version,
                           environment=environment, required_chains=chains, created_by=user.id)
        db.add(suite); db.commit()
        return suite_dict(suite, db)

    @router.get("/smoke-suites/{suite_id}")
    def get_suite(project_id: int, suite_id: int,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        return suite_dict(suite_for(project_id, suite_id, db), db)

    @router.patch("/smoke-suites/{suite_id}", dependencies=[Depends(csrf)])
    def update_suite(project_id: int, suite_id: int, payload: SuiteUpdate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        suite = suite_for(project_id, suite_id, db)
        if suite.revision != payload.revision:
            raise HTTPException(409, "冒烟集已被其他成员修改，请刷新")
        try:
            chains = normalize_chains(payload.required_chains)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        used = {name for (name,) in db.execute(select(SmokeItem.chain_name).where(SmokeItem.suite_id == suite_id))}
        if used - set(chains):
            raise HTTPException(409, "请先移除或调整仍在使用的核心链路")
        title, version, environment = payload.title.strip(), payload.target_version.strip(), payload.environment.strip()
        if not all((title, version, environment)):
            raise HTTPException(422, "名称、版本和环境均不能为空")
        bump_suite(suite, payload.revision, db, title=title, target_version=version,
                   environment=environment, required_chains=chains)
        db.commit(); db.refresh(suite)
        return suite_dict(suite, db)

    @router.get("/smoke-suites/{suite_id}/candidates")
    def candidates(project_id: int, suite_id: int, q: str = "",
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        suite_for(project_id, suite_id, db)
        included = set(db.scalars(select(SmokeItem.case_id).where(SmokeItem.suite_id == suite_id)))
        query = select(Case).where(Case.project_id == project_id, Case.review_status == "reviewed", Case.final_smoke == "是")
        if included:
            query = query.where(Case.id.not_in(included))
        if q.strip():
            query = query.where(Case.title.contains(q.strip()) | Case.original_id.contains(q.strip()) | Case.directory.contains(q.strip()))
        rows = db.scalars(query.order_by(Case.id).limit(50)).all()
        return [{"id": case.id, "original_id": case.original_id, "title": case.title,
                 "directory": case.directory, "level": case.final_level, "version": case.version} for case in rows]

    @router.post("/smoke-suites/{suite_id}/items", dependencies=[Depends(csrf)])
    def add_item(project_id: int, suite_id: int, payload: ItemCreate,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        suite = suite_for(project_id, suite_id, db)
        case = db.get(Case, payload.case_id)
        if not case or case.project_id != project_id:
            raise HTTPException(404, "用例不存在")
        if case.review_status != "reviewed" or case.final_smoke != "是":
            raise HTTPException(422, "需先由评审员将最终冒烟明确审核为“是”")
        chain = payload.chain_name.strip()
        if chain not in suite.required_chains:
            raise HTTPException(422, "请选择本冒烟集已配置的核心链路")
        if db.scalar(select(SmokeItem.id).where(SmokeItem.suite_id == suite_id, SmokeItem.case_id == case.id).limit(1)):
            raise HTTPException(409, "用例已在冒烟集中")
        bump_suite(suite, payload.revision, db)
        db.add(SmokeItem(suite_id=suite_id, case_id=case.id, case_version=case.version,
                         chain_name=chain, estimated_seconds=payload.estimated_seconds, confirmed_by=user.id))
        db.commit(); db.refresh(suite)
        return suite_dict(suite, db)

    @router.put("/smoke-suites/{suite_id}/items/{item_id}", dependencies=[Depends(csrf)])
    def update_item(project_id: int, suite_id: int, item_id: int, payload: ItemUpdate,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        suite = suite_for(project_id, suite_id, db)
        item = db.get(SmokeItem, item_id)
        if not item or item.suite_id != suite_id:
            raise HTTPException(404, "冒烟集用例不存在")
        case = db.get(Case, item.case_id)
        if not case or case.project_id != project_id or case.review_status != "reviewed" or case.final_smoke != "是":
            raise HTTPException(422, "用例最终冒烟已变化，请先重新审核")
        chain = payload.chain_name.strip()
        if chain not in suite.required_chains:
            raise HTTPException(422, "请选择本冒烟集已配置的核心链路")
        bump_suite(suite, payload.revision, db)
        item.chain_name = chain; item.estimated_seconds = payload.estimated_seconds
        item.case_version = case.version; item.confirmed_by = user.id; item.confirmed_at = utcnow()
        db.commit(); db.refresh(suite)
        return suite_dict(suite, db)

    @router.delete("/smoke-suites/{suite_id}/items/{item_id}", dependencies=[Depends(csrf)])
    def remove_item(project_id: int, suite_id: int, item_id: int, revision: int,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        suite = suite_for(project_id, suite_id, db)
        item = db.get(SmokeItem, item_id)
        if not item or item.suite_id != suite_id:
            raise HTTPException(404, "冒烟集用例不存在")
        bump_suite(suite, revision, db)
        db.delete(item); db.commit(); db.refresh(suite)
        return suite_dict(suite, db)

    @router.post("/smoke-suites/{suite_id}/runs", dependencies=[Depends(csrf)])
    def start_run(project_id: int, suite_id: int, payload: RunStart,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        suite = suite_for(project_id, suite_id, db)
        if suite.revision != payload.suite_revision:
            raise HTTPException(409, "冒烟集已被修改，请刷新")
        if not suite.required_chains:
            raise HTTPException(422, "请先配置核心链路")
        locked = db.execute(update(SmokeSuite).where(SmokeSuite.id == suite_id,
                                                    SmokeSuite.project_id == project_id,
                                                    SmokeSuite.revision == payload.suite_revision)
                            .values(updated_at=SmokeSuite.updated_at)
                            .execution_options(synchronize_session=False))
        if locked.rowcount != 1:
            db.rollback(); raise HTTPException(409, "冒烟集已被修改，请刷新")
        rows = db.scalars(select(SmokeItem).where(SmokeItem.suite_id == suite_id).order_by(SmokeItem.id)).all()
        if not rows:
            raise HTTPException(422, "冒烟集尚无确认用例")
        cases = {case.id: case for case in db.scalars(select(Case).where(Case.id.in_([row.case_id for row in rows])))}
        if any(not cases.get(row.case_id) or cases[row.case_id].project_id != project_id
               or cases[row.case_id].version != row.case_version
               or cases[row.case_id].review_status != "reviewed" or cases[row.case_id].final_smoke != "是" for row in rows):
            raise HTTPException(409, "冒烟集含需重新确认的用例，请核对后再开始")
        run = ExecutionRun(project_id=project_id, suite_id=suite_id, suite_revision=suite.revision,
                           title=suite.title, target_version=suite.target_version,
                           environment=suite.environment, required_chains=list(suite.required_chains),
                           status="active", started_by=user.id)
        db.add(run); db.flush()
        for row in rows:
            case = cases[row.case_id]
            db.add(RunCase(run_id=run.id, case_id=case.id, case_version=case.version,
                           original_id=case.original_id, title=case.title, level=case.final_level,
                           smoke=case.final_smoke, chain_name=row.chain_name,
                           estimated_seconds=row.estimated_seconds, result="not_run", note="", version=1))
        db.commit()
        return run_dict(run, db)

    def run_dict(run: ExecutionRun, db: Session, include_detail: bool = True) -> dict:
        rows = db.scalars(select(RunCase).where(RunCase.run_id == run.id).order_by(RunCase.id)).all()
        people = {item.id: item.username for item in db.scalars(select(User).where(User.id.in_(
            {uid for uid in [run.started_by, run.completed_by, *[row.executed_by for row in rows]] if uid is not None})))}
        events = db.scalars(select(ExecutionEvent).where(ExecutionEvent.run_case_id.in_([row.id for row in rows]))
                            .order_by(ExecutionEvent.id.desc()).limit(100)).all() if rows and include_detail else []
        summary = {key: sum(row.result == key for row in rows) for key in RESULTS}
        estimate = sum(row.estimated_seconds for row in rows)
        return {"id": run.id, "project_id": run.project_id, "suite_id": run.suite_id,
                "suite_revision": run.suite_revision, "title": run.title,
                "target_version": run.target_version, "environment": run.environment,
                "required_chains": run.required_chains, "status": run.status,
                "started_by": people.get(run.started_by, "未知账号"), "started_at": run.started_at,
                "completed_by": people.get(run.completed_by) if run.completed_by else None,
                "completed_at": run.completed_at, "budget_seconds": BUDGET_SECONDS,
                "estimated_seconds": estimate, "over_budget": estimate > BUDGET_SECONDS,
                "actual_seconds": sum(row.actual_seconds or 0 for row in rows), "summary": summary,
                "item_count": len(rows),
                "items": [{"id": row.id, "case_id": row.case_id, "case_version": row.case_version,
                           "original_id": row.original_id, "title": row.title, "level": row.level,
                           "smoke": row.smoke, "chain_name": row.chain_name,
                           "estimated_seconds": row.estimated_seconds, "result": row.result,
                           "actual_seconds": row.actual_seconds, "note": row.note,
                           "executed_by": people.get(row.executed_by) if row.executed_by else None,
                           "executed_at": row.executed_at, "version": row.version} for row in rows] if include_detail else [],
                "events": [{"id": event.id, "run_case_id": event.run_case_id,
                            "actor": people.get(event.user_id) or db.get(User, event.user_id).username,
                            "before": event.before, "after": event.after,
                            "created_at": event.created_at} for event in events],
                **coverage(run.required_chains, [row.chain_name for row in rows])}

    @router.get("/execution-runs")
    def list_runs(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        rows = db.scalars(select(ExecutionRun).where(ExecutionRun.project_id == project_id)
                          .order_by(ExecutionRun.id.desc()).limit(50)).all()
        return [run_dict(row, db, include_detail=False) for row in rows]

    @router.get("/execution-runs/{run_id}")
    def get_run(project_id: int, run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        return run_dict(run_for(project_id, run_id, db), db)

    @router.put("/execution-runs/{run_id}/cases/{run_case_id}", dependencies=[Depends(csrf)])
    def update_result(project_id: int, run_id: int, run_case_id: int, payload: ResultUpdate,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        run = run_for(project_id, run_id, db)
        if run.status != "active":
            raise HTTPException(409, "执行记录已结束")
        item = db.get(RunCase, run_case_id)
        if not item or item.run_id != run_id:
            raise HTTPException(404, "执行用例不存在")
        note = payload.note.strip()
        if payload.result in ("failed", "blocked", "skipped") and not note:
            raise HTTPException(422, "失败、阻塞或跳过必须填写说明")
        if item.version != payload.version:
            raise HTTPException(409, "执行结果已被其他成员修改，请刷新")
        before = {"result": item.result, "actual_seconds": item.actual_seconds, "note": item.note}
        after = {"result": payload.result, "actual_seconds": payload.actual_seconds, "note": note}
        if before == after:
            return run_dict(run, db)
        changed = db.execute(update(RunCase).where(RunCase.id == run_case_id, RunCase.run_id == run_id,
                                                    RunCase.version == payload.version,
                                                    exists(select(ExecutionRun.id).where(ExecutionRun.id == run_id,
                                                           ExecutionRun.status == "active")))
                             .values(result=payload.result, actual_seconds=payload.actual_seconds,
                                     note=note, executed_by=user.id, executed_at=utcnow(),
                                     version=RunCase.version + 1)
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback(); raise HTTPException(409, "执行结果已被其他成员修改，请刷新")
        db.add(ExecutionEvent(run_case_id=run_case_id, user_id=user.id, before=before, after=after))
        db.commit(); db.refresh(item)
        return run_dict(run, db)

    @router.post("/execution-runs/{run_id}/complete", dependencies=[Depends(csrf)])
    def complete_run(project_id: int, run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        writable(user, project_id, db)
        run = run_for(project_id, run_id, db)
        if run.status != "active":
            raise HTTPException(409, "执行记录已结束")
        if db.scalar(select(RunCase.id).where(RunCase.run_id == run_id, RunCase.result == "not_run").limit(1)):
            raise HTTPException(409, "仍有未执行用例，请完成或标记跳过")
        changed = db.execute(update(ExecutionRun).where(ExecutionRun.id == run_id, ExecutionRun.status == "active",
                                                       ~exists(select(RunCase.id).where(RunCase.run_id == run_id,
                                                                                      RunCase.result == "not_run")))
                             .values(status="completed", completed_by=user.id, completed_at=utcnow())
                             .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            db.rollback(); raise HTTPException(409, "执行记录已被其他成员结束")
        db.commit(); db.refresh(run)
        return run_dict(run, db)

    return router
