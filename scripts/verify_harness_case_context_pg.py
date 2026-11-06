"""Exercise the case-result/analysis-decision race with real PostgreSQL locks."""

import os
import threading
import uuid

from sqlalchemy import delete, event, select, text

from api.db import (AnalysisRun, CaseDraft, HarnessTask, HarnessTaskEvent,
                    Project, SessionLocal, User, engine)
from api.harness_case_context import AnalysisContextChanged, case_analysis_context, case_context_sha256
from api.harness_projection import project_event, project_invalid_result


EXPECTED_HEAD = "20261047_auth_session_revocations"


def verify() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("case-context smoke requires PostgreSQL")
    if os.getenv("TEST_ATELIER_PG_SMOKE") != "1":
        raise RuntimeError("case-context smoke requires explicit TEST_ATELIER_PG_SMOKE=1")
    with SessionLocal() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        if head != EXPECTED_HEAD:
            raise RuntimeError(f"unexpected migration head: {head}")
    exercise()


def exercise() -> None:
    task_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    source_id = uuid.uuid4().hex
    session_id = f"case-context-{uuid.uuid4().hex}"
    project_id = None
    with SessionLocal.begin() as db:
        user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
        if user_id is None:
            raise RuntimeError("case-context smoke requires a seeded user")
        project = Project(name="ci-harness-case-context")
        db.add(project)
        db.flush()
        project_id = project.id

    try:
        original = {
            "schema": "requirement-analysis.v1", "result_id": "analysis-result",
            "task_id": "source-task", "session_id": "source-session",
            "source_document_ids": [source_id], "generated_at": "2026-10-26T10:00:00Z",
            "warnings": [], "data": {"requirements": [{
                "id": "REQ-1", "title": "Login", "description": "Valid login", "priority": "high",
                "source_refs": [{"document_id": source_id, "locator": "body"}],
                "acceptance_criteria": [],
            }], "ambiguities": [], "quality_warnings": []},
        }
        run_result = {"harness_result": original, "ambiguities": [{
            "id": "AMB-1", "description": "Login policy", "critical": False, "decision": "initial",
        }]}
        context = case_analysis_context(run_id, 1, run_result)
        with SessionLocal.begin() as db:
            db.add(AnalysisRun(id=run_id, project_id=project_id, user_id=user_id,
                               provider="harness", status="ready", source_ids=[source_id], result=run_result))
            db.add(HarnessTask(id=task_id, project_id=project_id, user_id=user_id,
                               task_type="test-cases", task_version="v1", result_schema="test-cases.v1",
                               skill_id="test-cases", source_document_ids=[source_id], analysis_run_id=run_id,
                               session_id=session_id, session_ids=[session_id], status="running",
                               idempotency_key=task_id, sequence=1, result={}, error={}))
            db.add(HarnessTaskEvent(task_id=task_id, sequence=1, event="task.created", payload={
                "task_id": task_id, "sequence": 1, "analysis_version": 1,
                "analysis_sha256": case_context_sha256(context),
            }))

        result = {
            "schema": "test-cases.v1", "result_id": "staged-result", "task_id": task_id,
            "session_id": session_id, "source_document_ids": [source_id], "analysis_run_id": run_id,
            "generated_at": "2026-10-26T10:01:00Z", "warnings": [],
            "data": {"cases": [{
                "id": "CASE-1", "title": "Valid login", "requirement_ids": ["REQ-1"],
                "steps": [{"order": 1, "action": "Log in", "expected": "Home"}],
                "expected": "Home", "method": "EP", "priority": "high",
                "traceability": [{"requirement_id": "REQ-1"}],
            }]},
        }
        assert project_event(task_id, "task.result", {
            "task_id": task_id, "session_id": session_id, "result": result, "_remote_sequence": 2,
        })
        with SessionLocal() as db:
            assert db.get(HarnessTask, task_id).status == "running"
            assert db.scalar(select(CaseDraft.id).where(CaseDraft.analysis_id == run_id)) is None

        attempted = threading.Event()
        outcome: list[object] = []
        worker = None

        def observe_lock(_connection, _cursor, statement, _parameters, _context, _executemany):
            if threading.current_thread() is worker and statement.lstrip().upper().startswith("UPDATE ANALYSIS_RUNS"):
                attempted.set()

        def complete():
            try:
                outcome.append(project_event(task_id, "task.completed", {
                    "task_id": task_id, "_remote_sequence": 3,
                }))
            except BaseException as exc:
                outcome.append(exc)

        event.listen(engine, "before_cursor_execute", observe_lock)
        try:
            with SessionLocal.begin() as decision:
                run = decision.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update())
                if run is None:
                    raise AssertionError("fixture analysis disappeared")
                worker = threading.Thread(target=complete, name="case-context-projection", daemon=True)
                worker.start()
                if not attempted.wait(10):
                    raise AssertionError("projection did not reach the locked analysis row")
                if not worker.is_alive():
                    raise AssertionError(f"projection escaped the source lock: {outcome!r}")
                run.result = {**run.result, "ambiguities": [{
                    "id": "AMB-1", "description": "Login policy", "critical": False,
                    "decision": "revised",
                }]}
                run.version += 1
        finally:
            if worker is not None:
                worker.join(timeout=15)
            event.remove(engine, "before_cursor_execute", observe_lock)

        if worker is None or worker.is_alive() or len(outcome) != 1 or not isinstance(outcome[0], AnalysisContextChanged):
            raise AssertionError(f"stale projection was not rejected after concurrent decision: {outcome!r}")
        assert project_invalid_result(task_id, {"task_id": task_id, "_remote_sequence": 3}, outcome[0])
        with SessionLocal() as db:
            task = db.get(HarnessTask, task_id)
            assert task.status == "failed" and task.error["code"] == "analysis_changed"
            assert db.scalar(select(CaseDraft.id).where(CaseDraft.analysis_id == run_id)) is None
            assert db.get(AnalysisRun, run_id).version == 2
        print("PostgreSQL Harness case-context race: rejected stale draft")
    finally:
        if project_id is not None:
            with SessionLocal.begin() as db:
                db.execute(delete(CaseDraft).where(CaseDraft.analysis_id == run_id))
                db.execute(delete(HarnessTaskEvent).where(HarnessTaskEvent.task_id == task_id))
                db.execute(delete(HarnessTask).where(HarnessTask.id == task_id))
                db.execute(delete(AnalysisRun).where(AnalysisRun.id == run_id))
                db.execute(delete(Project).where(Project.id == project_id))


if __name__ == "__main__":
    verify()
