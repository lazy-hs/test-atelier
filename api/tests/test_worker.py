import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import worker
from api.db import AiJob, Base, SuggestionJob
from api.observability import request_id


def _session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'worker.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_recover_running_requeues_only_expired_leases(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    now = time.time()
    with session_factory() as db:
        db.add_all([
            AiJob(id="expired-ai", project_id=1, user_id=1, analysis_id="analysis-expired",
                  kind="analysis", status="running", lease_owner="dead-worker",
                  lease_until=now - 1, lease_heartbeat=now - 2),
            AiJob(id="live-ai", project_id=1, user_id=1, analysis_id="analysis-live",
                  kind="analysis", status="running", lease_owner="live-worker",
                  lease_until=now + 60, lease_heartbeat=now),
            SuggestionJob(id="expired-suggestion", project_id=1, created_by=1, provider="local",
                          case_ids=[1], total=1, status="running", lease_owner="dead-worker",
                          lease_until=now - 1, lease_heartbeat=now - 2),
        ])
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", session_factory)

    assert worker._recover_running() == 2
    with session_factory() as db:
        expired = db.get(AiJob, "expired-ai")
        live = db.get(AiJob, "live-ai")
        suggestion = db.get(SuggestionJob, "expired-suggestion")
        assert (expired.status, expired.lease_owner, expired.lease_until) == ("queued", "", 0.0)
        assert live.status == "running" and live.lease_owner == "live-worker"
        assert (suggestion.status, suggestion.lease_owner, suggestion.lease_until) == ("queued", "", 0.0)


def test_claim_next_assigns_distinct_leases_under_concurrency(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    with session_factory() as db:
        db.add_all([
            AiJob(id="job-a", project_id=1, user_id=1, analysis_id="analysis-a", kind="analysis"),
            AiJob(id="job-b", project_id=1, user_id=1, analysis_id="analysis-b", kind="analysis"),
        ])
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: worker.claim_next(), range(2)))

    claimed_ids = {claim[1] for claim in claims if claim}
    assert claimed_ids in ({"job-a"}, {"job-b"}, {"job-a", "job-b"})
    if len(claimed_ids) == 1:
        # A losing conditional update returns no claim; the next poll must
        # still be able to pick the remaining queued job.
        remaining = worker.claim_next()
        assert remaining and remaining[1] in {"job-a", "job-b"} - claimed_ids
        claimed_ids.add(remaining[1])
    assert claimed_ids == {"job-a", "job-b"}
    with session_factory() as db:
        assert {db.get(AiJob, job_id).lease_owner for job_id in ("job-a", "job-b")} == {"worker-test"}
        assert all(db.get(AiJob, job_id).status == "running" for job_id in ("job-a", "job-b"))


def test_renew_lease_requires_current_owner(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    with session_factory() as db:
        db.add(AiJob(id="job", project_id=1, user_id=1, analysis_id="analysis", kind="analysis",
                     status="running", lease_owner="worker-test", lease_until=0, lease_heartbeat=0))
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")

    assert worker._renew_lease("ai", "job") is True
    monkeypatch.setattr(worker, "WORKER_ID", "other-worker")
    assert worker._renew_lease("ai", "job") is False


def test_lease_renew_failure_marks_job_ownership_lost(monkeypatch):
    stop = threading.Event()
    lease_lost = threading.Event()
    worker.database_degraded.clear()
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 1.0)
    monkeypatch.setattr(worker, "LEASE_SECONDS", 30.0)
    monkeypatch.setattr(worker, "_renew_lease", lambda *_args: (_ for _ in ()).throw(RuntimeError("db down")))

    thread = threading.Thread(target=worker._lease_loop, args=("ai", "job", stop, lease_lost), daemon=True)
    thread.start()
    assert lease_lost.wait(2)
    assert worker.database_degraded.is_set()
    stop.set()
    thread.join(timeout=1)
    worker.database_degraded.clear()


def test_lease_error_keeps_job_request_correlation(monkeypatch):
    class OneRenewStop:
        def wait(self, _interval):
            return False

    observed = []
    lease_lost = threading.Event()
    monkeypatch.setattr(worker, "_renew_lease", lambda *_args: False)
    monkeypatch.setattr(worker, "_log_worker_error", lambda *_args, **_kwargs: observed.append(request_id()))

    worker._lease_loop("ai", "job", OneRenewStop(), lease_lost, "trace-worker")

    assert observed == ["trace-worker"]
    assert request_id() == "-"
    assert lease_lost.is_set()


def test_claimed_job_reloads_provider_settings_without_restart(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    with session_factory() as db:
        db.add(AiJob(id="job", project_id=1, user_id=1, analysis_id="analysis", kind="analysis",
                     status="running", lease_owner="worker-test", request_id="trace-1"))
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")
    monkeypatch.setattr(worker, "_lease_loop", lambda *_args: None)
    calls = []
    events = []
    monkeypatch.setattr(worker, "_load_ai_config", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(worker.ai_jobs, "process_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.logger, "info", lambda message, **kwargs: events.append((message, kwargs.get("extra", {}))))

    worker._run_claimed("ai", "job")

    assert calls == [{"log_availability": False}]
    finished = next(fields for message, fields in events if message == "worker.job.finished")
    assert finished["status"] == "running"
    assert finished["duration_ms"] >= 0


def test_claimed_job_is_requeued_when_provider_reload_fails(tmp_path, monkeypatch):
    session_factory = _session_factory(tmp_path)
    with session_factory() as db:
        db.add(AiJob(id="job", project_id=1, user_id=1, analysis_id="analysis", kind="analysis",
                     status="running", lease_owner="worker-test", lease_until=time.time() + 60,
                     lease_heartbeat=time.time()))
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")
    monkeypatch.setattr(worker, "_lease_loop", lambda *_args: None)
    monkeypatch.setattr(worker, "_load_ai_config", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")))

    worker._run_claimed("ai", "job")

    with session_factory() as db:
        job = db.get(AiJob, "job")
        assert (job.status, job.lease_owner, job.lease_until, job.lease_heartbeat) == ("queued", "", 0.0, 0.0)


def test_heartbeat_pauses_while_database_claims_are_degraded(tmp_path, monkeypatch):
    heartbeat = tmp_path / "worker-heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 1.0)
    stop = threading.Event()
    worker.database_degraded.set()
    thread = threading.Thread(target=worker._heartbeat_loop, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.05)
    stop.set()
    thread.join(timeout=1)
    worker.database_degraded.clear()
    assert not heartbeat.exists()


def test_heartbeat_records_bounded_worker_release(tmp_path, monkeypatch):
    heartbeat = tmp_path / "worker-heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(worker, "WORKER_ID", "worker-test")
    monkeypatch.setenv("AI_STUDIO_RELEASE", "release-2026.10.18")

    worker._heartbeat()

    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["release"] == "release-2026.10.18"
    assert payload["worker_id"] == "worker-test"
    assert isinstance(payload["timestamp"], float)


def test_worker_shutdown_does_not_delete_shared_heartbeat(tmp_path, monkeypatch):
    heartbeat = tmp_path / "worker-heartbeat"
    heartbeat.write_text("last-known-state", encoding="ascii")
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(worker.ai_jobs, "EXTERNAL_WORKER", True)
    monkeypatch.setattr(worker, "_startup", lambda: 0)
    monkeypatch.setattr(worker, "_heartbeat_loop", lambda _stop: None)

    def stop_after_poll():
        worker.stop_event.set()
        return None

    monkeypatch.setattr(worker, "claim_next", stop_after_poll)
    worker.stop_event.clear()
    previous_cancel_event = worker.ai_engine.get_cancel_event()
    try:
        worker.run()
    finally:
        worker.stop_event.clear()
        worker.ai_engine.set_cancel_event(previous_cancel_event)
    assert heartbeat.read_text(encoding="ascii") == "last-known-state"


def test_worker_recovers_expired_leases_during_poll(monkeypatch):
    order = []
    monkeypatch.setattr(worker.ai_jobs, "EXTERNAL_WORKER", True)
    monkeypatch.setattr(worker, "_startup", lambda: 0)
    monkeypatch.setattr(worker, "_heartbeat_loop", lambda _stop: None)
    monkeypatch.setattr(worker, "_recover_running", lambda: order.append("recover") or 2)

    def stop_after_claim():
        order.append("claim")
        worker.stop_event.set()
        return None

    monkeypatch.setattr(worker, "claim_next", stop_after_claim)
    worker.stop_event.clear()
    previous_cancel_event = worker.ai_engine.get_cancel_event()
    try:
        worker.run()
    finally:
        worker.stop_event.clear()
        worker.ai_engine.set_cancel_event(previous_cancel_event)
    assert order == ["recover", "claim"]


def test_worker_startup_failure_emits_structured_event(monkeypatch):
    records = []
    monkeypatch.setattr(worker.ai_jobs, "EXTERNAL_WORKER", True)
    monkeypatch.setattr(worker, "_startup", lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr(worker.logger, "exception", lambda message, **kwargs: records.append((message, kwargs.get("extra", {}))))
    previous_cancel_event = worker.ai_engine.get_cancel_event()
    with pytest.raises(RuntimeError, match="db unavailable"):
        worker.run()
    assert records and records[0][0] == "worker.start_failed"
    assert records[0][1]["component"] == "external-worker"
    assert worker.ai_engine.get_cancel_event() is previous_cancel_event


def test_worker_infrastructure_errors_are_rate_limited(monkeypatch):
    records = []
    clock = iter((100.0, 101.0, 132.0))
    monkeypatch.setattr(worker, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker, "_worker_error_log_last", {})
    monkeypatch.setattr(worker, "_WORKER_ERROR_LOG_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(worker.logger, "log", lambda level, message, **kwargs: records.append((level, message, kwargs)))

    worker._log_worker_error("worker.claim.failed", component="external-worker", error_type="OperationalError")
    worker._log_worker_error("worker.claim.failed", component="external-worker", error_type="OperationalError")
    worker._log_worker_error("worker.claim.failed", component="external-worker", error_type="OperationalError")

    assert len(records) == 2
    assert records[0][0] == logging.ERROR
    assert "count" not in records[0][2]["extra"]
    assert records[1][2]["extra"]["count"] == 1
