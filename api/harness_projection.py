"""Durable, idempotent projection of DeepSeek Harness events.

This module is deliberately independent from the HTTP routes.  Both the API
SSE adapter and the background projector use the same transaction boundary so
that a browser reconnect and a worker cannot create duplicate business rows.
No source document text is logged or copied into projector diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .db import (
    AnalysisRun,
    CaseDraft,
    HarnessTask,
    HarnessTaskEvent,
    SessionLocal,
    utcnow,
)
from .harness_case_context import AnalysisContextChanged, case_analysis_context, case_context_sha256
from .harness_contracts import validate_requirement_analysis, validate_skill_execution, validate_test_cases


TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
ALLOWED_HARNESS_EVENTS = frozenset({
    "task.created", "task.started", "task.progress", "task.confirmation_required",
    "task.result", "task.failed", "task.cancelled", "task.completed",
})


_CANCEL_MESSAGES = {
    "project_skill_disabled": "项目技能已停用",
    "project_access_revoked": "项目成员权限已移除",
    "project_member_readonly": "项目成员已变为只读",
    "project_archived": "项目已归档",
}

_REMOTE_FAILURE_MESSAGES = {
    "provider_unavailable": "Harness 运行器或模型服务不可用，请检查服务后重试",
    "host_unavailable": "Harness Host 暂不可用",
    "queue_full": "Harness 队列已满，请稍后重试",
    "input_timeout": "Harness 分段上传超时，请重新发起任务",
    "task_conflict": "Harness 任务冲突，请刷新后重试",
    "window_policy_mismatch": "Harness 与 Test Atelier 的分窗配置不一致，请联系管理员核对",
    "confirmation_unavailable": "Harness 当前无法处理人工确认",
    "timeout": "Harness 任务超过执行时限",
    "output_too_large": "Harness 模型输出超过任务上限",
    "runner_stop_unconfirmed": "Harness 运行器停止状态未确认",
    "request_too_large": "Harness 输入超过任务上限",
    "reduce_budget_exceeded": "Harness 聚合预算不足，请拆分材料或调整预算",
    "reduce_item_too_large": "Harness 聚合结果超过单批上限",
    "schema_invalid": "Harness 结果未通过格式或覆盖校验",
    "quality_gate_failed": "Harness 结果未通过质量门禁",
    "report_missing": "Harness 需求分析未返回可交付报告",
    "skill_not_allowed": "Harness 请求加载未授权技能",
    "profile_policy_mismatch": "Harness Profile 已变更，任务未启动",
    "host_shutdown": "Harness Host 已停止任务",
    "analysis_changed": "分析已更新，请基于最新分析重试用例任务",
}


def _safe_remote_failure(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or code not in _REMOTE_FAILURE_MESSAGES:
        code = "provider_unavailable"
    failure = {"code": code, "message": _REMOTE_FAILURE_MESSAGES[code]}
    if code == "analysis_changed" and error.get("retryable") is True:
        failure["retryable"] = True
    return failure


def cancel_active_tasks(db, project_id: int, *, reason: str,
                        skill_id: str | None = None, user_id: int | None = None) -> list[tuple[str, int]]:
    """Prevent revoked work from publishing in the same transaction as the ACL change."""
    message = _CANCEL_MESSAGES[reason]
    query = select(HarnessTask).where(
        HarnessTask.project_id == project_id,
        HarnessTask.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
    )
    if skill_id is not None:
        query = query.where(HarnessTask.skill_id == skill_id)
    if user_id is not None:
        query = query.where(HarnessTask.user_id == user_id)
    tasks = db.scalars(query.with_for_update()).all()
    cancelled = []
    for task in tasks:
        sequence = task.sequence + 1
        task.sequence = sequence
        task.status = "cancelled"
        task.result = {}
        task.updated_at = utcnow()
        task.error = {"code": reason, "message": message}
        if task.task_type == "requirement-analysis" and task.analysis_run_id:
            run = db.get(AnalysisRun, task.analysis_run_id)
            if run is not None and run.project_id == project_id:
                run.status = "failed"
                run.error = message
                run.updated_at = utcnow()
        db.add(HarnessTaskEvent(
            task_id=task.id,
            sequence=sequence,
            event="task.cancelled",
            payload={"task_id": task.id, "sequence": sequence, "reason": reason},
        ))
        cancelled.append((task.id, task.user_id))
    return cancelled


def cancel_tasks_for_disabled_skill(db, project_id: int, skill_id: str) -> list[tuple[str, int]]:
    return cancel_active_tasks(db, project_id, reason="project_skill_disabled", skill_id=skill_id)


def validate_remote_event(task_id: str, event_name: str, payload: Any, cursor: int) -> int | None:
    """Validate a Host event before assigning it a local sequence or cursor."""
    if event_name not in ALLOWED_HARNESS_EVENTS:
        raise ValueError("Harness event type is not allowed")
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise ValueError("Harness event task_id does not match task")
    sequence = payload.get("sequence")
    # The Host is a Node process: its monotonically increasing sequence is a
    # JSON-safe integer, never a float, bool or guessed value.
    if type(sequence) is not int or not 0 < sequence <= 2**53 - 1:
        raise ValueError("Harness event sequence is invalid")
    return sequence if sequence > cursor else None


def _remote_sequence(payload: dict[str, Any]) -> int | None:
    value = payload.get("_remote_sequence")
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _result_diagnostic(result: Any) -> dict[str, Any]:
    """Return a bounded, non-content diagnostic for an invalid result.

    A malformed model response must remain diagnosable after the remote
    stream has gone away, but storing the response verbatim would duplicate
    source material in the business database.  Keep only stable metadata and
    a digest so operators can correlate the protected Harness Session.
    """
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(result).encode("utf-8", errors="replace")
    summary: dict[str, Any] = {
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
        "result_bytes": len(encoded),
        "result_type": type(result).__name__,
    }
    def safe_keys(values: Any, allowed: set[str]) -> list[str]:
        if not isinstance(values, dict):
            return []
        output: list[str] = []
        for key in sorted(values.keys(), key=lambda item: str(item))[:64]:
            text = str(key)
            # Arbitrary model keys can contain user text. Keep only protocol
            # names; unknown keys are represented by a short digest.
            output.append(text if text in allowed else f"sha256:{hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:12]}")
        return output

    if isinstance(result, dict):
        # Model-owned envelope strings may themselves contain copied document
        # text or credentials. Only known schema names are safe to show raw.
        schema = result.get("schema")
        if isinstance(schema, str):
            known_schemas = {"requirement-analysis.v1", "test-cases.v1", "skill-execution.v1"}
            summary["schema"] = schema if schema in known_schemas else "unknown"
        for name in ("schema", "result_id", "session_id"):
            value = result.get(name)
            if isinstance(value, str):
                summary[f"{name}_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        data = result.get("data")
        if isinstance(data, dict):
            summary["data_keys"] = safe_keys(data, {"requirements", "ambiguities", "quality_warnings", "cases", "output", "endpoints", "models", "errors", "test_points", "items", "metadata"})
        summary["top_level_keys"] = safe_keys(result, {"schema", "result_id", "task_id", "session_id", "source_document_ids", "analysis_run_id", "skill_id", "generated_at", "warnings", "data"})
    return summary


def projection_failure(error: BaseException | str) -> dict[str, Any]:
    if isinstance(error, AnalysisContextChanged):
        return {"code": "analysis_changed", "message": "分析已更新，请基于最新分析重试用例任务", "retryable": True}
    # Never render a Pydantic error: its text can include model output.
    return {"code": "schema_invalid", "message": "Harness 结果未通过 Schema 校验"}


def project_invalid_result(task_id: str, payload: dict[str, Any], error: BaseException | str) -> bool:
    """Persist a schema failure without retaining the model's raw output."""
    if not isinstance(payload, dict):
        return False
    remote_sequence = _remote_sequence(payload)
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id, with_for_update=True)
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return False
        if remote_sequence is not None:
            latest_remote = db.scalar(select(func.max(HarnessTaskEvent.remote_sequence)).where(
                HarnessTaskEvent.task_id == task_id,
            ))
            if latest_remote is not None and remote_sequence <= int(latest_remote):
                return False
            duplicate = db.scalar(select(HarnessTaskEvent.id).where(
                HarnessTaskEvent.task_id == task_id,
                HarnessTaskEvent.remote_sequence == remote_sequence,
            ))
            if duplicate is not None:
                return False
        result = payload.get("result")
        details = _result_diagnostic(result)
        failure = {**projection_failure(error), "details": details}
        sequence = task.sequence + 1
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "sequence": sequence,
            "error": failure,
        }
        if remote_sequence is not None:
            event_payload["_remote_sequence"] = remote_sequence
        task.sequence = sequence
        task.status = "failed"
        task.error = failure
        task.updated_at = utcnow()
        # A failed case-authoring task must not invalidate the confirmed
        # analysis run it consumed; that run is the source for a safe retry.
        if task.task_type == "requirement-analysis" and task.analysis_run_id:
            run = db.get(AnalysisRun, task.analysis_run_id)
            if run is not None and run.project_id == task.project_id:
                run.status = "failed"
                run.error = failure["message"]
                run.updated_at = utcnow()
        db.add(HarnessTaskEvent(
            task_id=task.id,
            sequence=sequence,
            remote_sequence=remote_sequence,
            event="task.failed",
            payload=event_payload,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True


def _validate_result(db, task: HarnessTask, payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Harness result must be an object")
    if result.get("task_id") != task.id:
        raise ValueError("Harness result task_id does not match task")
    result_session = result.get("session_id")
    if payload.get("session_id") != result_session:
        raise ValueError("Harness result session_id does not match event")
    if result_session == f"ta-{task.id}":
        raise ValueError("Harness result session_id is only a provisional allocation")
    # Real dsh sessions can differ from the provisional ID and map/reduce can
    # bind several sessions. Only IDs already projected from earlier Host events
    # may become the final result session; the result cannot self-authorize one.
    if result_session not in {task.session_id, *(task.session_ids or [])}:
        raise ValueError("Harness result session_id was not bound to task")
    source_ids = set(task.source_document_ids or [])
    if task.task_type == "requirement-analysis":
        validate_requirement_analysis(result, source_ids)
        return result
    if task.task_type == "skill-execution":
        validate_skill_execution(result, source_ids, task.skill_id)
        return result
    # Serialize the source check with human decisions through completion.
    # A no-op UPDATE also takes SQLite's writer lock (FOR UPDATE is ignored
    # there); PostgreSQL holds the row lock until the draft commit.
    locked = db.execute(update(AnalysisRun).where(
        AnalysisRun.id == task.analysis_run_id,
        AnalysisRun.project_id == task.project_id,
    ).values(version=AnalysisRun.version))
    if locked.rowcount != 1:
        raise AnalysisContextChanged("analysis run not found for Harness result")
    run = db.get(AnalysisRun, task.analysis_run_id, populate_existing=True)
    if run is None or run.project_id != task.project_id:
        raise AnalysisContextChanged("analysis run not found for Harness result")
    if run.status != "ready":
        raise AnalysisContextChanged("analysis run is no longer ready for case authoring")
    if run.provider != "harness" or not isinstance(run.result, dict):
        raise AnalysisContextChanged("case authoring requires a valid Harness analysis")
    source_result = run.result.get("harness_result")
    if not isinstance(source_result, dict):
        raise AnalysisContextChanged("case authoring requires a valid Harness analysis")
    if set(run.source_ids or []) != source_ids:
        raise AnalysisContextChanged("case authoring analysis sources changed")
    validate_requirement_analysis(source_result, source_ids)
    created = db.scalar(select(HarnessTaskEvent).where(
        HarnessTaskEvent.task_id == task.id, HarnessTaskEvent.event == "task.created",
    ).order_by(HarnessTaskEvent.sequence).limit(1))
    if created is None or not isinstance(created.payload, dict):
        raise AnalysisContextChanged("case authoring analysis context was not pinned")
    if (type(created.payload.get("analysis_version")) is not int
            or not isinstance(created.payload.get("analysis_sha256"), str)):
        raise AnalysisContextChanged("case authoring analysis context was not pinned")
    context = case_analysis_context(run.id, run.version, run.result)
    if (created.payload.get("analysis_version") != run.version
            or created.payload.get("analysis_sha256") != case_context_sha256(context)):
        raise AnalysisContextChanged("case authoring analysis context changed")
    decisions = {
        str(item.get("id"))
        for item in run.result.get("ambiguities", [])
        if isinstance(item, dict) and str(item.get("decision") or "").strip()
    }
    if any(item.get("critical") and str(item.get("id")) not in decisions
           for item in run.result.get("ambiguities", []) if isinstance(item, dict)):
        raise AnalysisContextChanged("blocking ambiguity is not confirmed")
    if any(item.get("severity") == "blocking" and str(item.get("id")) not in decisions
           for item in source_result["data"].get("ambiguities", []) if isinstance(item, dict)):
        raise AnalysisContextChanged("blocking ambiguity is not confirmed")
    if result.get("analysis_run_id") != task.analysis_run_id:
        raise ValueError("Harness result analysis_run_id does not match task")
    requirements = {
        str(item.get("id"))
        for item in source_result["data"].get("requirements", [])
        if isinstance(item, dict)
    }
    criteria = {
        str(item.get("id")): str(req.get("id"))
        for req in source_result["data"].get("requirements", [])
        if isinstance(req, dict)
        for item in (req.get("acceptance_criteria") or [])
        if isinstance(item, dict)
    }
    validate_test_cases(result, source_ids, requirements, criteria)
    return result


def _project_result(db, task: HarnessTask, result: dict[str, Any]) -> None:
    """Commit a validated result after successful task completion.

    The caller has checked the deferred marker and remote event sequence.
    Business rows and the terminal event share one database transaction.
    """
    if task.task_type == "requirement-analysis":
        data = result["data"]
        legacy = {
            "requirements": [{
                "id": item["id"],
                "title": item["title"],
                "summary": item["description"],
                "source_ids": sorted({ref["document_id"] for ref in item["source_refs"]}),
                "acceptance": [criterion["text"] for criterion in item.get("acceptance_criteria", [])],
                # The Harness requirement envelope has no method proposal.
                # Do not invent one in the legacy business compatibility view.
                "methods": [],
            } for item in data.get("requirements", [])],
            "ambiguities": [{
                "id": item["id"],
                "description": item["question"],
                "critical": item["severity"] == "blocking",
                "decision": item.get("resolution") or "",
            } for item in data.get("ambiguities", [])],
            "risks": [warning["message"] for warning in data.get("quality_warnings", [])],
        }
        run = db.get(AnalysisRun, task.analysis_run_id)
        if run is None or run.project_id != task.project_id:
            raise ValueError("analysis run not found for Harness result")
        run.result = {
            **legacy,
            # Historical results may lack a report; never synthesize a skill
            # deliverable from the compatibility projection.
            **({"report_markdown": result["report_markdown"]} if "report_markdown" in result else {}),
            "harness_result": result,
        }
        run.status = "ready"
        run.error = ""
        run.updated_at = utcnow()
        run.version += 1
        return

    if task.task_type == "skill-execution":
        # Generic skills intentionally remain a read-only Harness result. No
        # requirement/case rows are inferred from arbitrary skill output.
        return

    run = db.get(AnalysisRun, task.analysis_run_id)
    if run is None or run.project_id != task.project_id:
        raise ValueError("analysis run not found for Harness result")
    for item in result["data"].get("cases", []):
        db.add(CaseDraft(
            id=uuid.uuid4().hex,
            project_id=task.project_id,
            analysis_id=run.id,
            payload={
                "module": item.get("module_path") or item.get("labels", [])[:8] or ["未分类"],
                "title": item["title"],
                "priority": item.get("skill_priority") or {"high": "P1", "medium": "P2", "low": "P3"}[item["priority"]],
                "precondition": "\n".join(item.get("preconditions", [])),
                "steps": [{"action": step["action"], "expected": step["expected"]} for step in item["steps"]],
                "requirement_ids": item["requirement_ids"],
                "method": item["method"],
                "label": ", ".join(item.get("labels", [])),
                "remark": "",
                "smoke": "",
            },
        ))


def project_event(task_id: str, event_name: str, payload: dict[str, Any]) -> bool:
    """Atomically append one event and apply its business effect.

    Returns ``False`` for duplicate or stale events.  Remote sequence numbers
    are persisted separately from the local browser sequence and provide the
    idempotency key used by the background worker.
    """
    if event_name not in ALLOWED_HARNESS_EVENTS or not isinstance(payload, dict):
        return False
    remote_sequence = _remote_sequence(payload)
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id, with_for_update=True)
        if task is None:
            return False
        if payload.get("task_id") != task_id:
            raise ValueError("Harness event task_id does not match task")
        # A terminal task is immutable. This also prevents a slower worker
        # stream from applying a late result after a browser/peer projector
        # already committed cancellation or failure.
        if task.status in TERMINAL_TASK_STATUSES:
            return False
        if remote_sequence is not None:
            latest_remote = db.scalar(select(func.max(HarnessTaskEvent.remote_sequence)).where(
                HarnessTaskEvent.task_id == task_id,
            ))
            if latest_remote is not None and remote_sequence <= int(latest_remote):
                return False
            duplicate = db.scalar(select(HarnessTaskEvent.id).where(
                HarnessTaskEvent.task_id == task_id,
                HarnessTaskEvent.remote_sequence == remote_sequence,
            ))
            if duplicate is not None:
                return False
        # The local sequence is owned by Test Atelier. Remote values are never
        # allowed to move it backwards, create gaps, or create an arbitrary
        # browser cursor. (The remote sequence is stored separately below.)
        sequence = task.sequence + 1
        event_payload = {**payload, "sequence": sequence}
        if event_name == "task.failed":
            # Failure messages and extra fields are Host-controlled, unlike a
            # validated successful result. Never archive them verbatim.
            event_payload = {"task_id": task_id, "sequence": sequence,
                             "error": _safe_remote_failure(payload)}
            if remote_sequence is not None:
                event_payload["_remote_sequence"] = remote_sequence
        if event_name == "task.result":
            result = _validate_result(db, task, event_payload)
            # Old result events already applied their business projection. A
            # marker on new events lets a rolling deployment defer only new
            # results until a confirmed terminal success, without duplicating
            # drafts for an in-flight task created by the previous version.
            event_payload["_business_projection"] = "pending"
        elif event_name == "task.completed":
            if not isinstance(task.result, dict) or task.result.get("schema") != task.result_schema:
                raise ValueError("Harness task completed without a validated result")
            staged = db.scalar(select(HarnessTaskEvent).where(
                HarnessTaskEvent.task_id == task_id,
                HarnessTaskEvent.event == "task.result",
            ).order_by(HarnessTaskEvent.sequence.desc()).limit(1))
            if staged is not None and staged.payload.get("_business_projection") == "pending":
                if task.task_type == "test-cases":
                    _validate_result(db, task, {"result": task.result, "session_id": task.result.get("session_id")})
                _project_result(db, task, task.result)
        task.sequence = sequence
        task.updated_at = utcnow()
        if event_name == "task.started":
            task.status = "running"
        elif event_name == "task.confirmation_required":
            task.status = "waiting_confirmation"
        elif event_name == "task.progress" and event_payload.get("status") in {"running", "waiting_confirmation"}:
            task.status = event_payload["status"]
        elif event_name == "task.result":
            task.result = event_payload["result"]
        elif event_name == "task.failed":
            task.status = "failed"
            task.error = event_payload.get("error") if isinstance(event_payload.get("error"), dict) else {"message": "Harness 任务失败"}
            if task.task_type == "requirement-analysis" and task.analysis_run_id:
                run = db.get(AnalysisRun, task.analysis_run_id)
                if run is not None and run.project_id == task.project_id:
                    run.status = "failed"
                    run.error = str(task.error.get("message") or "Harness 任务失败")
                    run.updated_at = utcnow()
        elif event_name == "task.cancelled":
            task.status = "cancelled"
            if task.task_type == "requirement-analysis" and task.analysis_run_id:
                run = db.get(AnalysisRun, task.analysis_run_id)
                if run is not None and run.project_id == task.project_id:
                    run.status = "failed"
                    run.error = "Harness 任务已取消"
                    run.updated_at = utcnow()
        elif event_name == "task.completed" and task.status not in {"failed", "cancelled"}:
            task.status = "completed"
        session_id = event_payload.get("session_id")
        binds_session = event_name in {"task.started", "task.result"} or (
            event_name == "task.progress" and event_payload.get("kind") == "session_bound"
        )
        if event_name == "task.started" and session_id == f"ta-{task.id}":
            task.session_id = session_id
        elif binds_session and isinstance(session_id, str) and session_id and session_id != f"ta-{task.id}":
            task.session_id = session_id
            if event_name == "task.progress" and event_payload.get("kind") == "session_bound":
                # The starting ID is an allocation placeholder, not a dsh
                # artifact. Preserve it only until a real Session is bound.
                provisional = f"ta-{task.id}"
                if session_id != provisional and provisional in (task.session_ids or []):
                    task.session_ids = [value for value in task.session_ids if value != provisional]
            if session_id not in (task.session_ids or []):
                task.session_ids = [*(task.session_ids or []), session_id]
        db.add(HarnessTaskEvent(
            task_id=task.id,
            sequence=sequence,
            remote_sequence=remote_sequence,
            event=event_name,
            payload=event_payload,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True


def remote_cursor(task_id: str) -> int:
    """Return the greatest remote event sequence already durably projected."""
    with SessionLocal() as db:
        values = db.scalars(select(HarnessTaskEvent.remote_sequence).where(
            HarnessTaskEvent.task_id == task_id,
            HarnessTaskEvent.remote_sequence.is_not(None),
        )).all()
        return max((int(value) for value in values if value is not None), default=0)
