import time

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api import harness_projector
from api import harness_projection
from api.harness_case_context import case_analysis_context, case_context_sha256
from api.db import AnalysisRun, Base, CaseDraft, HarnessTask, HarnessTaskEvent, Project, User


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'projector.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_task(factory, task_id="task-1", *, owner="", lease_until=0):
    with factory() as db:
        db.add_all([
            User(id=1, username="projector-user", password_hash="hash", role="reviewer"),
            Project(id=1, name="project"),
            AnalysisRun(id="analysis-1", project_id=1, user_id=1, source_ids=[], provider="harness", status="queued"),
            HarnessTask(
                id=task_id, project_id=1, user_id=1, task_type="requirement-analysis",
                task_version="v1", result_schema="requirement-analysis.v1", skill_id="requirement-analysis",
                source_document_ids=["doc-1"], analysis_run_id="analysis-1", status="running",
                idempotency_key=task_id, projector_owner=owner,
                projector_lease_until=lease_until,
            ),
        ])
        db.commit()


class FakeBridge:
    def __init__(self):
        self.calls = []

    def stream_events(self, task_id, after, request_id, scope, **kwargs):
        self.calls.append((task_id, after, request_id, scope, kwargs))
        yield "task.started", {"task_id": task_id, "sequence": 1, "session_id": "session-1"}
        yield "task.result", {
            "task_id": task_id,
            "sequence": 2,
            "session_id": "session-1",
            "result": {
                "schema": "requirement-analysis.v1",
                "result_id": "result-1",
                "task_id": task_id,
                "session_id": "session-1",
                "source_document_ids": ["doc-1"],
                "generated_at": "2026-10-24T00:00:00Z",
                "warnings": [],
                "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
            },
        }
        yield "task.completed", {"task_id": task_id, "sequence": 3}


def test_projector_claims_scoped_task_and_projects_once(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    monkeypatch.setattr(harness_projector, "WORKER_ID", "projector-test")
    monkeypatch.setattr(harness_projector, "LEASE_SECONDS", 60.0)
    bridge = FakeBridge()

    task_id = harness_projector.claim_next()
    assert task_id == "task-1"
    assert harness_projector.project_task(task_id, bridge_factory=lambda: bridge) == 3
    with factory() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "completed"
        assert task.projector_owner == ""
        events = db.scalars(select(HarnessTaskEvent).where(HarnessTaskEvent.task_id == task_id).order_by(HarnessTaskEvent.sequence)).all()
        assert [(event.event, event.remote_sequence) for event in events] == [
            ("task.started", 1), ("task.result", 2), ("task.completed", 3)
        ]
        assert task.result["result_id"] == "result-1"
    # Terminal tasks cannot be claimed again, even when a stale remote stream
    # is replayed by a second process.
    assert harness_projector.claim_next() is None
    assert bridge.calls[0][3] == {"subject_user_id": 1, "project_id": 1, "task_id": "task-1", "allowed_operation": "task.events"}


def test_expired_projector_lease_is_recovered(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory, owner="dead-projector", lease_until=time.time() - 1)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    assert harness_projector._recover_expired() == 1
    assert harness_projector.claim_next(owner="new-projector") == "task-1"
    with factory() as db:
        task = db.get(HarnessTask, "task-1")
        assert task.projector_owner == "new-projector"
        assert task.projector_attempts == 1


def test_waiting_confirmation_does_not_starve_a_new_task(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    with factory() as db:
        waiting = db.get(HarnessTask, "task-1")
        waiting.status = "waiting_confirmation"
        waiting.created_at = "2026-10-24T00:00:00Z"
        db.add(HarnessTask(
            id="task-new", project_id=1, user_id=1, task_type="requirement-analysis",
            task_version="v1", result_schema="requirement-analysis.v1", skill_id="requirement-analysis",
            source_document_ids=["doc-1"], status="running", idempotency_key="task-new",
            created_at="2026-10-25T00:00:00Z",
        ))
        db.commit()
    assert harness_projector.claim_next() == "task-1"
    assert harness_projector.release("task-1")
    assert harness_projector.claim_next() == "task-new"
    assert harness_projector.release("task-new")
    # After both have been served, the older waiting task is eligible again.
    assert harness_projector.claim_next() == "task-1"


def test_transport_failure_releases_lease_for_retry(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    monkeypatch.setattr(harness_projector, "WORKER_ID", "projector-test")
    task_id = harness_projector.claim_next()

    class DownBridge:
        def stream_events(self, *_args, **_kwargs):
            raise harness_projector.harness_bridge.HarnessBridgeUnavailable("down")

    assert harness_projector.project_task(task_id, bridge_factory=DownBridge) == 0
    with factory() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "running"
        assert task.projector_owner == ""
        assert task.projector_error == "HarnessBridgeUnavailable"


def test_projector_rejects_unknown_event_without_persisting_it(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)

    assert harness_projection.project_event(
        "task-1", "task.secret", {"task_id": "task-1", "_remote_sequence": 1},
    ) is False
    with factory() as db:
        assert db.scalars(select(HarnessTaskEvent)).all() == []


def test_projection_rejects_stale_remote_sequence(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event(
        "task-1", "task.progress", {"task_id": "task-1", "_remote_sequence": 4},
    ) is True
    assert harness_projection.project_event(
        "task-1", "task.progress", {"task_id": "task-1", "_remote_sequence": 3},
    ) is False
    with factory() as db:
        events = db.scalars(select(HarnessTaskEvent).where(HarnessTaskEvent.task_id == "task-1")).all()
        assert [event.remote_sequence for event in events] == [4]


def _valid_result(task_id="task-1"):
    return {
        "schema": "requirement-analysis.v1", "result_id": "result-1",
        "task_id": task_id, "session_id": "session-1",
        "source_document_ids": ["doc-1"], "generated_at": "2026-10-24T00:00:00Z",
        "warnings": [], "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
    }


def test_result_accepts_only_previously_bound_real_session(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event("task-1", "task.started", {
        "task_id": "task-1", "session_id": "provisional", "_remote_sequence": 1,
    })
    result = _valid_result()
    result["session_id"] = "real-dsh-session"
    payload = {"task_id": "task-1", "session_id": "real-dsh-session", "result": result, "_remote_sequence": 3}
    with pytest.raises(ValueError, match="was not bound"):
        harness_projection.project_event("task-1", "task.result", payload)
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "real-dsh-session", "_remote_sequence": 2,
    })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "reduce_completed", "session_id": "provisional", "_remote_sequence": 3,
    })
    payload["_remote_sequence"] = 4
    wrong_event = {**payload, "session_id": "provisional"}
    with pytest.raises(ValueError, match="does not match event"):
        harness_projection.project_event("task-1", "task.result", wrong_event)
    assert harness_projection.project_event("task-1", "task.result", payload)
    assert harness_projection.project_event("task-1", "task.completed", {
        "task_id": "task-1", "_remote_sequence": 5,
    })
    with factory() as db:
        task = db.get(HarnessTask, "task-1")
        assert task.session_id == "real-dsh-session"
        assert task.session_ids == ["provisional", "real-dsh-session"]
        assert db.get(AnalysisRun, "analysis-1").status == "ready"


def test_map_reduce_progress_does_not_authorize_synthetic_sessions(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    placeholder = "ta-task-1"
    assert harness_projection.project_event("task-1", "task.started", {
        "task_id": "task-1", "session_id": placeholder, "_remote_sequence": 1,
    })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "chunk_started", "session_id": f"{placeholder}-map-1", "_remote_sequence": 2,
    })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "reduce_started", "session_id": f"{placeholder}-reduce-r1", "_remote_sequence": 3,
    })
    with factory() as db:
        task = db.get(HarnessTask, "task-1")
        assert task.session_id == placeholder and task.session_ids == []
    unbound = _valid_result()
    unbound["session_id"] = f"{placeholder}-map-1"
    with pytest.raises(ValueError, match="was not bound"):
        harness_projection.project_event("task-1", "task.result", {
            "task_id": "task-1", "session_id": unbound["session_id"], "result": unbound, "_remote_sequence": 4,
        })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "real-map-1", "_remote_sequence": 4,
    })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "reduce_completed", "session_id": f"{placeholder}-reduce-r1-b1", "_remote_sequence": 5,
    })
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "real-reduce-1", "_remote_sequence": 6,
    })
    result = _valid_result()
    result["session_id"] = "real-reduce-1"
    assert harness_projection.project_event("task-1", "task.result", {
        "task_id": "task-1", "session_id": "real-reduce-1", "result": result, "_remote_sequence": 7,
    })
    assert harness_projection.project_event("task-1", "task.completed", {
        "task_id": "task-1", "_remote_sequence": 8,
    })
    with factory() as db:
        task = db.get(HarnessTask, "task-1")
        assert task.session_id == "real-reduce-1"
        assert task.session_ids == ["real-map-1", "real-reduce-1"]
        assert [event.event for event in db.scalars(select(HarnessTaskEvent).where(HarnessTaskEvent.task_id == "task-1")).all()].count("task.completed") == 1


def test_provisional_allocation_cannot_complete_as_result(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    result = _valid_result()
    result["session_id"] = "ta-task-1"
    with pytest.raises(ValueError, match="provisional allocation"):
        harness_projection.project_event("task-1", "task.result", {
            "task_id": "task-1", "session_id": "ta-task-1", "result": result, "_remote_sequence": 1,
        })


def test_result_cannot_bind_its_own_session_when_no_prior_session_exists(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    with pytest.raises(ValueError, match="was not bound"):
        harness_projection.project_event("task-1", "task.result", {
            "task_id": "task-1", "session_id": "session-1",
            "result": _valid_result(), "_remote_sequence": 1,
        })
    with factory() as db:
        assert db.get(HarnessTask, "task-1").result == {}
        assert db.get(AnalysisRun, "analysis-1").status == "queued"


def test_result_waits_for_completed_before_writing_business_analysis(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "session-1", "_remote_sequence": 1,
    })
    model_result = _valid_result()
    model_result["report_markdown"] = "# Login analysis\n\nHarness original report"
    model_result["data"]["requirements"] = [{
        "id": "REQ-1", "title": "Log in", "description": "Accept a valid account", "priority": "medium",
        "source_refs": [{"document_id": "doc-1", "locator": "body"}], "acceptance_criteria": [],
    }]
    result = {"task_id": "task-1", "session_id": "session-1", "result": model_result, "_remote_sequence": 2}
    assert harness_projection.project_event("task-1", "task.result", result)
    with factory() as db:
        assert db.get(AnalysisRun, "analysis-1").status == "queued"
        assert db.get(HarnessTask, "task-1").result["result_id"] == "result-1"
    assert harness_projection.project_event("task-1", "task.completed", {"task_id": "task-1", "_remote_sequence": 3})
    with factory() as db:
        run = db.get(AnalysisRun, "analysis-1")
        assert run.status == "ready"
        assert run.result["requirements"][0]["methods"] == []
        assert run.result["harness_result"] == model_result
        assert run.result["report_markdown"] == model_result["report_markdown"]
        assert db.get(HarnessTask, "task-1").status == "completed"
    assert not harness_projection.project_event("task-1", "task.completed", {"task_id": "task-1", "_remote_sequence": 3})


def test_historical_harness_result_without_report_does_not_gain_a_generated_report(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "session-1", "_remote_sequence": 1,
    })
    assert harness_projection.project_event("task-1", "task.result", {
        "task_id": "task-1", "session_id": "session-1", "result": _valid_result(), "_remote_sequence": 2,
    })
    assert harness_projection.project_event("task-1", "task.completed", {"task_id": "task-1", "_remote_sequence": 3})
    with factory() as db:
        run = db.get(AnalysisRun, "analysis-1")
        assert run.status == "ready"
        assert "report_markdown" not in run.result
        assert "report_markdown" not in run.result["harness_result"]


@pytest.mark.parametrize("terminal", ["task.failed", "task.cancelled"])
def test_result_followed_by_failure_never_publishes_business_analysis(tmp_path, monkeypatch, terminal):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event("task-1", "task.progress", {
        "task_id": "task-1", "kind": "session_bound", "session_id": "session-1", "_remote_sequence": 1,
    })
    assert harness_projection.project_event("task-1", "task.result", {
        "task_id": "task-1", "session_id": "session-1", "result": _valid_result(), "_remote_sequence": 2,
    })
    assert harness_projection.project_event("task-1", terminal, {"task_id": "task-1", "_remote_sequence": 3})
    with factory() as db:
        assert db.get(AnalysisRun, "analysis-1").status == "failed"
        assert db.get(HarnessTask, "task-1").status == terminal.split(".")[1]


@pytest.mark.parametrize("code,expected", [
    ("report_missing", "report_missing"),
    ("input_timeout", "input_timeout"),
    ("SECRET_source_text", "provider_unavailable"),
])
def test_host_failure_discards_untrusted_message_and_extra_fields(tmp_path, monkeypatch, code, expected):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    assert harness_projection.project_event("task-1", "task.failed", {
        "task_id": "task-1", "_remote_sequence": 1,
        "error": {"code": code, "message": "SECRET source text", "details": "SECRET source text"},
        "debug": "SECRET source text",
    })
    with factory() as db:
        task = db.get(HarnessTask, "task-1")
        event = db.scalar(select(HarnessTaskEvent).where(HarnessTaskEvent.task_id == task.id))
        run = db.get(AnalysisRun, "analysis-1")
        assert task.error["code"] == expected
        assert "SECRET" not in str({"task": task.error, "event": event.payload, "run": run.error})
        assert event.remote_sequence == 1


def test_completion_without_validated_result_is_not_success(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    payload = {"task_id": "task-1", "_remote_sequence": 1}
    with pytest.raises(ValueError, match="without a validated result"):
        harness_projection.project_event("task-1", "task.completed", payload)
    assert harness_projection.project_invalid_result("task-1", payload, "invalid")
    with factory() as db:
        assert db.get(HarnessTask, "task-1").status == "failed"
        assert db.get(AnalysisRun, "analysis-1").status == "failed"
        assert [event.event for event in db.scalars(select(HarnessTaskEvent)).all()] == ["task.failed"]


def test_case_drafts_are_created_only_after_completed_event(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.status = "ready"
        analysis.source_ids = ["doc-1"]
        analysis.result = {
            "requirements": [{"id": "REQ-1"}, {"id": "REQ-2"}], "ambiguities": [],
            "harness_result": {
                "schema": "requirement-analysis.v1", "result_id": "analysis-result",
                "task_id": "task-1", "session_id": "session-1",
                "source_document_ids": ["doc-1"], "generated_at": "2026-10-24T00:00:00Z",
                "warnings": [], "data": {"requirements": [
                    {"id": "REQ-1", "title": "Login", "description": "Valid login", "priority": "high",
                     "source_refs": [{"document_id": "doc-1", "locator": "1"}],
                     "acceptance_criteria": [{"id": "AC-1", "text": "Home",
                                              "source_refs": [{"document_id": "doc-1", "locator": "1"}]}]},
                    {"id": "REQ-2", "title": "Logout", "description": "Valid logout", "priority": "medium",
                     "source_refs": [{"document_id": "doc-1", "locator": "2"}],
                     "acceptance_criteria": [{"id": "AC-2", "text": "Signed out",
                                              "source_refs": [{"document_id": "doc-1", "locator": "2"}]}]},
                ], "ambiguities": [], "quality_warnings": []},
            },
        }
        db.add(HarnessTask(
            id="case-task", project_id=1, user_id=1, task_type="test-cases", task_version="v1",
            result_schema="test-cases.v1", skill_id="test-cases", source_document_ids=["doc-1"],
            analysis_run_id="analysis-1", status="running", idempotency_key="case-task",
            session_id="case-session", sequence=1,
        ))
        context = case_analysis_context(analysis.id, analysis.version, analysis.result)
        db.add(HarnessTaskEvent(task_id="case-task", event="task.created", sequence=1,
                                payload={"task_id": "case-task", "sequence": 1,
                                         "analysis_version": analysis.version,
                                         "analysis_sha256": case_context_sha256(context)}))
        db.commit()
    result = {
        "schema": "test-cases.v1", "result_id": "cases-1", "task_id": "case-task",
        "session_id": "case-session", "source_document_ids": ["doc-1"],
        "analysis_run_id": "analysis-1", "generated_at": "2026-10-24T00:00:00Z", "warnings": [],
        "data": {"cases": [{
            "id": "TC-1", "title": "Valid login", "module_path": ["登录", "账户"],
            "skill_priority": "P0", "requirement_ids": ["REQ-1"],
            "preconditions": [], "steps": [{"order": 1, "action": "Log in", "expected": "Home"}],
            "expected": "Home", "method": "EP", "priority": "medium", "labels": [],
            "traceability": [{"requirement_id": "REQ-1", "acceptance_criterion_id": "AC-1"}],
            "quality_warnings": [],
        }]},
    }
    from copy import deepcopy

    wrong_criterion = deepcopy(result)
    wrong_criterion["data"]["cases"][0]["traceability"][0]["acceptance_criterion_id"] = "AC-2"
    with pytest.raises(ValueError, match="belongs to another requirement"):
        harness_projection.project_event("case-task", "task.result", {
            "task_id": "case-task", "session_id": "case-session",
            "result": wrong_criterion, "_remote_sequence": 1,
        })
    with factory() as db:
        assert db.query(CaseDraft).count() == 0
        assert db.query(HarnessTaskEvent).filter_by(task_id="case-task").count() == 1
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.result = {**analysis.result, "ambiguities": [
            {"id": "AMB-1", "critical": True, "decision": ""},
        ]}
        db.commit()
    with pytest.raises(ValueError, match="blocking ambiguity is not confirmed"):
        harness_projection.project_event("case-task", "task.result", {
            "task_id": "case-task", "session_id": "case-session",
            "result": result, "_remote_sequence": 1,
        })
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.result = {**analysis.result, "ambiguities": []}
        db.commit()
    ghost = deepcopy(result)
    ghost["data"]["cases"][0]["requirement_ids"] = ["REQ-GHOST"]
    ghost["data"]["cases"][0]["traceability"][0]["requirement_id"] = "REQ-GHOST"
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.result = {**analysis.result, "requirements": [*analysis.result["requirements"], {"id": "REQ-GHOST"}]}
        db.commit()
    with pytest.raises(ValueError, match="unknown requirement"):
        harness_projection.project_event("case-task", "task.result", {
            "task_id": "case-task", "session_id": "case-session", "result": ghost, "_remote_sequence": 1,
        })
    assert harness_projection.project_event("case-task", "task.result", {
        "task_id": "case-task", "session_id": "case-session", "result": result, "_remote_sequence": 1,
    })
    with factory() as db:
        assert db.query(CaseDraft).count() == 0
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        pinned = deepcopy(analysis.result)
        analysis.version += 1
        db.commit()
    with pytest.raises(ValueError, match="analysis context changed"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.version -= 1
        analysis.result = pinned
        db.commit()
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        changed = deepcopy(analysis.result)
        changed["harness_result"]["data"]["requirements"][0]["description"] = "Changed without version bump"
        analysis.result = changed
        db.commit()
    with pytest.raises(ValueError, match="analysis context changed"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.result = pinned
        created = db.query(HarnessTaskEvent).filter_by(task_id="case-task", event="task.created").one()
        saved_created = deepcopy(created.payload)
        created.payload = {"task_id": "case-task", "sequence": 1}
        db.commit()
    with pytest.raises(ValueError, match="was not pinned"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        created = db.query(HarnessTaskEvent).filter_by(task_id="case-task", event="task.created").one()
        created.payload = saved_created
        db.commit()
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        saved = deepcopy(analysis.result)
        analysis.result = {**saved, "harness_result": None}
        db.commit()
    with pytest.raises(ValueError, match="valid Harness analysis"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        assert db.query(CaseDraft).count() == 0
        assert db.get(HarnessTask, "case-task").status == "running"
        assert db.query(HarnessTaskEvent).filter_by(task_id="case-task").count() == 2
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.result = saved
        analysis.source_ids = ["other-doc"]
        db.commit()
    with pytest.raises(ValueError, match="analysis sources changed"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.source_ids = ["doc-1"]
        analysis.provider = "remote"
        db.commit()
    with pytest.raises(ValueError, match="valid Harness analysis"):
        harness_projection.project_event("case-task", "task.completed", {
            "task_id": "case-task", "_remote_sequence": 2,
        })
    with factory() as db:
        analysis = db.get(AnalysisRun, "analysis-1")
        analysis.provider = "harness"
        db.commit()
    assert harness_projection.project_event("case-task", "task.completed", {
        "task_id": "case-task", "_remote_sequence": 2,
    })
    with factory() as db:
        assert db.query(CaseDraft).count() == 1
        assert db.get(HarnessTask, "case-task").status == "completed"
        draft = db.query(CaseDraft).one()
        assert draft.payload["module"] == ["登录", "账户"]
        assert draft.payload["priority"] == "P0"
        assert db.get(HarnessTask, "case-task").result == result
    assert not harness_projection.project_event("case-task", "task.completed", {
        "task_id": "case-task", "_remote_sequence": 2,
    })
    with factory() as db:
        assert db.query(CaseDraft).count() == 1


def test_legacy_result_event_does_not_project_business_rows_twice(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    with factory() as db:
        run = db.get(AnalysisRun, "analysis-1")
        run.status = "ready"
        run.version = 2
        run.result = {"requirements": [{"id": "REQ-1"}], "ambiguities": []}
        task = db.get(HarnessTask, "task-1")
        task.sequence = 1
        task.result = _valid_result()
        db.add(HarnessTaskEvent(
            task_id="task-1", event="task.result", sequence=1, remote_sequence=1,
            payload={"task_id": "task-1", "result": _valid_result(), "sequence": 1},
        ))
        db.commit()
    assert harness_projection.project_event("task-1", "task.completed", {
        "task_id": "task-1", "_remote_sequence": 2,
    })
    with factory() as db:
        run = db.get(AnalysisRun, "analysis-1")
        assert run.status == "ready"
        assert run.version == 2
        assert run.result == {"requirements": [{"id": "REQ-1"}], "ambiguities": []}


def test_projector_preserves_bounded_schema_failure_diagnostic(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    monkeypatch.setattr(harness_projector, "WORKER_ID", "projector-schema-test")
    task_id = harness_projector.claim_next()

    class InvalidBridge:
        def stream_events(self, task_id, _after, _request_id, _scope, **_kwargs):
            # Deliberately omit the required result envelope fields and use a
            # large output to prove the failure path stores only metadata.
            yield "task.result", {
                "task_id": task_id,
                "sequence": 2,
                "session_id": "session-1",
                "result": {
                    "schema": "requirement-analysis.v0",
                    "task_id": task_id,
                    "session_id": "session-1",
                    "source_document_ids": ["doc-1"],
                    "data": {
                        "sensitive-source-key": "sensitive-source-text" * 1000,
                        "output": "sensitive-source-text" * 1000,
                    },
                },
            }

    assert harness_projector.project_task(task_id, bridge_factory=InvalidBridge) == 0
    with factory() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "failed"
        assert task.error["code"] == "schema_invalid"
        details = task.error["details"]
        assert details["schema"] == "unknown"
        assert len(details["schema_sha256"]) == 64
        assert details["result_bytes"] > 1000
        assert "sensitive-source-text" not in str(task.error)
        assert "sensitive-source-key" not in str(task.error)
        assert task.projector_error == "schema_invalid"
        event = db.scalar(select(HarnessTaskEvent).where(
            HarnessTaskEvent.task_id == task_id,
            HarnessTaskEvent.event == "task.failed",
        ))
        assert event is not None
        assert event.remote_sequence == 2


def test_projector_marks_malformed_event_after_result_failed(tmp_path, monkeypatch):
    factory = _session_factory(tmp_path)
    _seed_task(factory)
    monkeypatch.setattr(harness_projector, "SessionLocal", factory)
    monkeypatch.setattr(harness_projection, "SessionLocal", factory)
    monkeypatch.setattr(harness_projector, "WORKER_ID", "projector-protocol-test")
    task_id = harness_projector.claim_next()

    class MalformedBridge:
        def stream_events(self, task_id, _after, _request_id, _scope, **_kwargs):
            yield "task.started", {"task_id": task_id, "sequence": 1, "session_id": "session-1"}
            yield "task.result", {
                "task_id": task_id,
                "sequence": 2,
                "session_id": "session-1",
                "result": {
                    "schema": "requirement-analysis.v1",
                    "result_id": "result-1",
                    "task_id": task_id,
                    "session_id": "session-1",
                    "source_document_ids": ["doc-1"],
                    "generated_at": "2026-10-24T00:00:00Z",
                    "warnings": [],
                    "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
                },
            }
            # A malformed later event must not reuse the previous result's
            # remote sequence while creating the failure event.
            yield "task.progress", {"task_id": task_id, "sequence": "bad"}

    assert harness_projector.project_task(task_id, bridge_factory=MalformedBridge) == 2
    with factory() as db:
        task = db.get(HarnessTask, task_id)
        assert task.status == "failed"
        assert task.error["code"] == "schema_invalid"
        failure = db.scalar(select(HarnessTaskEvent).where(
            HarnessTaskEvent.task_id == task_id,
            HarnessTaskEvent.event == "task.failed",
        ))
        assert failure is not None
        assert failure.remote_sequence is None
