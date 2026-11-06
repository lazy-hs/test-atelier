"""Project-scoped API Bridge routes for DeepSeek Harness tasks."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import harness_bridge
from .harness_artifacts import SkillArtifactUnavailable, skill_xmind_cases
from .harness_case_context import case_analysis_context, case_context_sha256
from .db import AnalysisRun, HarnessTask, HarnessTaskEvent, Project, ProjectSkill, SourceDocument, User
from .harness_contracts import (
    GENERIC_SKILL_IDS,
    HarnessConfirmation,
    HarnessTaskCreate,
    HarnessTaskView,
    validate_requirement_analysis,
)
from .harness_chunking import chunk_limits, count_transport_chunks, iter_transport_chunks
from .harness_projection import (
    project_event,
    project_invalid_result,
    remote_cursor as persisted_remote_cursor,
    validate_remote_event,
)
from .observability import request_id as current_request_id
from .skill_runner import SkillRunnerError, discover_skills, generate_xmind as generate_skill_xmind

logger = logging.getLogger(__name__)


def _invalid_remote_payload(task_id: str, event_name: str, payload: object, sequence: int | None) -> dict:
    """Keep only verified routing metadata and a result for safe diagnostics."""
    failure = {"task_id": task_id}
    if sequence is not None:
        failure["_remote_sequence"] = sequence
    if (event_name == "task.result" and isinstance(payload, dict)
            and payload.get("task_id") == task_id):
        failure["result"] = payload.get("result")
    return failure


def _result_schema(task_type: str) -> str:
    if task_type == "requirement-analysis":
        return "requirement-analysis.v1"
    if task_type == "test-cases":
        return "test-cases.v1"
    return "skill-execution.v1"


def _human_decisions(result: dict) -> dict[str, str]:
    return {
        item["id"]: item["decision"].strip()
        for item in result.get("ambiguities", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("decision"), str) and item["decision"].strip()
    }


def _max_bridge_request_bytes() -> int:
    raw = os.getenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "8388608")
    try:
        value = int(raw)
    except ValueError:
        value = 8 * 1024 * 1024
    return max(64 * 1024, min(value, 32 * 1024 * 1024))


def _needs_staged_source_windows(request_bytes: int, document_count: int) -> bool:
    """Stage before the regular Host window count can be exhausted by line breaks."""

    size, overlap, maximum = chunk_limits()
    # The Host may choose a line just past one third of a window. Account for
    # that shortest advance, the overlap and one window per source document.
    shortest_advance = max(size // 3, overlap) + 1 - overlap
    return request_bytes > max(0, maximum - document_count) * shortest_advance


def _staged_input_manifest(documents: list[dict], request_limit: int) -> tuple[list[dict], int, list[bytes]]:
    """Keep each Host upload below its normal request cap, including JSON overhead."""

    chunk_bytes = min(768 * 1024, (request_limit - 4096) // 2)
    manifest = []
    encoded_documents = []
    total = 0
    for document in documents:
        encoded = document["text"].encode("utf-8")
        size = len(encoded)
        total += size
        if size < 1 or size > 100 * 1024 * 1024 or total > 128 * 1024 * 1024:
            raise ValueError("Harness 文本超出单材料 100 MiB 或单任务 128 MiB 的传输预算")
        chunks = count_transport_chunks(encoded, chunk_bytes, request_limit - 4096)
        if chunks > 2048:
            raise ValueError("Harness 材料传输窗口超过单文档上限")
        manifest.append({"id": document["id"], "bytes": size, "chunks": chunks,
                         "sha256": hashlib.sha256(encoded).hexdigest()})
        encoded_documents.append(encoded)
    if not manifest:
        raise ValueError("Harness 大任务缺少可上传的材料")
    return manifest, chunk_bytes, encoded_documents


def _task_view(task: HarnessTask) -> dict:
    provisional = f"ta-{task.id}"
    return HarnessTaskView(
        task_id=task.id,
        project_id=task.project_id,
        task_type=task.task_type,
        skill_id=task.skill_id,
        task_version=task.task_version,
        status=task.status,
        session_id=task.session_id if task.session_id != provisional else None,
        session_ids=[str(value) for value in (task.session_ids or []) if isinstance(value, str) and value != provisional],
        source_document_ids=[str(value) for value in (task.source_document_ids or []) if isinstance(value, str)],
        retry_of=task.retry_of,
        result_schema=task.result_schema,
        analysis_run_id=task.analysis_run_id,
        sequence=task.sequence,
        created_at=task.created_at,
        updated_at=task.updated_at,
    ).model_dump()


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/harness", tags=["deepseek-harness"])

    def scope(project_id: int, user: User, db: Session) -> None:
        project_access(project_id, user, db)

    def require_skill(project_id: int, skill_id: str, user: User, db: Session) -> ProjectSkill:
        skill = db.get(ProjectSkill, (project_id, skill_id))
        if skill is None:
            try:
                spec = next(item for item in discover_skills() if item.skill_id == skill_id)
            except (SkillRunnerError, StopIteration) as exc:
                raise HTTPException(503, "项目技能仓不可用") from exc
            writable(user, project_id, db, fence=True)
            skill = ProjectSkill(project_id=project_id, skill_id=spec.skill_id, name=spec.name,
                                 label=spec.label, source_path=spec.path, description=spec.description,
                                 enabled=spec.enabled_by_default)
            db.add(skill)
            db.commit()
            db.refresh(skill)
        if not skill.enabled:
            raise HTTPException(409, f"项目技能已停用：{skill.label}")
        return skill

    def find_task(project_id: int, task_id: str, db: Session) -> HarnessTask:
        task = db.get(HarnessTask, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(404, "Harness 任务不存在")
        return task

    def append_event(task_id: str, event_name: str, payload: dict) -> bool:
        """Use the same atomic projection path as the background worker."""

        return project_event(task_id, event_name, payload)

    def _project_remote_events(task_id: str, user_id: int, project_id: int, *, limit: int = 64) -> int:
        """Catch up a task without requiring a browser SSE connection.

        Harness owns a durable event log, while the Test Atelier database owns
        the business projection.  The browser normally drives this projection
        through ``task_events``; a disconnected browser must not be the only
        consumer, however.  This bounded catch-up is intentionally reusable by
        the task-detail endpoint (and therefore by page refresh/retry) without
        holding the request's SQLAlchemy session while reading remote events.
        """
        from .db import SessionLocal

        bridge = harness_bridge.HarnessBridge()
        if not bridge.config.base_url:
            return 0
        with SessionLocal() as state_db:
            task = state_db.get(HarnessTask, task_id)
            if task is None or task.project_id != project_id or task.status in {"completed", "failed", "cancelled"}:
                return 0
            remote_cursor_for_task = persisted_remote_cursor(task.id)

        projected = 0
        request_id = current_request_id()
        try:
            scope = {
                "subject_user_id": user_id,
                "project_id": project_id,
                "task_id": task_id,
                "allowed_operation": "task.events",
            }
            try:
                remote_stream = bridge.stream_events(task_id, remote_cursor_for_task, request_id, scope,
                                                     idle_timeout_seconds=1.0)
            except TypeError as exc:
                # Keep small test/deployment adapters written against the
                # pre-projector bridge signature usable during a rolling
                # upgrade.  The built-in bridge always supports the bound.
                if "idle_timeout_seconds" not in str(exc):
                    raise
                remote_stream = bridge.stream_events(task_id, remote_cursor_for_task, request_id, scope)
            for event_name, payload in remote_stream:
                if projected >= limit:
                    break
                remote_sequence = None
                try:
                    remote_sequence = validate_remote_event(task_id, event_name, payload, remote_cursor_for_task)
                    if remote_sequence is None:
                        continue
                    projected_payload = {**payload, "_remote_sequence": remote_sequence}
                    if project_event(task_id, event_name, projected_payload):
                        projected += 1
                except (ValueError, TypeError) as exc:
                    # Preserve a bounded digest/metadata diagnostic while
                    # avoiding a second copy of the model's raw document text.
                    project_invalid_result(task_id, _invalid_remote_payload(task_id, event_name, payload, remote_sequence), exc)
                    projected += 1
                    break
                remote_cursor_for_task = remote_sequence
        except harness_bridge.HarnessBridgeError as exc:
            # A transient catch-up failure must not turn a still-running task
            # into a business failure.  The next refresh/SSE reconnect retries
            # from the last persisted remote cursor.
            logger.warning("harness.projector.unavailable", extra={
                "event": "harness.projector.unavailable", "task_id": task_id,
                "project_id": project_id, "error_type": type(exc).__name__,
            })
        return projected

    def _session_for_validation():
        from .db import SessionLocal

        return SessionLocal()

    @router.post("/tasks", dependencies=[Depends(csrf)])
    def create_task(project_id: int, payload: HarnessTaskCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        require_skill(project_id, payload.skill_id, user, db)
        analysis_payload = None
        # Serialize the new task row with the durable deletion intent. The
        # UPDATE is also a writer lock on SQLite, where FOR UPDATE is ignored.
        locked = db.execute(update(Project).where(Project.id == project_id).values(name=Project.name))
        if locked.rowcount != 1:
            raise HTTPException(404, "项目不存在")
        db.expire_all()
        if db.get(Project, project_id).deletion_pending_at:
            raise HTTPException(409, "项目正在删除，请稍后重试删除操作")
        existing = db.scalar(select(HarnessTask).where(
            HarnessTask.project_id == project_id,
            HarnessTask.user_id == user.id,
            HarnessTask.idempotency_key == payload.idempotency_key,
        ))
        if existing is not None:
            return _task_view(existing)
        if payload.task_type in {"requirement-analysis", "skill-execution"}:
            for source_id in payload.source_document_ids:
                source = db.get(SourceDocument, source_id)
                if source is None or source.project_id != project_id or not source.extracted_text.strip():
                    raise HTTPException(422, "文档不存在或未提取到可分析内容")
        if payload.task_type == "requirement-analysis":
            analysis_run = AnalysisRun(
                id=uuid.uuid4().hex,
                project_id=project_id,
                user_id=user.id,
                source_ids=payload.source_document_ids,
                status="queued",
                provider="harness",
                result={},
            )
            db.add(analysis_run)
            db.flush()
            payload.analysis_run_id = analysis_run.id
        elif payload.task_type == "test-cases":
            run = db.get(AnalysisRun, payload.analysis_run_id)
            if run is None or run.project_id != project_id or run.status != "ready":
                raise HTTPException(409, "分析结果尚未就绪")
            if run.provider != "harness" or not isinstance(run.result, dict):
                raise HTTPException(409, "用例编写需要有效的 Harness 需求分析结果")
            original = run.result.get("harness_result")
            if not isinstance(original, dict):
                raise HTTPException(409, "用例编写需要有效的 Harness 需求分析结果")
            try:
                validate_requirement_analysis(original, set(run.source_ids or []))
            except (ValueError, TypeError) as exc:
                raise HTTPException(409, "用例编写需要有效的 Harness 需求分析结果") from exc
            if any(item.get("critical") and not item.get("decision") for item in (run.result or {}).get("ambiguities", []) if isinstance(item, dict)):
                raise HTTPException(409, "请先确认关键歧义")
            decisions = _human_decisions(run.result)
            if any(item.get("severity") == "blocking" and item.get("id") not in decisions
                   for item in original["data"].get("ambiguities", []) if isinstance(item, dict)):
                raise HTTPException(409, "请先确认关键歧义")
            payload.source_document_ids = list(run.source_ids or [])
            analysis_payload = case_analysis_context(run.id, run.version, run.result)
        task = HarnessTask(
            id=uuid.uuid4().hex,
            project_id=project_id,
            user_id=user.id,
            task_type=payload.task_type,
            task_version=payload.task_version,
            result_schema=_result_schema(payload.task_type),
            skill_id=payload.skill_id,
            source_document_ids=payload.source_document_ids,
            analysis_run_id=payload.analysis_run_id,
            retry_of=None,
            session_id=payload.session_id,
            session_ids=[payload.session_id] if payload.session_id else [],
            idempotency_key=payload.idempotency_key,
            status="queued",
            sequence=1,
            result={},
            error={},
        )
        created_event = {"task_id": task.id, "sequence": 1}
        if analysis_payload is not None:
            created_event.update({"analysis_version": analysis_payload["analysis_version"],
                                  "analysis_sha256": case_context_sha256(analysis_payload)})
        db.add(task)
        db.add(HarnessTaskEvent(task_id=task.id, sequence=1, event="task.created", payload=created_event))
        db.commit()
        bridge = harness_bridge.HarnessBridge()
        remote_created = False

        def cancel_unfinished_remote() -> None:
            if not remote_created:
                return
            try:
                bridge.cancel_task(task.id, current_request_id(), {
                    "subject_user_id": user.id, "project_id": project_id, "task_id": task.id,
                    "allowed_operation": "task.cancel",
                })
            except harness_bridge.HarnessBridgeError as cancel_error:
                logger.warning("harness.task.input_cancel_unavailable", extra={
                    "event": "harness.task.input_cancel_unavailable", "component": "harness",
                    "project_id": project_id, "task_id": task.id, "error_type": type(cancel_error).__name__,
                })

        try:
            source_payload = []
            for source_id in payload.source_document_ids:
                source = db.get(SourceDocument, source_id)
                if source is not None:
                    source_payload.append({"id": source.id, "filename": source.filename,
                                           "text": "" if payload.task_type == "test-cases" else source.extracted_text})
            bridge_payload = {
                "task_id": task.id,
                "project_id": project_id,
                "user_id": user.id,
                "task_type": payload.task_type,
                "task_version": payload.task_version,
                "skill_id": payload.skill_id,
                "source_document_ids": payload.source_document_ids,
                "analysis_run_id": payload.analysis_run_id,
                "session_id": payload.session_id,
                "input": payload.input,
                "source_documents": source_payload,
                "analysis": analysis_payload,
            }
            if payload.task_type != "test-cases":
                size, overlap, maximum = chunk_limits()
                bridge_payload["window_policy"] = {
                    "chunk_chars": size, "overlap_chars": overlap, "max_chunks": maximum,
                }
            request_bytes = len(json.dumps(bridge_payload, ensure_ascii=False).encode("utf-8"))
            request_limit = _max_bridge_request_bytes()
            staged = request_bytes > request_limit or (
                payload.task_type != "test-cases" and _needs_staged_source_windows(request_bytes, len(source_payload))
            )
            if staged:
                try:
                    if payload.task_type == "test-cases":
                        if not isinstance(analysis_payload, dict) or analysis_payload.get("schema") != "requirement-analysis.v1":
                            raise ValueError("超限用例任务需要已确认的 Harness 需求分析")
                        staged_input = [{"id": f"analysis:{payload.analysis_run_id}",
                                         "text": json.dumps(analysis_payload, ensure_ascii=False, separators=(",", ":"))}]
                        bridge_payload["analysis"] = None
                        bridge_payload["staged_kind"] = "analysis"
                    else:
                        staged_input = source_payload
                        bridge_payload["source_documents"] = [
                            {"id": item["id"], "filename": item["filename"], "text": ""} for item in source_payload
                        ]
                    manifest, chunk_bytes, staged_bytes = _staged_input_manifest(staged_input, request_limit)
                except ValueError as exc:
                    append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                            "error": {"code": "request_too_large", "message": str(exc)}})
                    raise HTTPException(413, str(exc)) from exc
                bridge_payload["input_manifest"] = manifest
                if len(json.dumps(bridge_payload, ensure_ascii=False).encode("utf-8")) > request_limit:
                    append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                            "error": {"code": "request_too_large", "message": "Harness 分析上下文超出传输预算"}})
                    raise HTTPException(413, "Harness 分析上下文超出传输预算")
            if staged:
                # A lost create response is ambiguous: Host may already own
                # the ID. Best-effort cancellation prevents an orphaned upload.
                remote_created = True
            response = bridge.create_task(bridge_payload, current_request_id(), {
                "subject_user_id": user.id,
                "project_id": project_id,
                "task_id": task.id,
                "allowed_operation": "task.create",
            })
            if response.get("task_id") != task.id:
                raise harness_bridge.HarnessBridgeError("Harness task id mismatch")
            remote_created = True
            if staged:
                input_scope = {"subject_user_id": user.id, "project_id": project_id,
                               "task_id": task.id, "allowed_operation": "task.input"}
                for document_index, encoded in enumerate(staged_bytes):
                    for chunk_index, text in enumerate(iter_transport_chunks(encoded, chunk_bytes, request_limit - 4096)):
                        bridge.upload_input_chunk(task.id, document_index, chunk_index, text,
                                                  current_request_id(), input_scope)
                    staged_bytes[document_index] = b""
                bridge.start_staged_task(task.id, current_request_id(), input_scope)
        except harness_bridge.HarnessBridgeUnavailable as exc:
            cancel_unfinished_remote()
            append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                     "error": {"code": "provider_unavailable", "message": "DeepSeek Harness 暂不可用"}})
            raise HTTPException(503, "DeepSeek Harness 尚未配置") from exc
        except harness_bridge.HarnessBridgeHostUnavailable as exc:
            cancel_unfinished_remote()
            append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                     "error": {"code": "host_unavailable", "message": "DeepSeek Harness Host 不可用"}})
            raise HTTPException(503, "DeepSeek Harness Host 不可用，请先启动 Host") from exc
        except harness_bridge.HarnessBridgeHTTPError as exc:
            cancel_unfinished_remote()
            append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                     "error": {"code": exc.code, "message": "DeepSeek Harness 未接受任务"}})
            raise HTTPException(exc.status_code, exc.code) from exc
        except harness_bridge.HarnessBridgeError as exc:
            cancel_unfinished_remote()
            append_event(task.id, "task.failed", {"task_id": task.id, "sequence": 2,
                                                     "error": {"code": "provider_unavailable", "message": "DeepSeek Harness 任务创建失败"}})
            raise HTTPException(502, "DeepSeek Harness 任务创建失败") from exc
        started_payload = {"task_id": task.id, "sequence": 2, "_remote_sequence": 1,
                           "session_id": response.get("session_id")}
        append_event(task.id, "task.started", started_payload)
        with _session_for_validation() as final_db:
            current = final_db.get(HarnessTask, task.id)
            if current is not None and current.status == "cancelled":
                # Revocation may commit while Host creation is in flight.
                # Cancel again now that Host has definitely accepted the ID.
                try:
                    bridge.cancel_task(task.id, current_request_id(), {
                        "subject_user_id": current.user_id,
                        "project_id": project_id,
                        "task_id": task.id,
                        "allowed_operation": "task.cancel",
                    })
                except harness_bridge.HarnessBridgeError as exc:
                    logger.warning("harness.task.cancel_unavailable", extra={
                        "event": "harness.task.cancel_unavailable", "component": "harness",
                        "project_id": project_id, "task_id": task.id, "error_type": type(exc).__name__,
                    })
            return _task_view(current)

    @router.get("/config")
    def harness_config(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        bridge = harness_bridge.HarnessBridge()
        try:
            scope_secret_configured = bool(harness_bridge.scope_secret())
        except ValueError:
            scope_secret_configured = False
        required = os.getenv("TEST_ATELIER_HARNESS_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}
        return {
            "enabled": bool(bridge.config.base_url) and scope_secret_configured,
            "required": required,
            "scope_secret_configured": scope_secret_configured,
            "task_types": ["requirement-analysis", "test-cases", "skill-execution"],
            "result_schemas": ["requirement-analysis.v1", "test-cases.v1", "skill-execution.v1"],
            # Keep the capability list explicit.  A project skill must not be
            # treated as Harness-executable merely because it exists in the
            # registry; adding a new skill requires a versioned task contract.
            "skills": [
                {"skill_id": "requirement-analysis", "task_type": "requirement-analysis",
                 "result_schema": "requirement-analysis.v1", "confirmation": "headless-or-acp"},
                {"skill_id": "test-cases", "task_type": "test-cases",
                 "result_schema": "test-cases.v1", "confirmation": "headless-or-acp"},
                *({"skill_id": skill_id, "task_type": "skill-execution",
                   "result_schema": "skill-execution.v1", "confirmation": "headless-or-acp"}
                  for skill_id in sorted(GENERIC_SKILL_IDS)),
            ],
        }

    @router.get("/status")
    def harness_status(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Report whether the configured DeepSeek Harness Host is reachable.

        Credentials and scope secrets never leave the server.  A missing URL
        or scope secret is reported as ``unconfigured``; a configured target
        that cannot be reached is ``unavailable``; only a successful Host
        ``/health`` response is considered ``running``.
        """

        scope(project_id, user, db)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        bridge = harness_bridge.HarnessBridge()
        try:
            scope_secret_configured = bool(harness_bridge.scope_secret())
        except ValueError:
            scope_secret_configured = False
        configured = bool(bridge.config.base_url) and scope_secret_configured
        if not configured:
            return {
                "status": "unconfigured",
                "configured": False,
                "reachable": False,
                "scope_secret_configured": scope_secret_configured,
                "checked_at": checked_at,
                "message": "DeepSeek Harness 尚未配置",
            }
        try:
            health = bridge.health()
        except harness_bridge.HarnessBridgeHostUnavailable:
            return {
                "status": "unavailable",
                "configured": True,
                "reachable": False,
                "scope_secret_configured": True,
                "checked_at": checked_at,
                "message": "DeepSeek Harness Host 不可用",
            }
        except harness_bridge.HarnessBridgeError:
            return {
                "status": "unavailable",
                "configured": True,
                "reachable": False,
                "scope_secret_configured": True,
                "checked_at": checked_at,
                "message": "DeepSeek Harness 健康检查失败",
            }
        healthy = health.get("status") == "ok"
        return {
            "status": "running" if healthy else "unavailable",
            "configured": True,
            "reachable": healthy,
            "scope_secret_configured": True,
            "checked_at": checked_at,
            "message": "DeepSeek Harness 运行中" if healthy else "DeepSeek Harness Host 不可用",
        }

    @router.get("/tasks")
    def list_tasks(project_id: int, task_type: str | None = Query(default=None),
                   analysis_run_id: str | None = Query(default=None, min_length=1, max_length=36),
                   limit: int = Query(default=20, ge=1, le=100),
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
        """List project tasks, optionally finding a historical analysis beyond the recent window."""

        scope(project_id, user, db)
        query = select(HarnessTask).where(HarnessTask.project_id == project_id)
        if task_type in {"requirement-analysis", "test-cases", "skill-execution"}:
            query = query.where(HarnessTask.task_type == task_type)
        if analysis_run_id is not None:
            query = query.where(HarnessTask.analysis_run_id == analysis_run_id)
        tasks = db.scalars(query.order_by(HarnessTask.created_at.desc()).limit(limit)).all()
        return [_task_view(task) for task in tasks]

    @router.get("/tasks/{task_id}")
    def get_task(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        # A detail poll is also a bounded projector tick.  This closes the
        # browser-disconnect gap while keeping the durable Harness event log
        # as the source of truth and preserving SSE for live updates.
        _project_remote_events(task.id, user.id, project_id)
        with _session_for_validation() as projected_db:
            return _task_view(find_task(project_id, task_id, projected_db))

    @router.get("/tasks/{task_id}/session")
    def get_session_replay(project_id: int, task_id: str, session_id: str | None = Query(default=None, max_length=128), user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Return a Harness-owned Session only after the project ACL check."""

        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        provisional = f"ta-{task.id}"
        available_sessions = [str(value) for value in (task.session_ids or []) if isinstance(value, str) and value != provisional]
        if not available_sessions and task.session_id and task.session_id != provisional:
            available_sessions = [task.session_id]
        selected_session_id = session_id or (task.session_id if task.session_id != provisional else None)
        if not selected_session_id:
            raise HTTPException(409, "Harness Session 尚未创建")
        if selected_session_id not in available_sessions:
            raise HTTPException(409, "Harness Session 不属于当前任务")
        try:
            return harness_bridge.HarnessBridge().read_session(task.id, selected_session_id, current_request_id(), {
                "subject_user_id": user.id,
                "project_id": project_id,
                "task_id": task.id,
                "allowed_operation": "session.read",
            })
        except harness_bridge.HarnessBridgeUnavailable as exc:
            raise HTTPException(503, "DeepSeek Harness 尚未配置") from exc
        except harness_bridge.HarnessBridgeHTTPError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
        except harness_bridge.HarnessBridgeError as exc:
            raise HTTPException(502, "DeepSeek Harness Session 回放不可用") from exc

    @router.get("/tasks/{task_id}/result")
    def get_task_result(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Return only the validated Harness result for an authorized task."""

        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        _project_remote_events(task.id, user.id, project_id)
        with _session_for_validation() as result_db:
            task = find_task(project_id, task_id, result_db)
            return {
                "task_id": task.id,
                "project_id": task.project_id,
                "analysis_run_id": task.analysis_run_id,
                "source_document_ids": task.source_document_ids,
                "status": task.status,
                "result_schema": task.result_schema,
                "result": task.result if task.status == "completed" and isinstance(task.result, dict) else {},
                "error": task.error if isinstance(task.error, dict) else {},
            }

    @router.get("/tasks/{task_id}/report.md")
    def get_analysis_report(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Download only the original report supplied by a completed Harness task."""
        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        _project_remote_events(task.id, user.id, project_id)
        with _session_for_validation() as result_db:
            task = find_task(project_id, task_id, result_db)
            if task.task_type != "requirement-analysis" or task.status != "completed" or not isinstance(task.result, dict):
                raise HTTPException(409, "只有已完成的 Harness 需求分析可以下载报告")
            report = task.result.get("report_markdown")
            if not isinstance(report, str) or not report.lstrip().startswith("# "):
                raise HTTPException(409, "该 Harness 结果没有原始 Markdown 报告")
        return Response(report, media_type="text/markdown; charset=utf-8", headers={
            "content-disposition": f"attachment; filename*=UTF-8''Harness-analysis-{task.id}.md",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        })

    @router.get("/tasks/{task_id}/xmind")
    def get_skill_xmind(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Export only a completed raw case result using the source skill script."""
        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        _project_remote_events(task.id, user.id, project_id)
        with _session_for_validation() as result_db:
            task = find_task(project_id, task_id, result_db)
            if task.task_type != "test-cases" or task.status != "completed" or not isinstance(task.result, dict):
                raise HTTPException(409, "只有已完成的 Harness 用例结果可以导出 XMind")
            try:
                cases = skill_xmind_cases(task.result, set(task.source_document_ids or []),
                                          task_id=task.id, analysis_run_id=task.analysis_run_id)
            except SkillArtifactUnavailable as exc:
                raise HTTPException(409, str(exc)) from exc
        try:
            content = generate_skill_xmind(cases, "测试用例")
        except SkillRunnerError as exc:
            raise HTTPException(503, "项目技能 XMind 生成器不可用") from exc
        return Response(content, media_type="application/vnd.xmind.workbook", headers={
            "content-disposition": f"attachment; filename*=UTF-8''Harness-cases-{task.id}.xmind",
            "cache-control": "no-store",
        })

    @router.get("/tasks/{task_id}/events")
    def task_events(project_id: int, task_id: str, request: Request, after: int | None = Query(default=None, ge=0), user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        task = find_task(project_id, task_id, db)
        if after is None:
            last_event_id = request.headers.get("last-event-id", "")
            if last_event_id and (not last_event_id.isascii() or not last_event_id.isdecimal() or len(last_event_id) > 19):
                raise HTTPException(422, "Last-Event-ID 必须是本地事件序号")
            after = int(last_event_id) if last_event_id else 0
        events = db.scalars(select(HarnessTaskEvent).where(
            HarnessTaskEvent.task_id == task.id, HarnessTaskEvent.sequence > after,
        ).order_by(HarnessTaskEvent.sequence)).all()
        # Harness and Test Atelier each have their own event sequence. The
        # former is used only as a remote cursor; the latter stays monotonic
        # for browser reconnects and business projections.
        remote_events = db.scalars(select(HarnessTaskEvent).where(
            HarnessTaskEvent.task_id == task.id,
        ).order_by(HarnessTaskEvent.sequence)).all()
        remote_after = max((int(item.payload.get("_remote_sequence", 0))
                            for item in remote_events
                            if isinstance(item.payload, dict) and str(item.payload.get("_remote_sequence", "")).isdigit()), default=0)
        bridge = harness_bridge.HarnessBridge()
        task_id_value = task.id
        terminal = task.status in {"completed", "failed", "cancelled"}
        db.close()

        def stream() -> Iterator[str]:
            from .db import SessionLocal

            last = after
            remote_cursor = remote_after
            for item in events:
                last = max(last, item.sequence)
                yield _sse(item.event, item.payload, item.sequence)
            if not bridge.config.base_url or terminal:
                return
            try:
                for event_name, payload in bridge.stream_events(task_id_value, remote_cursor, current_request_id(), {
                    "subject_user_id": user.id,
                    "project_id": project_id,
                    "task_id": task_id_value,
                    "allowed_operation": "task.events",
                }):
                    remote_sequence = None
                    try:
                        remote_sequence = validate_remote_event(task_id_value, event_name, payload, remote_cursor)
                        if remote_sequence is None:
                            continue
                        projected_payload = {**payload, "_remote_sequence": remote_sequence}
                        project_event(task_id_value, event_name, projected_payload)
                    except (ValueError, TypeError) as exc:
                        # Protocol and completion violations fail closed just
                        # like invalid result envelopes. Never serialize the
                        # exception's raw model input into business events.
                        project_invalid_result(task_id_value, _invalid_remote_payload(task_id_value, event_name, payload, remote_sequence), exc)
                        with SessionLocal() as state_db:
                            failure = state_db.scalar(select(HarnessTaskEvent).where(
                                HarnessTaskEvent.task_id == task_id_value,
                                HarnessTaskEvent.event == "task.failed",
                                HarnessTaskEvent.sequence > last,
                            ).order_by(HarnessTaskEvent.sequence.desc()))
                        if failure is None:
                            break
                        last = failure.sequence
                        yield _sse(failure.event, failure.payload, failure.sequence)
                        break
                    with SessionLocal() as state_db:
                        persisted = state_db.scalar(select(HarnessTaskEvent).where(
                            HarnessTaskEvent.task_id == task_id_value,
                            HarnessTaskEvent.remote_sequence == remote_sequence,
                        ))
                    if persisted is None:
                        # Another projector may have consumed this event; the
                        # next reconnect will replay it from the durable log.
                        remote_cursor = max(remote_cursor, remote_sequence)
                        continue
                    previous_last = last
                    last = max(last, persisted.sequence)
                    remote_cursor = max(remote_cursor, remote_sequence)
                    if persisted.sequence > previous_last:
                        yield _sse(persisted.event, persisted.payload, persisted.sequence)
            except harness_bridge.HarnessBridgeError as exc:
                # A broken subscription is not proof that the durable Host
                # task failed. Leave its business state and local cursor intact
                # so a reconnect or background projector can resume it.
                logger.warning("harness.stream.unavailable", extra={
                    "event": "harness.stream.unavailable", "task_id": task_id_value,
                    "project_id": project_id, "error_type": type(exc).__name__,
                })
                yield "event: task.transport_error\ndata: " + json.dumps({
                    "task_id": task_id_value,
                    "error": {"code": "provider_unavailable", "message": "Harness 连接中断，正在等待恢复", "retryable": True},
                }, ensure_ascii=False) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/tasks/{task_id}/cancel", dependencies=[Depends(csrf)])
    def cancel_task(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        task = find_task(project_id, task_id, db)
        if task.status in {"completed", "failed", "cancelled"}:
            return _task_view(task)
        try:
            harness_bridge.HarnessBridge().cancel_task(task.id, current_request_id(), {
                "subject_user_id": user.id,
                "project_id": project_id,
                "task_id": task.id,
                "allowed_operation": "task.cancel",
            })
        except harness_bridge.HarnessBridgeUnavailable:
            pass
        except harness_bridge.HarnessBridgeError as exc:
            raise HTTPException(502, "DeepSeek Harness 取消失败") from exc
        append_event(task.id, "task.cancelled", {"task_id": task.id, "sequence": task.sequence + 1, "reason": "user"})
        with _session_for_validation() as final_db:
            return _task_view(final_db.get(HarnessTask, task.id))

    @router.post("/tasks/{task_id}/retry", dependencies=[Depends(csrf)])
    def retry_task(project_id: int, task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Start a fresh Harness task from persisted, authorized references.

        Retry never resends the old task's raw input.  The source document IDs
        and (for case authoring) the confirmed analysis run are revalidated by
        ``create_task`` before the new Host request is built.
        """

        scope(project_id, user, db)
        writable(user, project_id, db)
        locked = db.execute(update(Project).where(Project.id == project_id).values(name=Project.name))
        if locked.rowcount != 1:
            raise HTTPException(404, "项目不存在")
        db.expire_all()
        task = find_task(project_id, task_id, db)
        if task.status not in {"failed", "cancelled"}:
            raise HTTPException(409, "只有失败或已取消的 Harness 任务可以重试")
        if task.analysis_run_id:
            analysis = db.get(AnalysisRun, task.analysis_run_id)
            if analysis is not None and analysis.status == "deleting":
                raise HTTPException(409, "关联分析正在删除，不能重试 Harness 任务")
        previous_error = task.error if isinstance(task.error, dict) else {}
        if previous_error.get("retryable") is False:
            raise HTTPException(409, "该 Harness 失败不可重试")

        retry_payload = HarnessTaskCreate(
            task_type=task.task_type,
            task_version=task.task_version,
            source_document_ids=(list(task.source_document_ids or [])
                                 if task.task_type != "test-cases" else []),
            analysis_run_id=(task.analysis_run_id if task.task_type == "test-cases" else None),
            skill_id=task.skill_id,
            # A retry intentionally starts a new Session.  The old Session
            # remains available as an audit/replay record.
            session_id=None,
            input={},
            idempotency_key=f"retry:{task.id}:{uuid.uuid4().hex}",
        )
        created = create_task(project_id, retry_payload, user, db)
        retry_task_row = db.get(HarnessTask, created["task_id"])
        if retry_task_row is not None:
            # create_task commits before the remote Host call. Recheck under
            # the project lock so a concurrent analysis deletion cannot leave
            # a retry pointer to a task that has already been removed.
            db.execute(update(Project).where(Project.id == project_id).values(name=Project.name))
            db.expire_all()
            old_task = db.get(HarnessTask, task_id)
            old_analysis = db.get(AnalysisRun, old_task.analysis_run_id) if old_task and old_task.analysis_run_id else None
            if old_task is not None and (old_analysis is None or old_analysis.status != "deleting"):
                retry_task_row.retry_of = old_task.id
            db.commit()
            db.refresh(retry_task_row)
            return _task_view(retry_task_row)
        return created

    @router.post("/tasks/{task_id}/confirm", dependencies=[Depends(csrf)])
    def confirm_task(project_id: int, task_id: str, payload: HarnessConfirmation, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        task = find_task(project_id, task_id, db)
        if task.status != "waiting_confirmation":
            raise HTTPException(409, "任务当前不等待确认")
        try:
            response = harness_bridge.HarnessBridge().confirm_task(task.id, payload.model_dump(exclude_none=True), current_request_id(), {
                "subject_user_id": user.id,
                "project_id": project_id,
                "task_id": task.id,
                "allowed_operation": "task.confirm",
            })
        except harness_bridge.HarnessBridgeUnavailable as exc:
            raise HTTPException(503, "DeepSeek Harness 尚未配置") from exc
        except harness_bridge.HarnessBridgeHTTPError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
        except harness_bridge.HarnessBridgeError as exc:
            raise HTTPException(502, "DeepSeek Harness 确认失败") from exc
        append_event(task.id, "task.progress", {"task_id": task.id, "sequence": task.sequence + 1,
                                                  "kind": "confirmation_submitted",
                                                  "status": response.get("status", "running")})
        with _session_for_validation() as final_db:
            return _task_view(final_db.get(HarnessTask, task.id))

    return router


def _sse(event_name: str, payload: dict, sequence: int) -> str:
    return f"id: {sequence}\nevent: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
