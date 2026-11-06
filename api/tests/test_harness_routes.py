import os
import hashlib
import json

import pytest

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api import ai_routes, harness_bridge, harness_projection, harness_routes  # noqa: E402
from api.db import AiJob, AnalysisRun, CaseDraft, HarnessTask, HarnessTaskEvent, Project, ProjectSkill, SessionLocal, utcnow  # noqa: E402
from api.tests.test_ai_workflow import context  # noqa: E402,F401
from api.tests.test_projects import login  # noqa: E402


class FakeBridge:
    class _Config:
        base_url = "http://fake-harness"

    config = _Config()

    def create_task(self, payload, _request_id, _scope=None):
        self.last_payload = payload
        return {"task_id": payload["task_id"], "session_id": "session-1", "status": "running"}

    def upload_input_chunk(self, task_id, document_index, chunk_index, text, _request_id, scope):
        assert scope["allowed_operation"] == "task.input"
        self.chunks = getattr(self, "chunks", []) + [(task_id, document_index, chunk_index, text)]

    def start_staged_task(self, task_id, _request_id, scope):
        assert scope["allowed_operation"] == "task.input"
        self.started_staged = task_id
        return {"task_id": task_id, "status": "running"}

    def cancel_task(self, _task_id, _request_id, _scope=None):
        return {"status": "cancelled"}

    def delete_task(self, task_id, _request_id, scope):
        self.deleted_scopes = getattr(self, "deleted_scopes", []) + [scope]
        return {"task_id": task_id, "status": "deleted"}

    def confirm_task(self, _task_id, _payload, _request_id, _scope=None):
        return {"status": "running"}

    def read_session(self, _task_id, session_id, _request_id, _scope=None):
        return {
            "session_id": session_id,
            "header": {"type": "session", "id": session_id, "version": 4},
            "events": [{"type": "user/message", "seq": 1}],
            "event_count": 1,
            "truncated": False,
            "format": "v3",
        }

    def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
        self.stream_count = getattr(self, "stream_count", 0) + 1
        payload = {
            "task_id": task_id,
            "sequence": 3,
            "session_id": "session-1",
            "result": {
                "schema": "requirement-analysis.v1",
                "result_id": "result-1",
                "task_id": task_id,
                "session_id": "session-1",
                "source_document_ids": [self.source_id],
                "generated_at": "2026-10-23T10:00:00Z",
                "warnings": [],
                "data": {
                    "requirements": [],
                    "ambiguities": [],
                    "quality_warnings": [],
                },
            },
        }
        yield "task.result", payload
        yield "task.result", payload
        yield "task.completed", {"task_id": task_id, "sequence": 4}


class DisconnectingBridge(FakeBridge):
    """Leave the remote result behind until the next projector tick."""

    def stream_events(self, task_id, after, _request_id, _scope=None, **_kwargs):
        self.stream_count = getattr(self, "stream_count", 0) + 1
        if after < 2:
            yield "task.progress", {"task_id": task_id, "sequence": 2, "status": "running"}
            return
        payload = {
            "task_id": task_id,
            "sequence": 3,
            "session_id": "session-1",
            "result": {
                "schema": "requirement-analysis.v1",
                "result_id": "disconnect-result",
                "task_id": task_id,
                "session_id": "session-1",
                "source_document_ids": [self.source_id],
                "generated_at": "2026-10-23T10:00:00Z",
                "warnings": [],
                "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
            },
        }
        yield "task.result", payload
        yield "task.completed", {"task_id": task_id, "sequence": 4}


def test_harness_status_reports_unconfigured_without_target(context, monkeypatch):
    client, headers, root = context
    monkeypatch.delenv("TEST_ATELIER_HARNESS_URL", raising=False)
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    response = client.get(f"{root}/harness/status", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "unconfigured"
    assert response.json()["reachable"] is False


def test_harness_status_reports_running_for_healthy_host(context, monkeypatch):
    client, headers, root = context

    class HealthyBridge:
        class _Config:
            base_url = "http://fake-harness"
        config = _Config()

        def health(self):
            return {"status": "ok", "service": "test-atelier-harness-host"}

    monkeypatch.setattr(harness_bridge, "HarnessBridge", HealthyBridge)
    monkeypatch.setattr(harness_bridge, "scope_secret", lambda: "s" * 32)
    response = client.get(f"{root}/harness/status", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["configured"] is True
    assert payload["reachable"] is True


def test_new_task_rejects_caller_supplied_session_before_creating_business_rows(context):
    client, headers, root = context
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": ["source-1"], "session_id": "another-task-session",
        "idempotency_key": "invalid-session-resume",
    }, headers=headers)
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(HarnessTask).filter_by(idempotency_key="invalid-session-resume").count() == 0


def test_new_task_rejects_instruction_input_before_creating_business_rows(context):
    client, headers, root = context
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": ["source-1"], "input": {"instruction": "call another skill"},
        "idempotency_key": "invalid-instruction-input",
    }, headers=headers)
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(HarnessTask).filter_by(idempotency_key="invalid-instruction-input").count() == 0


def test_lazy_harness_skill_registration_rejects_project_deletion_race(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")}, headers=headers)
    assert uploaded.status_code == 200
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        skill = db.get(ProjectSkill, (project_id, "requirement-analysis"))
        db.delete(skill)
        db.commit()
    discover = harness_routes.discover_skills

    def mark_before_registration():
        specs = discover()
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return specs

    monkeypatch.setattr(harness_routes, "discover_skills", mark_before_registration)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "pending-skill-registration",
    }, headers=headers)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(ProjectSkill, (project_id, "requirement-analysis")) is None
        assert db.query(HarnessTask).filter_by(project_id=project_id).count() == 0


def test_completed_harness_analysis_deletes_host_tasks_before_business_history(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "analysis-delete-retention",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    analysis_id = created.json()["analysis_run_id"]
    with SessionLocal.begin() as db:
        db.get(HarnessTask, task_id).status = "completed"
        db.get(AnalysisRun, analysis_id).status = "ready"
        db.add(HarnessTask(id="linked-case-task", project_id=int(root.rsplit("/", 1)[-1]), user_id=1,
                           task_type="test-cases", task_version="v1", result_schema="test-cases.v1",
                           skill_id="test-cases", source_document_ids=[uploaded.json()["id"]],
                           analysis_run_id=analysis_id, status="completed", idempotency_key="linked-case-task",
                           result={}, error={}))

    deleted = client.delete(f"{root}/analyses/{analysis_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert {item["task_id"] for item in bridge.deleted_scopes} == {task_id, "linked-case-task"}
    assert all(item["allowed_operation"] == "task.delete" for item in bridge.deleted_scopes)
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_id) is None
        assert db.get(HarnessTask, task_id) is None
        assert db.get(HarnessTask, "linked-case-task") is None
    assert client.get(f"{root}/harness/tasks/{task_id}").status_code == 404


def test_harness_analysis_delete_survives_host_outage_and_retries(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "analysis-delete-retry",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id, analysis_id = created.json()["task_id"], created.json()["analysis_run_id"]
    assert client.delete(f"{root}/analyses/{analysis_id}", headers=headers).status_code == 409
    with SessionLocal.begin() as db:
        db.get(HarnessTask, task_id).status = "completed"
        db.get(AnalysisRun, analysis_id).status = "ready"

    def unavailable(*_args):
        raise harness_bridge.HarnessBridgeUnavailable("host down")

    monkeypatch.setattr(bridge, "delete_task", unavailable)
    failed = client.delete(f"{root}/analyses/{analysis_id}", headers=headers)
    assert failed.status_code == 503, failed.text
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_id).status == "deleting"
        assert db.get(HarnessTask, task_id) is not None
    assert client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": analysis_id,
        "idempotency_key": "blocked-during-delete",
    }, headers=headers).status_code == 409
    monkeypatch.setattr(bridge, "delete_task", FakeBridge.delete_task.__get__(bridge))
    recovered = client.delete(f"{root}/analyses/{analysis_id}", headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert len(bridge.deleted_scopes) == 1
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_id) is None
        assert db.get(HarnessTask, task_id) is None


def test_harness_analysis_delete_retries_after_partial_remote_cleanup(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "analysis-partial-cleanup",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id, analysis_id = created.json()["task_id"], created.json()["analysis_run_id"]
    other_id = f"{task_id}-case"
    with SessionLocal.begin() as db:
        db.get(HarnessTask, task_id).status = "completed"
        db.get(AnalysisRun, analysis_id).status = "ready"
        db.add(HarnessTask(id=other_id, project_id=int(root.rsplit("/", 1)[-1]), user_id=1,
                           task_type="test-cases", task_version="v1", result_schema="test-cases.v1",
                           skill_id="test-cases", source_document_ids=[uploaded.json()["id"]],
                           analysis_run_id=analysis_id, status="completed", idempotency_key="partial-case",
                           result={}, error={}))
    ordered = sorted((task_id, other_id))
    calls = []
    interrupted = False

    def flaky_delete(selected, request_id, scope):
        nonlocal interrupted
        calls.append(selected)
        if selected == ordered[1] and not interrupted:
            interrupted = True
            raise harness_bridge.HarnessBridgeUnavailable("host down")
        return FakeBridge.delete_task(bridge, selected, request_id, scope)

    monkeypatch.setattr(bridge, "delete_task", flaky_delete)
    response = client.delete(f"{root}/analyses/{analysis_id}", headers=headers)
    assert response.status_code == 503, response.text
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_id).status == "deleting"
        assert all(db.get(HarnessTask, selected) is not None for selected in ordered)
    assert client.delete(f"{root}/analyses/{analysis_id}", headers=headers).status_code == 200
    assert calls == ordered + ordered
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_id) is None
        assert all(db.get(HarnessTask, selected) is None for selected in ordered)


def test_failed_harness_task_cannot_retry_an_analysis_being_deleted(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "retry-during-analysis-delete",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id, analysis_id = created.json()["task_id"], created.json()["analysis_run_id"]
    with SessionLocal.begin() as db:
        db.get(HarnessTask, task_id).status = "failed"
        db.get(AnalysisRun, analysis_id).status = "deleting"
    retry = client.post(f"{root}/harness/tasks/{task_id}/retry", headers=headers)
    assert retry.status_code == 409 and "正在删除" in retry.json()["detail"]
    with SessionLocal() as db:
        assert db.query(HarnessTask).filter_by(project_id=int(root.rsplit("/", 1)[-1])).count() == 1


def test_decision_rejects_update_committed_after_initial_version_read(context, monkeypatch):
    client, headers, root = context
    run_id = "decision-conflict-during-write"
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal.begin() as db:
        db.add(AnalysisRun(id=run_id, project_id=project_id, user_id=1,
                           provider="harness", status="ready", source_ids=[],
                           result={"ambiguities": [{"id": "AMB-1", "decision": ""}]}))

    real_update = ai_routes.update
    injected = False

    def interleave_update(model):
        nonlocal injected
        if model is AnalysisRun and not injected:
            injected = True
            with SessionLocal.begin() as db:
                other = db.get(AnalysisRun, run_id)
                other.result = {**other.result, "ambiguities": [{"id": "AMB-1", "decision": "earlier decision"}]}
                other.version += 1
        return real_update(model)

    monkeypatch.setattr(ai_routes, "update", interleave_update)
    response = client.patch(f"{root}/analyses/{run_id}/ambiguities/AMB-1",
                            json={"version": 1, "decision": "late decision"}, headers=headers)
    assert injected and response.status_code == 409, response.text
    with SessionLocal() as db:
        run = db.get(AnalysisRun, run_id)
        assert run.version == 2
        assert run.result["ambiguities"][0]["decision"] == "earlier decision"


def test_case_prompt_uses_confirmed_decision_without_mutating_harness_result(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200
    source_id = uploaded.json()["id"]
    original = {
        "schema": "requirement-analysis.v1", "result_id": "confirmed-result",
        "task_id": "confirmed-task", "session_id": "confirmed-session",
        "source_document_ids": [source_id], "generated_at": "2026-10-23T10:00:00Z", "warnings": [],
        "report_markdown": "# Login analysis\n\nHarness original report",
        "data": {
            "requirements": [{"id": "REQ-1", "title": "Login", "description": "Valid login", "priority": "high",
                              "source_refs": [{"document_id": source_id, "locator": "body"}],
                              "acceptance_criteria": [{"id": "AC-1", "text": "Home",
                                                       "source_refs": [{"document_id": source_id, "locator": "body"}]}]}],
            "ambiguities": [{"id": "AMB-1", "question": "What happens on failure?", "severity": "blocking",
                             "related_requirement_ids": ["REQ-1"],
                             "source_refs": [{"document_id": source_id, "locator": "body"}], "resolution": None}],
            "quality_warnings": [],
        },
    }
    with SessionLocal() as db:
        db.add(AnalysisRun(id="confirmed-case-input", project_id=1, user_id=1,
                           provider="harness", status="ready", source_ids=[source_id],
                           result={"harness_result": original,
                                   "report_markdown": original["report_markdown"],
                                   "requirements": [{"id": "REQ-1", "title": "Login", "summary": "Valid login",
                                                     "source_ids": [source_id], "acceptance": ["Home"], "methods": []}],
                                   "risks": [],
                                   "ambiguities": [{"id": "AMB-1", "description": "What happens on failure?",
                                                    "critical": True, "decision": ""}]}))
        db.commit()
    # A damaged compatibility view must not erase a blocking question present
    # in the archived Harness output and let case generation bypass review.
    with SessionLocal() as db:
        run_row = db.get(AnalysisRun, "confirmed-case-input")
        pending = run_row.result["ambiguities"]
        run_row.result = {**run_row.result, "ambiguities": []}
        db.commit()
    unconfirmed = client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": "confirmed-case-input",
        "idempotency_key": "missing-human-confirmation",
    }, headers=headers)
    assert unconfirmed.status_code == 409
    with SessionLocal() as db:
        run_row = db.get(AnalysisRun, "confirmed-case-input")
        run_row.result = {**run_row.result, "ambiguities": pending}
        db.commit()
    run = client.get(f"{root}/analyses/confirmed-case-input").json()
    confirmed = client.patch(f"{root}/analyses/confirmed-case-input/ambiguities/AMB-1",
                             json={"version": run["version"], "decision": "锁定账号并提示重试"}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["result"]["report_markdown"] == original["report_markdown"]
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": "confirmed-case-input",
        "idempotency_key": "confirmed-case-context",
    }, headers=headers)
    assert created.status_code == 200, created.text
    context_payload = bridge.last_payload["analysis"]
    assert context_payload["analysis_version"] == confirmed.json()["version"]
    with SessionLocal() as db:
        event = db.query(HarnessTaskEvent).filter_by(task_id=created.json()["task_id"], event="task.created").one()
        assert event.payload["analysis_version"] == context_payload["analysis_version"]
        assert event.payload["analysis_sha256"] == harness_routes.case_context_sha256(context_payload)
    assert context_payload["requirements"][0]["acceptance_criteria"][0]["id"] == "AC-1"
    assert context_payload["ambiguities"][0]["resolution"] == "锁定账号并提示重试"
    assert "report_markdown" not in context_payload and "harness_result" not in context_payload
    assert bridge.last_payload["source_documents"] == [{"id": source_id, "filename": "r.md", "text": ""}]
    with SessionLocal() as db:
        assert db.get(AnalysisRun, "confirmed-case-input").result["harness_result"] == original
    revised = client.patch(f"{root}/analyses/confirmed-case-input/ambiguities/AMB-1",
                           json={"version": confirmed.json()["version"], "decision": "改为通知管理员"}, headers=headers)
    assert revised.status_code == 200, revised.text
    assert revised.json()["version"] > context_payload["analysis_version"]
    case_result = {
        "schema": "test-cases.v1", "result_id": "stale-decision-case",
        "task_id": created.json()["task_id"], "session_id": "session-1",
        "source_document_ids": [source_id], "analysis_run_id": "confirmed-case-input",
        "generated_at": "2026-10-23T10:00:00Z", "warnings": [], "data": {"cases": []},
    }
    with pytest.raises(ValueError, match="analysis context changed"):
        harness_projection.project_event(created.json()["task_id"], "task.result", {
            "task_id": created.json()["task_id"], "session_id": "session-1",
            "result": case_result, "_remote_sequence": 2,
        })
    with SessionLocal() as db:
        assert db.query(CaseDraft).filter_by(analysis_id="confirmed-case-input").count() == 0


@pytest.mark.parametrize("case_id,provider,original", [
    ("legacy-provider", "remote", "valid"),
    ("missing-raw", "harness", None),
    ("incomplete-raw", "harness", {"schema": "requirement-analysis.v1", "data": {"requirements": []}}),
    ("wrong-schema", "harness", {"schema": "test-cases.v1", "data": {"requirements": []}}),
])
def test_cases_reject_non_harness_or_invalid_analysis_before_creating_task(
    context, monkeypatch, case_id, provider, original,
):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    source_id = uploaded.json()["id"]
    if original == "valid":
        original = {
            "schema": "requirement-analysis.v1", "result_id": "raw-result",
            "task_id": "raw-task", "session_id": "raw-session",
            "source_document_ids": [source_id], "generated_at": "2026-10-23T10:00:00Z",
            "warnings": [], "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
        }
    project_id = int(root.rsplit("/", 1)[-1])
    run_id = f"invalid-case-input-{case_id}"
    with SessionLocal.begin() as db:
        db.add(AnalysisRun(id=run_id, project_id=project_id, user_id=1,
                           provider=provider, status="ready", source_ids=[source_id],
                           result={"harness_result": original, "requirements": [{"id": "REQ-1"}],
                                   "ambiguities": []}))

    def unexpected_bridge():
        raise AssertionError("rejected input must not reach the Host")

    monkeypatch.setattr(harness_bridge, "HarnessBridge", unexpected_bridge)
    key = f"invalid-{case_id}"
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": run_id,
        "idempotency_key": key,
    }, headers=headers)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.query(HarnessTask).filter_by(project_id=project_id, idempotency_key=key).count() == 0
        assert db.get(AnalysisRun, run_id).status == "ready"


@pytest.mark.parametrize("consumer", ["events", "detail", "projector"])
def test_changed_analysis_after_staged_case_result_fails_closed_for_all_consumers(context, monkeypatch, consumer):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    source_id = uploaded.json()["id"]
    project_id = int(root.rsplit("/", 1)[-1])
    run_id = "case-revised-during-completion"
    with SessionLocal.begin() as db:
        db.add(AnalysisRun(id=run_id, project_id=project_id, user_id=1,
                           provider="harness", status="ready", source_ids=[source_id],
                           result={"harness_result": {
                               "schema": "requirement-analysis.v1", "result_id": "revised-analysis",
                               "task_id": "revised-analysis-task", "session_id": "revised-session",
                               "source_document_ids": [source_id], "generated_at": "2026-10-23T10:00:00Z",
                               "warnings": [], "data": {"requirements": [], "ambiguities": [{
                                   "id": "AMB-1", "question": "Login policy?", "severity": "blocking",
                                   "related_requirement_ids": [], "source_refs": [{"document_id": source_id, "locator": "1"}],
                                   "resolution": None,
                               }], "quality_warnings": []}},
                               "requirements": [], "ambiguities": [
                                   {"id": "AMB-1", "description": "Login policy?", "critical": True,
                                    "decision": "Use first policy"}], "risks": []}))

    class CompletionOnlyBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.completed", {"task_id": task_id, "sequence": 3}

    bridge = CompletionOnlyBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": run_id,
        "idempotency_key": f"revised-{consumer}",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    assert harness_projection.project_event(task_id, "task.result", {
        "task_id": task_id, "session_id": "session-1", "_remote_sequence": 2,
        "result": {"schema": "test-cases.v1", "result_id": "stale-case-result",
                   "task_id": task_id, "session_id": "session-1", "source_document_ids": [source_id],
                   "analysis_run_id": run_id, "generated_at": "2026-10-23T10:01:00Z",
                   "warnings": [], "data": {"cases": []}},
    })
    updated = client.patch(f"{root}/analyses/{run_id}/ambiguities/AMB-1",
                           json={"version": 1, "decision": "Use revised policy"}, headers=headers)
    assert updated.status_code == 200, updated.text
    if consumer == "events":
        response = client.get(f"{root}/harness/tasks/{task_id}/events?after=3")
        assert response.status_code == 200
        assert "task.failed" in response.text and "task.completed" not in response.text
        assert "stale-case-result" not in response.text
    elif consumer == "detail":
        response = client.get(f"{root}/harness/tasks/{task_id}")
        assert response.status_code == 200 and response.json()["status"] == "failed"
    else:
        from api import harness_projector

        with SessionLocal.begin() as db:
            db.get(HarnessTask, task_id).projector_owner = harness_projector.WORKER_ID
        assert harness_projector.project_task(task_id, bridge_factory=lambda: bridge) == 0

    result = client.get(f"{root}/harness/tasks/{task_id}/result")
    assert result.status_code == 200 and result.json()["status"] == "failed"
    assert result.json()["result"] == {}
    assert result.json()["error"]["code"] == "analysis_changed"
    assert result.json()["error"]["retryable"] is True
    assert "stale-case-result" not in str(result.json()["error"])
    with SessionLocal() as db:
        assert db.query(CaseDraft).filter_by(analysis_id=run_id).count() == 0
        assert db.get(AnalysisRun, run_id).status == "ready"
        assert db.get(HarnessTask, task_id).status == "failed"
    if consumer == "events":
        retried = client.post(f"{root}/harness/tasks/{task_id}/retry", headers=headers)
        assert retried.status_code == 200, retried.text
        assert retried.json()["task_id"] != task_id
        assert bridge.last_payload["analysis"]["analysis_version"] == updated.json()["version"]
        assert bridge.last_payload["analysis"]["ambiguities"][0]["resolution"] == "Use revised policy"


def test_analysis_report_download_requires_completed_original_and_project_scope(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200
    source_id = uploaded.json()["id"]
    bridge = FakeBridge()
    bridge.source_id = source_id
    bridge.stream_events = lambda *_args, **_kwargs: iter(())
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [source_id], "idempotency_key": "original-report",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    endpoint = f"{root}/harness/tasks/{task_id}/report.md"
    assert client.get(endpoint).status_code == 409
    report = "\n# Login analysis\n\nModel-authored report\n"
    _, payload = next(FakeBridge.stream_events(bridge, task_id, 0, "test"))
    payload["result"]["report_markdown"] = report
    assert harness_projection.project_event(task_id, "task.progress", {
        "task_id": task_id, "kind": "session_bound", "session_id": "session-1", "_remote_sequence": 2,
    })
    assert harness_projection.project_event(task_id, "task.result", {**payload, "_remote_sequence": 3})
    assert client.get(endpoint).status_code == 409
    assert harness_projection.project_event(task_id, "task.completed", {"task_id": task_id, "_remote_sequence": 4})
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.text == report
    assert response.headers["content-disposition"].endswith(f"Harness-analysis-{task_id}.md")
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"{root}/harness/tasks/{task_id}/result").json()["result"]["report_markdown"] == report
    saved = client.get(f"{root}/analyses/{created.json()['analysis_run_id']}").json()
    assert saved["result"]["report_markdown"] == report
    edit = client.patch(f"{root}/analyses/{saved['id']}/report", json={
        "version": saved["version"], "report_markdown": "# Rewritten by Test Atelier",
    }, headers=headers)
    assert edit.status_code == 409
    assert client.get(f"{root}/analyses/{saved['id']}").json()["result"]["report_markdown"] == report
    another = client.post("/api/v1/projects", json={"name": "Report outsider"}, headers=headers)
    assert another.status_code == 200
    assert client.get(f"/api/v1/projects/{another.json()['id']}/harness/tasks/{task_id}/report.md").status_code == 404


def test_analysis_report_download_rejects_historical_results_without_report(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    bridge = FakeBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "old-no-report",
    }, headers=headers)
    assert created.status_code == 200
    assert client.get(f"{root}/harness/tasks/{created.json()['task_id']}/report.md").status_code == 409


def test_harness_task_projection_and_reconnect(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "same-task",
    }, headers=headers)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "running"
    assert task["source_document_ids"] == [bridge.source_id]
    repeated = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "same-task",
    }, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["task_id"] == task["task_id"]

    events = client.get(f"{root}/harness/tasks/{task['task_id']}/events?after=2")
    assert events.status_code == 200
    assert "task.result" in events.text
    detail = client.get(f"{root}/harness/tasks/{task['task_id']}")
    assert detail.json()["sequence"] == 4
    replay = client.get(f"{root}/harness/tasks/{task['task_id']}/events?after=0")
    assert replay.status_code == 200
    assert replay.text.count("task.result") == 1
    assert "task.completed" in replay.text
    assert bridge.stream_count == 1
    with SessionLocal() as db:
        run = db.get(AnalysisRun, task["task_id"])
        assert run is None
        task_run = db.query(AnalysisRun).filter(AnalysisRun.provider == "harness").one()
        assert task_run.status == "ready"
        assert db.query(CaseDraft).filter(CaseDraft.analysis_id == task_run.id).count() == 0


@pytest.mark.parametrize("terminal", ["task.completed", "task.failed", "task.cancelled"])
def test_result_endpoint_requires_success_after_staged_result(context, monkeypatch, terminal):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class QuietBridge(FakeBridge):
        def stream_events(self, *_args, **_kwargs):
            return iter(())

    bridge = QuietBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "staged-result-boundary",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    assert harness_projection.project_event(task_id, "task.progress", {
        "task_id": task_id, "kind": "session_bound", "session_id": "session-1", "_remote_sequence": 2,
    })
    _, payload = next(FakeBridge.stream_events(bridge, task_id, 0, "test"))
    assert harness_projection.project_event(task_id, "task.result", {**payload, "_remote_sequence": 3})
    endpoint = f"{root}/harness/tasks/{task_id}/result"
    pending = client.get(endpoint)
    assert pending.status_code == 200 and pending.json()["result"] == {}
    with SessionLocal() as db:
        assert db.get(HarnessTask, task_id).result["result_id"] == "result-1"
    assert harness_projection.project_event(task_id, terminal, {"task_id": task_id, "_remote_sequence": 4})
    final = client.get(endpoint)
    assert final.status_code == 200
    assert final.json()["status"] == terminal.removeprefix("task.")
    assert (final.json()["result"].get("result_id") == "result-1") == (terminal == "task.completed")


def test_missing_source_skill_report_fails_analysis_without_projecting_a_result(context, monkeypatch):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200

    class MissingReportBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.failed", {"task_id": task_id, "sequence": 2, "error": {
                "code": "report_missing", "message": "Harness 需求分析未返回可交付的 Markdown 报告",
            }}

    bridge = MissingReportBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "report-missing-task",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    result = client.get(f"{root}/harness/tasks/{task_id}/result")
    assert result.status_code == 200
    assert result.json()["status"] == "failed" and result.json()["result"] == {}
    assert result.json()["error"]["code"] == "report_missing"
    assert client.get(f"{root}/harness/tasks/{task_id}/report.md").status_code == 409
    with SessionLocal() as db:
        assert db.get(AnalysisRun, created.json()["analysis_run_id"]).status == "failed"


def test_failed_harness_task_can_retry_from_persisted_source_ids(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "retry-source-task",
    }, headers=headers)
    assert created.status_code == 200
    original = created.json()
    with SessionLocal() as db:
        task = db.get(HarnessTask, original["task_id"])
        task.status = "failed"
        task.error = {"code": "provider_unavailable", "message": "temporary", "retryable": True}
        db.commit()

    retried = client.post(f"{root}/harness/tasks/{original['task_id']}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    retry_view = retried.json()
    assert retry_view["task_id"] != original["task_id"]
    assert retry_view["retry_of"] == original["task_id"]
    assert retry_view["source_document_ids"] == [bridge.source_id]
    assert retry_view["status"] == "running"


def test_failed_case_task_retry_does_not_invalidate_analysis_run(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    analysis_response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "retry-case-analysis",
    }, headers=headers)
    assert analysis_response.status_code == 200
    analysis_task_id = analysis_response.json()["task_id"]
    # Consume the fake result so the referenced analysis run is confirmed.
    client.get(f"{root}/harness/tasks/{analysis_task_id}/events?after=0", headers=headers)
    with SessionLocal() as db:
        analysis_run = db.query(AnalysisRun).filter(AnalysisRun.provider == "harness").one()
        assert analysis_run.status == "ready"
        case_response = client.post(f"{root}/harness/tasks", json={
            "task_type": "test-cases",
            "skill_id": "test-cases",
            "analysis_run_id": analysis_run.id,
            "idempotency_key": "retry-case-task",
        }, headers=headers)
        assert case_response.status_code == 200, case_response.text
        case_task_id = case_response.json()["task_id"]
        case_task = db.get(HarnessTask, case_task_id)
        case_task.status = "failed"
        case_task.error = {"code": "provider_unavailable", "message": "temporary", "retryable": True}
        db.commit()
    retried = client.post(f"{root}/harness/tasks/{case_task_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["task_type"] == "test-cases"
    assert retried.json()["retry_of"] == case_task_id
    with SessionLocal() as db:
        assert db.get(AnalysisRun, analysis_run.id).status == "ready"


def test_task_detail_projects_remote_result_after_browser_disconnect(context, monkeypatch):
    """A detail poll must catch up the durable Host log after SSE disconnects."""
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = DisconnectingBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "disconnect-projector",
    }, headers=headers)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]

    # The first SSE consumer disconnects after receiving only a progress event.
    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=0")
    assert events.status_code == 200
    assert "task.progress" in events.text
    assert "task.result" not in events.text

    # No browser stream is required now: a normal detail refresh performs one
    # bounded catch-up and projects both the result and terminal event.
    detail = client.get(f"{root}/harness/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["sequence"] == 5
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "completed"
        run = db.query(AnalysisRun).filter(AnalysisRun.provider == "harness").one()
        assert run.status == "ready"
        assert task.result["result_id"] == "disconnect-result"
    assert bridge.stream_count == 2


def test_transient_stream_disconnect_keeps_task_recoverable(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class FlakyBridge(FakeBridge):
        def stream_events(self, task_id, after, _request_id, _scope=None, **_kwargs):
            self.stream_count = getattr(self, "stream_count", 0) + 1
            if self.stream_count == 1:
                yield "task.progress", {"task_id": task_id, "sequence": 2, "kind": "text", "text": "working"}
                raise harness_bridge.HarnessBridgeUnavailable("temporary Host outage")
            yield from super().stream_events(task_id, after, _request_id, _scope, **_kwargs)

    bridge = FlakyBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "recover-transport",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    interrupted = client.get(f"{root}/harness/tasks/{task_id}/events?after=0")
    assert interrupted.status_code == 200
    assert "task.transport_error" in interrupted.text
    assert "task.failed" not in interrupted.text
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "running"
        assert task.sequence == 3
        assert db.query(HarnessTaskEvent).filter(HarnessTaskEvent.task_id == task_id).count() == 3

    resumed = client.get(f"{root}/harness/tasks/{task_id}/events?after=3")
    assert resumed.status_code == 200
    assert "task.result" in resumed.text
    assert "task.completed" in resumed.text
    assert "task.transport_error" not in resumed.text
    with SessionLocal() as db:
        assert db.get(HarnessTask, task_id).status == "completed"
        assert db.query(HarnessTaskEvent).filter(HarnessTaskEvent.task_id == task_id).count() == 5

    # Native EventSource uses Last-Event-ID; the explicit after query wins if
    # both are provided by different clients during a rolling upgrade.
    header_replay = client.get(f"{root}/harness/tasks/{task_id}/events", headers={"Last-Event-ID": "3"})
    assert header_replay.status_code == 200
    assert "task.created" not in header_replay.text
    assert "task.result" in header_replay.text
    query_replay = client.get(f"{root}/harness/tasks/{task_id}/events?after=0", headers={"Last-Event-ID": "3"})
    assert "task.created" in query_replay.text
    invalid_cursor = client.get(f"{root}/harness/tasks/{task_id}/events", headers={"Last-Event-ID": "untrusted"})
    assert invalid_cursor.status_code == 422


def test_harness_completion_without_result_fails_closed_over_sse(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class MissingResultBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.completed", {"task_id": task_id, "sequence": 2}

    bridge = MissingResultBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "no-result-task",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=0")
    assert events.status_code == 200
    assert "task.failed" in events.text
    assert "task.completed" not in events.text
    with SessionLocal() as db:
        assert db.get(HarnessTask, task_id).status == "failed"
        assert db.get(AnalysisRun, created.json()["analysis_run_id"]).status == "failed"


@pytest.mark.parametrize("consumer", ["events", "detail"])
@pytest.mark.parametrize("malformation", ["unknown", "foreign", "missing_sequence", "string_sequence", "bool_sequence", "unsafe_sequence"])
def test_remote_protocol_violation_fails_closed_without_advancing_cursor(context, monkeypatch, consumer, malformation):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class MalformedBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            payload = {"task_id": task_id, "sequence": 3, "session_id": "foreign-session", "text": "secret-payload"}
            event_name = "task.progress"
            if malformation == "unknown":
                event_name = "task.secret"
            elif malformation == "foreign":
                payload["task_id"] = "other-task"
            elif malformation == "missing_sequence":
                del payload["sequence"]
            elif malformation == "string_sequence":
                payload["sequence"] = "3"
            elif malformation == "bool_sequence":
                payload["sequence"] = True
            elif malformation == "unsafe_sequence":
                payload["sequence"] = 2**53
            yield event_name, payload
            yield from super().stream_events(task_id, 3, _request_id, _scope, **_kwargs)

    bridge = MalformedBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": f"protocol-{consumer}-{malformation}",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    path = f"{root}/harness/tasks/{task_id}"
    response = client.get(f"{path}/events?after=0" if consumer == "events" else path)
    assert response.status_code == 200
    if consumer == "events":
        assert "task.failed" in response.text
        assert "task.completed" not in response.text
        assert "secret-payload" not in response.text
    else:
        assert response.json()["status"] == "failed"
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "failed"
        assert task.session_id == "session-1"
        assert task.result == {}
        assert db.get(AnalysisRun, task.analysis_run_id).status == "failed"
        events = db.query(HarnessTaskEvent).filter(HarnessTaskEvent.task_id == task_id).order_by(HarnessTaskEvent.sequence).all()
        assert [item.event for item in events] == ["task.created", "task.started", "task.failed"]
        assert [item.remote_sequence for item in events] == [None, 1, None]
        assert "secret-payload" not in str(task.error)


def test_harness_bridge_is_disabled_by_default(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    monkeypatch.delenv("TEST_ATELIER_HARNESS_URL", raising=False)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [upload.json()["id"]],
        "idempotency_key": "disabled-task",
    }, headers=headers)
    assert response.status_code == 503


def test_harness_config_exposes_explicit_only_mode(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_URL", "http://fake-harness")
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", "test-scope-secret-32-characters-long")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_ONLY", "1")
    response = client.get(f"{root}/harness/config")
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["required"] is True
    capabilities = response.json()["skills"]
    assert {item["skill_id"] for item in capabilities} == {
        "requirement-analysis", "test-cases", "api-parsing", "dev-doc-analysis", "figma-to-prd",
    }
    assert all(item["result_schema"].endswith(".v1") for item in capabilities)


def test_harness_config_only_mode_is_false_by_default(context, monkeypatch):
    client, headers, root = context
    monkeypatch.delenv("TEST_ATELIER_HARNESS_ONLY", raising=False)
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    response = client.get(f"{root}/harness/config")
    assert response.status_code == 200
    assert response.json()["required"] is False


def test_generic_skill_task_uses_authorized_source_document(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("api.md", b"GET /health\n200")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "skill-execution",
        "skill_id": "api-parsing",
        "source_document_ids": [bridge.source_id],
        "input": {"language": "zh-CN"},
        "idempotency_key": "generic-route-task",
    }, headers=headers)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["task_type"] == "skill-execution"
    assert task["skill_id"] == "api-parsing"
    assert task["result_schema"] == "skill-execution.v1"


@pytest.mark.parametrize("filename,body", [
    ("openapi.json", b'{"openapi":"3.1.0","paths":{"/health":{"get":{}}}}'),
    ("postman.json", b'{"info":{"name":"collection"},"item":[]}'),
    ("openapi.yaml", b"openapi: 3.1.0\npaths:\n  /health:\n    get: {}\n"),
])
def test_api_spec_upload_reaches_authorized_harness_skill_as_source(context, monkeypatch, filename, body):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": (filename, body)}, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    source_id = uploaded.json()["id"]
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "skill-execution",
        "skill_id": "api-parsing",
        "source_document_ids": [source_id],
        "idempotency_key": f"api-spec-{filename}",
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert bridge.last_payload["source_document_ids"] == [source_id]
    assert bridge.last_payload["source_documents"] == [{
        "id": source_id, "filename": filename, "text": body.decode("utf-8"),
    }]


def test_invalid_api_spec_is_rejected_before_harness_dispatch(context, monkeypatch):
    client, headers, root = context
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    rejected = client.post(f"{root}/documents", files={"file": ("openapi.json", b"\xff")}, headers=headers)
    assert rejected.status_code == 422
    assert not hasattr(bridge, "last_payload")


def test_generic_skill_result_is_read_only_and_acl_scoped(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("api.md", b"GET /health\n200")}, headers=headers)
    assert upload.status_code == 200

    class GenericBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.result", {"task_id": task_id, "sequence": 3, "session_id": "session-1", "result": {
                "schema": "skill-execution.v1", "result_id": "generic-result", "task_id": task_id,
                "session_id": "session-1", "skill_id": "api-parsing", "source_document_ids": [self.source_id],
                "generated_at": "2026-10-24T00:00:00Z", "warnings": [], "data": {"output": "GET /health"},
            }}
            yield "task.completed", {"task_id": task_id, "sequence": 4}

    bridge = GenericBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "skill-execution", "skill_id": "api-parsing",
        "source_document_ids": [bridge.source_id], "idempotency_key": "generic-result-task",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=2")
    assert events.status_code == 200
    result = client.get(f"{root}/harness/tasks/{task_id}/result")
    assert result.status_code == 200
    assert result.json()["project_id"] == int(root.rsplit("/", 1)[-1])
    assert result.json()["source_document_ids"] == [bridge.source_id]
    assert result.json()["analysis_run_id"] is None
    assert result.json()["result"]["schema"] == "skill-execution.v1"
    assert result.json()["result"]["data"]["output"] == "GET /health"


def test_empty_generic_skill_result_is_not_delivered_as_completed(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("api.md", b"GET /health\n200")}, headers=headers)
    assert upload.status_code == 200

    class EmptySkillBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.result", {"task_id": task_id, "sequence": 3, "session_id": "session-1", "result": {
                "schema": "skill-execution.v1", "result_id": "empty-skill", "task_id": task_id,
                "session_id": "session-1", "skill_id": "api-parsing", "source_document_ids": [self.source_id],
                "generated_at": "2026-10-26T00:00:00Z", "warnings": [], "data": {},
            }}
            yield "task.completed", {"task_id": task_id, "sequence": 4}

    bridge = EmptySkillBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "skill-execution", "skill_id": "api-parsing",
        "source_document_ids": [bridge.source_id], "idempotency_key": "empty-skill-result",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=2")
    assert events.status_code == 200
    assert "task.failed" in events.text and "task.completed" not in events.text
    result = client.get(f"{root}/harness/tasks/{task_id}/result").json()
    assert result["status"] == "failed" and result["error"]["code"] == "schema_invalid"
    assert result["result"] == {}


def test_invalid_harness_result_fails_without_persisting_raw_output(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class InvalidBridge(FakeBridge):
        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.result", {
                "task_id": task_id,
                "sequence": 3,
                "session_id": "session-1",
                "result": {
                    "schema": "requirement-analysis.v0",
                    "result_id": "sensitive-source-text-result-id",
                    "task_id": task_id,
                    "session_id": "sensitive-source-text-session-id",
                    "source_document_ids": [self.source_id],
                    "data": {"output": "sensitive-source-text" * 1000},
                },
            }

    bridge = InvalidBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "invalid-result-route",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=0")
    assert events.status_code == 200
    assert "schema_invalid" in events.text
    assert "sensitive-source-text" not in events.text
    result = client.get(f"{root}/harness/tasks/{task_id}/result").json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "schema_invalid"
    assert "sensitive-source-text" not in str(result)


def test_harness_confirmation_round_trip_is_project_scoped(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class ConfirmationBridge(FakeBridge):
        def create_task(self, payload, _request_id, _scope=None):
            return {"task_id": payload["task_id"], "session_id": "session-confirm", "status": "running"}

        def stream_events(self, task_id, _after, _request_id, _scope=None, **_kwargs):
            yield "task.confirmation_required", {
                "task_id": task_id,
                "sequence": 3,
                "session_id": "session-confirm",
                "confirmation": {"id": "confirm-1", "prompt": "允许继续"},
            }

        def confirm_task(self, _task_id, payload, _request_id, _scope=None):
            self.confirmed_payload = payload
            return {"status": "running"}

    bridge = ConfirmationBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "confirmation-round-trip",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    events = client.get(f"{root}/harness/tasks/{task_id}/events?after=2")
    assert events.status_code == 200
    assert "task.confirmation_required" in events.text
    detail = client.get(f"{root}/harness/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "waiting_confirmation"

    confirmed = client.post(f"{root}/harness/tasks/{task_id}/confirm", json={
        "confirmation_id": "confirm-1",
        "decision": "approve",
        "comment": "仅 Web 适用",
    }, headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "running"
    assert bridge.confirmed_payload == {"confirmation_id": "confirm-1", "decision": "approve", "comment": "仅 Web 适用"}

    second = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "ambiguity-choice-round-trip",
    }, headers=headers)
    assert second.status_code == 200
    second_id = second.json()["task_id"]
    assert client.get(f"{root}/harness/tasks/{second_id}/events?after=2").status_code == 200
    invalid = client.post(f"{root}/harness/tasks/{second_id}/confirm", json={
        "confirmation_id": "confirm-1", "decision": "approve", "ambiguity_choice": "provide_explanation",
        "comment": "  ",
    }, headers=headers)
    assert invalid.status_code == 422
    choice = client.post(f"{root}/harness/tasks/{second_id}/confirm", json={
        "confirmation_id": "confirm-1", "decision": "approve", "ambiguity_choice": "mark_pending",
    }, headers=headers)
    assert choice.status_code == 200
    assert bridge.confirmed_payload == {"confirmation_id": "confirm-1", "decision": "approve",
                                        "ambiguity_choice": "mark_pending"}


def test_legacy_analysis_endpoints_are_blocked_in_harness_only_mode(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_ONLY", "1")
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200
    response = client.post(f"{root}/analyses", json={"source_ids": [upload.json()["id"]]}, headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize("harness_only", [True, False])
def test_legacy_case_generation_cannot_change_harness_analysis_provenance(context, monkeypatch, harness_only):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_ONLY", "1" if harness_only else "0")
    with SessionLocal() as db:
        project_id = int(root.rsplit("/", 1)[-1])
        run = AnalysisRun(
            id="legacy-harness-only-run",
            project_id=project_id,
            user_id=1,
            source_ids=[],
            status="ready",
            provider="harness",
            result={"requirements": [], "ambiguities": []},
        )
        db.add(run)
        db.commit()
    response = client.post(f"{root}/analyses/{run.id}/generate", json={"provider": "remote"}, headers=headers)
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.get(AnalysisRun, run.id).provider == "harness"
        assert db.query(AiJob).filter(AiJob.analysis_id == run.id).count() == 0


def test_harness_stages_oversized_task_input_in_bounded_verified_chunks(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "65536")
    upload = client.post(f"{root}/documents", files={"file": ("large.md", ("需求\n" + "x" * 70000).encode())}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "oversized-task",
    }, headers=headers)
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    assert bridge.started_staged == task_id
    manifest = bridge.last_payload["input_manifest"][0]
    chunks = bridge.chunks
    assert bridge.last_payload["source_documents"][0]["text"] == ""
    assert len(chunks) == manifest["chunks"] > 1
    assert [item[2] for item in chunks] == list(range(len(chunks)))
    assert all(len(item[3].encode("utf-8")) <= 1024 * 1024 for item in chunks)
    restored = "".join(item[3] for item in chunks)
    assert restored == "需求\n" + "x" * 70000
    assert hashlib.sha256(restored.encode("utf-8")).hexdigest() == manifest["sha256"]


def test_harness_stages_source_before_regular_window_budget_even_under_http_limit(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", str(8 * 1024 * 1024))
    document = ("需求\n" + "x" * 500000).encode()
    upload = client.post(f"{root}/documents", files={"file": ("middle-sized.md", document)}, headers=headers)
    assert upload.status_code == 200, upload.text
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "window-budget-before-http-limit",
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert bridge.started_staged == response.json()["task_id"]
    assert bridge.last_payload["source_documents"][0]["text"] == ""
    assert bridge.last_payload["window_policy"] == {"chunk_chars": 12000, "overlap_chars": 400, "max_chunks": 128}
    manifest = bridge.last_payload["input_manifest"][0]
    assert manifest["bytes"] == len(document)
    assert "".join(part[3] for part in bridge.chunks) == document.decode()


def test_harness_normal_window_budget_accounts_for_line_overlap_and_document_count(monkeypatch):
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_CHARS", "12000")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", "400")
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_CHUNKS", "128")
    safe = (128 - 2) * (12000 // 3 + 1 - 400)
    assert harness_routes._needs_staged_source_windows(safe, 2) is False
    assert harness_routes._needs_staged_source_windows(safe + 1, 2) is True
    monkeypatch.setenv("TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS", "6000")
    assert harness_routes._needs_staged_source_windows(128, 2) is True


def test_regular_harness_task_carries_the_same_window_policy_used_for_routing(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("small.md", b"# Login")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [upload.json()["id"]], "idempotency_key": "normal-window-policy",
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert bridge.last_payload["window_policy"] == {"chunk_chars": 12000, "overlap_chars": 400, "max_chunks": 128}
    assert "input_manifest" not in bridge.last_payload


def test_generic_skill_stages_oversized_source_without_promoting_business_results(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "65536")
    document = b"GET /health\n" * 7000
    upload = client.post(f"{root}/documents", files={"file": ("api.md", document)}, headers=headers)
    assert upload.status_code == 200, upload.text
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "skill-execution", "skill_id": "api-parsing",
        "source_document_ids": [bridge.source_id], "idempotency_key": "staged-generic-skill",
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result_schema"] == "skill-execution.v1"
    assert bridge.last_payload["source_documents"][0]["text"] == ""
    assert bridge.started_staged == response.json()["task_id"]
    assert "".join(item[3] for item in bridge.chunks) == document.decode()


def test_cases_stage_only_confirmed_analysis_context_without_resending_source(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "65536")
    upload = client.post(f"{root}/documents", files={"file": ("req.md", b"short source")}, headers=headers)
    assert upload.status_code == 200, upload.text
    source_id = upload.json()["id"]
    run_id = "large-confirmed-analysis"
    original = {
        "schema": "requirement-analysis.v1", "result_id": "large-result",
        "task_id": "large-task", "session_id": "large-session",
        "source_document_ids": [source_id], "generated_at": "2026-10-23T10:00:00Z", "warnings": [],
        "data": {"requirements": [
            {"id": "REQ-1", "title": "Login", "description": "A" * 70000,
             "priority": "medium", "source_refs": [{"document_id": source_id, "locator": "req.md:1"}],
             "acceptance_criteria": []}], "ambiguities": [], "quality_warnings": []},
    }
    with SessionLocal() as db:
        db.add(AnalysisRun(id=run_id, project_id=int(root.rsplit("/", 1)[-1]), user_id=1,
                           source_ids=[source_id], provider="harness", status="ready",
                           result={"harness_result": original, "ambiguities": [],
                                   "report_markdown": "NOT AN INPUT"}))
        db.commit()
    bridge = FakeBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "test-cases", "skill_id": "test-cases", "analysis_run_id": run_id,
        "idempotency_key": "staged-confirmed-cases",
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert bridge.started_staged == response.json()["task_id"]
    assert bridge.last_payload["staged_kind"] == "analysis"
    assert bridge.last_payload["analysis"] is None
    assert bridge.last_payload["source_document_ids"] == [source_id]
    assert bridge.last_payload["source_documents"] == [{"id": source_id, "filename": "req.md", "text": ""}]
    manifest = bridge.last_payload["input_manifest"]
    assert len(manifest) == 1 and manifest[0]["id"] == f"analysis:{run_id}"
    assembled = "".join(part[3] for part in bridge.chunks)
    assert hashlib.sha256(assembled.encode()).hexdigest() == manifest[0]["sha256"]
    analysis = json.loads(assembled)
    assert analysis["requirements"][0]["description"] == "A" * 70000
    assert "report_markdown" not in analysis and "NOT AN INPUT" not in assembled


def test_harness_xmind_export_uses_completed_raw_cases_and_project_scope(context, monkeypatch):
    from api.tests.test_harness_artifacts import _result

    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    task_id = "skill-xmind-task"
    result = _result()
    result["task_id"] = task_id
    result["source_document_ids"] = ["doc-1"]
    with SessionLocal() as db:
        db.add(AnalysisRun(id="analysis-1", project_id=project_id, user_id=1,
                           source_ids=["doc-1"], provider="harness", status="ready", result={}))
        db.add(HarnessTask(id=task_id, project_id=project_id, user_id=1, task_type="test-cases",
                           result_schema="test-cases.v1", skill_id="test-cases", source_document_ids=["doc-1"],
                           analysis_run_id="analysis-1", status="running", idempotency_key="skill-xmind-test",
                           result=result))
        db.commit()

    path = f"{root}/harness/tasks/{task_id}/xmind"
    assert client.get(path).status_code == 409
    captured = []
    monkeypatch.setattr(harness_routes, "generate_skill_xmind", lambda cases, title: captured.append((cases, title)) or b"PKskill")
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        task.status = "completed"
        db.commit()
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.content == b"PKskill"
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert captured[0][0][0]["模块"] == ["登录", "边界"]
    assert captured[0][0][0]["优先级"] == "P0"
    assert "Harness 任务：skill-xmind-task" in captured[0][0][0]["备注"]
    assert "分析运行：analysis-1" in captured[0][0][0]["备注"]
    assert client.get(f"{root}/harness/tasks/{task_id}/result").json()["result"] == result

    other = client.post("/api/v1/projects", json={"name": "Export isolation"}, headers=headers)
    assert other.status_code == 200
    assert client.get(f"/api/v1/projects/{other.json()['id']}/harness/tasks/{task_id}/xmind").status_code == 404

    result["data"]["cases"][0].pop("module_path")
    with SessionLocal() as db:
        db.get(HarnessTask, task_id).result = result
        db.commit()
    assert client.get(path).status_code == 409
    assert len(captured) == 1

    result = _result()
    result["task_id"] = "another-task"
    with SessionLocal() as db:
        db.get(HarnessTask, task_id).result = result
        db.commit()
    assert client.get(path).status_code == 409
    assert len(captured) == 1


def test_harness_staged_input_failure_cancels_remote_before_reporting_failure(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setenv("TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES", "65536")
    upload = client.post(f"{root}/documents", files={"file": ("large.md", b"x" * 70000)}, headers=headers)
    assert upload.status_code == 200

    class FailingBridge(FakeBridge):
        def upload_input_chunk(self, task_id, document_index, chunk_index, text, request_id, scope):
            if chunk_index == 1:
                raise harness_bridge.HarnessBridgeHTTPError(503, "provider_unavailable")
            return super().upload_input_chunk(task_id, document_index, chunk_index, text, request_id, scope)

        def cancel_task(self, task_id, request_id, scope=None):
            self.cancelled_remote = task_id
            return super().cancel_task(task_id, request_id, scope)

    bridge = FailingBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "failed-staged-input",
    }, headers=headers)
    assert response.status_code == 503
    assert bridge.cancelled_remote == bridge.last_payload["task_id"]
    with SessionLocal() as db:
        task = db.get(HarnessTask, bridge.cancelled_remote)
        assert task.status == "failed"
        assert task.error["code"] == "provider_unavailable"


@pytest.mark.parametrize("failure,status,code", [
    (harness_bridge.HarnessBridgeUnavailable("SECRET source text"), 503, "provider_unavailable"),
    (harness_bridge.HarnessBridgeHTTPError(503, "queue_full", "SECRET source text"), 503, "queue_full"),
    (harness_bridge.HarnessBridgeHTTPError(409, "input_timeout", "SECRET source text"), 409, "input_timeout"),
    (harness_bridge.HarnessBridgeHTTPError(409, "window_policy_mismatch", "SECRET source text"), 409, "window_policy_mismatch"),
    (harness_bridge.HarnessBridgeHTTPError(503, "SECRET_source_text", "SECRET source text"), 503, "provider_unavailable"),
    (harness_bridge.HarnessBridgeHTTPError(503, "confirmation_unavailable", "SECRET source text"), 503, "confirmation_unavailable"),
    (harness_bridge.HarnessBridgeHostUnavailable("connection refused"), 503, "host_unavailable"),
    (harness_bridge.HarnessBridgeError("SECRET source text"), 502, "provider_unavailable"),
])
def test_create_failure_replays_only_fixed_error_text(context, monkeypatch, failure, status, code):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200

    class FailingBridge(FakeBridge):
        def create_task(self, payload, _request_id, _scope=None):
            self.last_payload = payload
            raise failure

    bridge = FailingBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [uploaded.json()["id"]], "idempotency_key": "redact-" + type(failure).__name__,
    }, headers=headers)
    assert response.status_code == status
    assert "SECRET" not in response.text
    with SessionLocal() as db:
        task = db.get(HarnessTask, bridge.last_payload["task_id"])
        events = db.query(HarnessTaskEvent).filter_by(task_id=task.id).all()
        assert task.status == "failed" and task.error["code"] == code
        assert "SECRET" not in json.dumps({"error": task.error, "events": [event.payload for event in events]})


def test_session_replay_is_project_scoped_and_read_only(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "session-replay-task",
    }, headers=headers)
    assert response.status_code == 200
    task = response.json()
    replay = client.get(f"{root}/harness/tasks/{task['task_id']}/session")
    assert replay.status_code == 200, replay.text
    assert replay.json()["session_id"] == "session-1"
    assert replay.json()["events"][0]["type"] == "user/message"


def test_session_replay_allows_a_bound_map_reduce_session(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "session-replay-map-reduce-task",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        task.session_ids = ["session-1", "session-1-map-1"]
        db.commit()
    replay = client.get(f"{root}/harness/tasks/{task_id}/session?session_id=session-1-map-1")
    assert replay.status_code == 200, replay.text
    rejected = client.get(f"{root}/harness/tasks/{task_id}/session?session_id=unrelated")
    assert rejected.status_code == 409


def test_session_replay_rejects_unbound_id_before_calling_host(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "missing-session-task",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        task.session_ids = []
        db.commit()
    legacy_primary = client.get(f"{root}/harness/tasks/{task_id}/session")
    assert legacy_primary.status_code == 200

    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        task.session_id = None
        task.session_ids = []
        db.commit()
    arbitrary = client.get(f"{root}/harness/tasks/{task_id}/session?session_id=foreign-session")
    assert arbitrary.status_code == 409
    assert client.get(f"{root}/harness/tasks/{task_id}/session").status_code == 409


def test_provisional_session_is_not_exposed_or_replayable(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200

    class ProvisionalBridge(FakeBridge):
        def create_task(self, payload, request_id, scope=None):
            response = super().create_task(payload, request_id, scope)
            response["session_id"] = f"ta-{payload['task_id']}"
            return response

        def stream_events(self, *_args, **_kwargs):
            return iter(())

    monkeypatch.setattr(harness_bridge, "HarnessBridge", ProvisionalBridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [upload.json()["id"]], "idempotency_key": "provisional-session-task",
    }, headers=headers)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["session_id"] is None and task["session_ids"] == []
    assert client.get(f"{root}/harness/tasks/{task['task_id']}/session").status_code == 409
    assert client.get(
        f"{root}/harness/tasks/{task['task_id']}/session",
        params={"session_id": f"ta-{task['task_id']}"},
    ).status_code == 409


def test_recent_tasks_can_restore_a_page_after_refresh(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id],
        "idempotency_key": "refresh-task",
    }, headers=headers)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    listed = client.get(f"{root}/harness/tasks?task_type=requirement-analysis&limit=5")
    assert listed.status_code == 200
    assert listed.json()[0]["task_id"] == task_id


def test_analysis_task_lookup_outlives_recent_task_window(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "historic-task",
    }, headers=headers)
    assert created.status_code == 200, created.text
    original = created.json()
    with SessionLocal() as db:
        task = db.get(HarnessTask, original["task_id"])
        for index in range(22):
            db.add(HarnessTask(
                id=f"newer-task-{index}", project_id=task.project_id, user_id=task.user_id,
                task_type="requirement-analysis", task_version="v1",
                result_schema="requirement-analysis.v1", skill_id="requirement-analysis",
                source_document_ids=[], status="completed", idempotency_key=f"newer-key-{index}",
                created_at=f"2099-01-01T00:00:{index:02d}Z",
            ))
        db.commit()
    recent = client.get(f"{root}/harness/tasks?task_type=requirement-analysis&limit=20")
    assert recent.status_code == 200
    assert original["task_id"] not in {task["task_id"] for task in recent.json()}
    found = client.get(f"{root}/harness/tasks?task_type=requirement-analysis&analysis_run_id={original['analysis_run_id']}&limit=1")
    assert found.status_code == 200
    assert [task["task_id"] for task in found.json()] == [original["task_id"]]
    wrong_type = client.get(f"{root}/harness/tasks?task_type=test-cases&analysis_run_id={original['analysis_run_id']}&limit=1")
    assert wrong_type.status_code == 200 and wrong_type.json() == []
    other_project = client.post("/api/v1/projects", json={"name": "Other"}, headers=headers)
    assert other_project.status_code == 200
    foreign = client.get(
        f"/api/v1/projects/{other_project.json()['id']}/harness/tasks?analysis_run_id={original['analysis_run_id']}&limit=1"
    )
    assert foreign.status_code == 200 and foreign.json() == []


def test_project_skill_disable_is_enforced_server_side(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Upload\nfile")}, headers=headers)
    assert upload.status_code == 200
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: FakeBridge())
    with SessionLocal() as db:
        project_id = int(root.rsplit("/", 1)[-1])
        skill = db.get(ProjectSkill, (project_id, "requirement-analysis"))
        if skill is None:
            skill = ProjectSkill(project_id=project_id, skill_id="requirement-analysis", name="requirement-analysis",
                                 label="需求分析", source_path="/tmp/skill", description="", enabled=False)
            db.add(skill)
        else:
            skill.enabled = False
        db.commit()
    response = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis",
        "skill_id": "requirement-analysis",
        "source_document_ids": [upload.json()["id"]],
        "idempotency_key": "disabled-skill",
    }, headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize("host_unavailable", [False, True])
def test_disabling_skill_cancels_inflight_task_even_if_host_is_unavailable(context, monkeypatch, host_unavailable):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class CancelBridge(FakeBridge):
        def __init__(self):
            self.cancelled = []

        def cancel_task(self, task_id, request_id, scope=None):
            self.cancelled.append((task_id, scope))
            if host_unavailable:
                raise harness_bridge.HarnessBridgeUnavailable("down")
            return {"status": "cancelled"}

    bridge = CancelBridge()
    bridge.source_id = upload.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "revoked-skill-task",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    event_name, result = next(bridge.stream_events(task_id, 1, "test"))
    assert harness_projection.project_event(task_id, event_name, {**result, "_remote_sequence": result["sequence"]})
    revoked = client.patch(f"{root}/skills/requirement-analysis", json={"enabled": False}, headers=headers)
    assert revoked.status_code == 200 and revoked.json()["enabled"] is False
    assert bridge.cancelled == [(task_id, {
        "subject_user_id": 1, "project_id": int(root.rsplit("/", 1)[-1]),
        "task_id": task_id, "allowed_operation": "task.cancel",
    })]
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "cancelled" and task.result == {}
        assert task.error["code"] == "project_skill_disabled"
        assert db.get(AnalysisRun, task.analysis_run_id).status == "failed"
        events = db.query(HarnessTaskEvent).filter(HarnessTaskEvent.task_id == task_id).order_by(HarnessTaskEvent.sequence).all()
        assert [event.event for event in events][-2:] == ["task.result", "task.cancelled"]
        assert events[-1].payload["reason"] == "project_skill_disabled"
    assert harness_projection.project_event(task_id, "task.completed", {
        "task_id": task_id, "_remote_sequence": 4,
    }) is False
    result_view = client.get(f"{root}/harness/tasks/{task_id}/result")
    assert result_view.status_code == 200 and result_view.json()["result"] == {}
    assert "task.completed" not in client.get(f"{root}/harness/tasks/{task_id}/events?after=0").text


def test_disabling_skill_during_host_creation_cancels_the_just_created_remote_task(context, monkeypatch):
    client, headers, root = context
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nfile")}, headers=headers)
    assert upload.status_code == 200

    class RacingBridge(FakeBridge):
        def __init__(self):
            self.cancelled = []

        def create_task(self, payload, request_id, scope=None):
            with SessionLocal() as db:
                skill = db.get(ProjectSkill, (payload["project_id"], payload["skill_id"]))
                skill.enabled = False
                assert harness_projection.cancel_tasks_for_disabled_skill(
                    db, payload["project_id"], payload["skill_id"],
                ) == [(payload["task_id"], payload["user_id"])]
                db.commit()
            return super().create_task(payload, request_id, scope)

        def cancel_task(self, task_id, request_id, scope=None):
            self.cancelled.append(task_id)
            return {"status": "cancelled"}

    bridge = RacingBridge()
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [upload.json()["id"]], "idempotency_key": "disable-during-remote-create",
    }, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "cancelled"
    assert bridge.cancelled == [created.json()["task_id"]]
    with SessionLocal() as db:
        assert db.get(HarnessTask, created.json()["task_id"]).result == {}


@pytest.mark.parametrize("revocation", ["removed", "reader", "archived"])
@pytest.mark.parametrize("host_unavailable", [False, True])
def test_project_access_change_cancels_inflight_harness_work(context, monkeypatch, revocation, host_unavailable):
    client, admin_headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    member = client.post("/api/v1/users", json={
        "username": "harness-member", "password": "member-password-123", "role": "reviewer",
    }, headers=admin_headers).json()
    assert client.post(f"{root}/members", json={"user_id": member["id"], "role": "reviewer"},
                       headers=admin_headers).status_code == 200
    client.post("/api/v1/auth/logout", headers=admin_headers)
    member_headers = login(client, "harness-member", "member-password-123")
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")},
                           headers=member_headers)
    assert uploaded.status_code == 200, uploaded.text

    class CancelBridge(FakeBridge):
        def __init__(self):
            self.cancelled = []

        def cancel_task(self, task_id, request_id, scope=None):
            self.cancelled.append((task_id, scope))
            if host_unavailable:
                raise harness_bridge.HarnessBridgeUnavailable("down")
            return {"status": "cancelled"}

    bridge = CancelBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "permission-change",
    }, headers=member_headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    event_name, result = next(bridge.stream_events(task_id, 1, "test"))
    assert harness_projection.project_event(task_id, event_name, {
        **result, "_remote_sequence": result["sequence"],
    })
    client.post("/api/v1/auth/logout", headers=member_headers)
    admin_headers = login(client)
    if revocation == "removed":
        changed = client.delete(f"{root}/members/{member['id']}", headers=admin_headers)
    elif revocation == "reader":
        changed = client.patch(f"{root}/members/{member['id']}", json={"role": "reader"},
                               headers=admin_headers)
    else:
        changed = client.post(f"{root}/archive", headers=admin_headers)
    assert changed.status_code == 200, changed.text
    assert bridge.cancelled == [(task_id, {
        "subject_user_id": member["id"], "project_id": project_id,
        "task_id": task_id, "allowed_operation": "task.cancel",
    })]
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "cancelled" and task.result == {}
        assert task.error["code"] == {
            "removed": "project_access_revoked", "reader": "project_member_readonly",
            "archived": "project_archived",
        }[revocation]
        assert db.get(AnalysisRun, task.analysis_run_id).status == "failed"
        assert db.query(HarnessTaskEvent).filter_by(task_id=task_id).order_by(
            HarnessTaskEvent.sequence.desc()).first().event == "task.cancelled"
    assert harness_projection.project_event(task_id, "task.completed", {
        "task_id": task_id, "_remote_sequence": 4,
    }) is False
    with SessionLocal() as db:
        assert db.query(CaseDraft).filter_by(project_id=project_id).count() == 0
        assert db.get(HarnessTask, task_id).result == {}


@pytest.mark.parametrize("finish", ["completed", "cancelled"])
def test_project_delete_requires_finished_harness_work_and_removes_its_history(context, monkeypatch, finish):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")}, headers=headers)
    assert uploaded.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "delete-harness-history",
    }, headers=headers)
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    blocked = client.delete(root, headers=headers)
    assert blocked.status_code == 409, blocked.text
    with SessionLocal() as db:
        assert db.get(Project, project_id) is not None
        assert db.get(HarnessTask, task_id).status == "running"

    if finish == "completed":
        result = client.get(f"{root}/harness/tasks/{task_id}/events?after=0", headers=headers)
        assert result.status_code == 200 and "task.completed" in result.text
    else:
        result = client.post(f"{root}/harness/tasks/{task_id}/cancel", headers=headers)
        assert result.status_code == 200 and result.json()["status"] == "cancelled"
    with SessionLocal() as db:
        task = db.get(HarnessTask, task_id)
        analysis_id = task.analysis_run_id
        assert task.status == finish and db.get(AnalysisRun, analysis_id) is not None
        assert db.query(HarnessTaskEvent).filter_by(task_id=task_id).count() > 0
    deleted = client.delete(root, headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert bridge.deleted_scopes == [{"subject_user_id": 1, "project_id": project_id,
                                      "task_id": task_id, "allowed_operation": "task.delete"}]
    with SessionLocal() as db:
        assert db.get(Project, project_id) is None
        assert db.get(AnalysisRun, analysis_id) is None
        assert db.get(HarnessTask, task_id) is None
        assert db.query(HarnessTaskEvent).filter_by(task_id=task_id).count() == 0


def test_project_delete_survives_host_outage_without_losing_authority(context, monkeypatch):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")}, headers=headers)
    assert uploaded.status_code == 200
    bridge = FakeBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "delete-retry",
    }, headers=headers)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    assert "task.completed" in client.get(f"{root}/harness/tasks/{task_id}/events?after=0", headers=headers).text

    def unavailable(*_args):
        raise harness_bridge.HarnessBridgeUnavailable("temporary outage")

    monkeypatch.setattr(bridge, "delete_task", unavailable)
    refused = client.delete(root, headers=headers)
    assert refused.status_code == 503
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at
        assert db.get(HarnessTask, task_id) is not None
    assert client.get(f"{root}/overview", headers=headers).status_code == 409
    blocked = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "after-delete-intent",
    }, headers=headers)
    assert blocked.status_code == 409
    with SessionLocal() as db:
        assert db.query(HarnessTask).filter_by(project_id=project_id).count() == 1

    monkeypatch.setattr(bridge, "delete_task", lambda task_id, _request_id, scope: {
        "task_id": task_id, "status": "deleted",
    })
    assert client.delete(root, headers=headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(Project, project_id) is None
        assert db.get(HarnessTask, task_id) is None


def test_project_delete_retries_after_only_one_remote_task_was_cleaned(context, monkeypatch):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")}, headers=headers)
    assert uploaded.status_code == 200

    class PartiallyAvailableBridge(FakeBridge):
        fail_once = True

        def delete_task(self, task_id, request_id, scope):
            self.deleted_scopes = getattr(self, "deleted_scopes", []) + [scope]
            if self.fail_once and len(self.deleted_scopes) == 2:
                self.fail_once = False
                raise harness_bridge.HarnessBridgeHTTPError(503, "host_unavailable")
            return {"task_id": task_id, "status": "deleted"}

    bridge = PartiallyAvailableBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    ids = []
    for key in ("first", "second"):
        created = client.post(f"{root}/harness/tasks", json={
            "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
            "source_document_ids": [bridge.source_id], "idempotency_key": key,
        }, headers=headers)
        assert created.status_code == 200
        task_id = created.json()["task_id"]
        ids.append(task_id)
        assert "task.completed" in client.get(f"{root}/harness/tasks/{task_id}/events?after=0", headers=headers).text

    assert client.delete(root, headers=headers).status_code == 503
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at
        assert all(db.get(HarnessTask, task_id) for task_id in ids)
    assert client.delete(root, headers=headers).status_code == 200
    assert [scope["task_id"] for scope in bridge.deleted_scopes] == sorted(ids) + sorted(ids)
    with SessionLocal() as db:
        assert db.get(Project, project_id) is None


def test_project_delete_refuses_stale_intent_after_host_response(context, monkeypatch):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Requirement")}, headers=headers)
    assert uploaded.status_code == 200

    class ChangedIntentBridge(FakeBridge):
        def delete_task(self, task_id, _request_id, _scope):
            with SessionLocal() as db:
                project = db.get(Project, project_id)
                project.deletion_pending_at = "2026-10-26T00:00:00Z"
                db.commit()
            return {"task_id": task_id, "status": "deleted"}

    bridge = ChangedIntentBridge()
    bridge.source_id = uploaded.json()["id"]
    monkeypatch.setattr(harness_bridge, "HarnessBridge", lambda: bridge)
    created = client.post(f"{root}/harness/tasks", json={
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
        "source_document_ids": [bridge.source_id], "idempotency_key": "stale-intent",
    }, headers=headers)
    task_id = created.json()["task_id"]
    assert "task.completed" in client.get(f"{root}/harness/tasks/{task_id}/events?after=0", headers=headers).text
    assert client.delete(root, headers=headers).status_code == 409
    with SessionLocal() as db:
        assert db.get(Project, project_id) is not None
        assert db.get(HarnessTask, task_id) is not None
