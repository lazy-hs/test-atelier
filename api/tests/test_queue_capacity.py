from concurrent.futures import ThreadPoolExecutor
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("AI_STUDIO_SESSION_SECRET", "queue-test-session-secret-32-characters")
from api import ai_jobs
from api import main as api_main
from api.db import AiJob, AiQueueState, Base


def test_external_queue_reservation_is_atomic_and_released(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        db.add(AiQueueState(id=1, active_jobs=0))
        db.add_all([
            AiJob(id="job-a", project_id=1, user_id=1, analysis_id="analysis-a", kind="analysis"),
            AiJob(id="job-b", project_id=1, user_id=1, analysis_id="analysis-b", kind="analysis"),
        ])
        db.commit()
    monkeypatch.setattr(ai_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(ai_jobs, "MAX_SUBMITTED_JOBS", 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(("job-a", "job-b"), pool.map(ai_jobs.reserve_external_slot, ("job-a", "job-b"))))
    assert sum(results.values()) == 1
    winner = next(job_id for job_id, reserved in results.items() if reserved)
    loser = "job-b" if winner == "job-a" else "job-a"
    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 1
    assert ai_jobs.reserve_external_slot(winner) is True
    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 1
    ai_jobs.release_external_slot(winner)
    assert ai_jobs.reserve_external_slot(loser) is True
    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 1
    ai_jobs.release_external_slot(loser)
    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 0
        assert db.get(AiJob, loser).queue_reserved is False
    # A duplicate finalizer must not decrement capacity twice.
    ai_jobs.release_external_slot(loser)
    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 0


def test_external_queue_lazily_creates_singleton_under_concurrent_reservation(tmp_path, monkeypatch):
    """Two fresh API replicas must not race on the queue-state singleton."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'queue-lazy-init.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        db.add_all([
            AiJob(id="job-a", project_id=1, user_id=1, analysis_id="analysis-a", kind="analysis"),
            AiJob(id="job-b", project_id=1, user_id=1, analysis_id="analysis-b", kind="analysis"),
        ])
        db.commit()
    monkeypatch.setattr(ai_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(ai_jobs, "MAX_SUBMITTED_JOBS", 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ai_jobs.reserve_external_slot, ("job-a", "job-b")))

    assert results == [True, True]
    with session_factory() as db:
        state = db.get(AiQueueState, 1)
        assert state is not None
        assert state.active_jobs == 2


def test_external_queue_release_is_atomic_for_concurrent_workers(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'release.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        db.add(AiQueueState(id=1, active_jobs=0))
        db.add_all([
            AiJob(id="job-a", project_id=1, user_id=1, analysis_id="analysis-a", kind="analysis"),
            AiJob(id="job-b", project_id=1, user_id=1, analysis_id="analysis-b", kind="analysis"),
        ])
        db.commit()
    monkeypatch.setattr(ai_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(ai_jobs, "MAX_SUBMITTED_JOBS", 2)
    assert ai_jobs.reserve_external_slot("job-a") is True
    assert ai_jobs.reserve_external_slot("job-b") is True

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(ai_jobs.release_external_slot, ("job-a", "job-b")))

    with session_factory() as db:
        assert db.get(AiQueueState, 1).active_jobs == 0


def test_startup_reconciles_unreserved_jobs_for_external_worker(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        db.add(AiQueueState(id=1, active_jobs=0))
        db.add(AiJob(id="orphaned-job", project_id=1, user_id=1,
                     analysis_id="analysis", kind="analysis", status="queued",
                     queue_reserved=False))
        db.commit()
    monkeypatch.setattr(api_main, "SessionLocal", session_factory)
    monkeypatch.setenv("AI_STUDIO_EXTERNAL_WORKER", "1")
    with session_factory() as db:
        active, released = api_main.reconcile_queue_capacity(db)
        db.commit()
    assert (active, released) == (1, 0)
    with session_factory() as db:
        assert db.get(AiJob, "orphaned-job").queue_reserved is True
        assert db.get(AiQueueState, 1).active_jobs == 1


def test_production_startup_refuses_unknown_queue_capacity(monkeypatch):
    error = SQLAlchemyError("database unavailable")
    monkeypatch.setattr(api_main, "reconcile_queue_capacity", lambda _db: (_ for _ in ()).throw(error))
    monkeypatch.setenv("AI_STUDIO_EXTERNAL_WORKER", "1")

    class Session:
        def rollback(self):
            return None

    with pytest.raises(RuntimeError, match="队列容量状态无法校验"):
        api_main._reconcile_queue_capacity_or_raise(Session(), environment="production")
