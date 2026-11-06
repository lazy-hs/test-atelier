import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.audit_harness_project_storage import AuditError, audit


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def storage(tmp_path):
    database = tmp_path / "atelier.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE projects (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE harness_tasks (id TEXT PRIMARY KEY, project_id INTEGER)"))
        connection.execute(text("INSERT INTO projects VALUES (4)"))
        connection.execute(text("INSERT INTO harness_tasks VALUES ('task-one', 4)"))
    engine.dispose()
    tasks = tmp_path / "tasks"
    sessions = tmp_path / "sessions"
    tasks.mkdir()
    session_dir = sessions / "nested" / "one"
    session_dir.mkdir(parents=True)
    (tasks / "task-one.json").write_text(json.dumps({
        "id": "task-one", "project_id": 4, "session_id": "session-one",
        "session_ids": ["ta-task-one", "session-one"],
        "source_documents": [{"filename": "private-requirement.md"}],
    }))
    (tasks / "task-one.events.json").write_text("[]")
    (session_dir / "session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "session-one"}) + "\n")
    return database, tasks, sessions


def test_storage_audit_reconciles_host_and_database_without_exposing_source_metadata(storage):
    database, tasks, sessions = storage
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "consistent"
    assert report["business_tasks"] == report["host_tasks"] == report["present_sessions"] == 1
    assert report["missing_sessions"] == report["host_only_tasks"] == 0
    assert report["legacy_layout_sessions"] == 1
    assert "private-requirement.md" not in json.dumps(report)


def test_storage_audit_blocks_quarantined_runner_even_with_complete_files(storage):
    database, tasks, sessions = storage
    index = tasks / "task-one.json"
    saved = json.loads(index.read_text())
    saved["termination_unconfirmed"] = True
    index.write_text(json.dumps(saved))
    command = [sys.executable, str(ROOT / "scripts/audit_harness_project_storage.py"),
               "--project-id", "4", "--task-root", str(tasks), "--session-root", str(sessions),
               "--database-url", f"sqlite:///{database}"]
    proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "inconsistent"
    assert report["unconfirmed_runner_stops"] == 1
    assert report["missing_sessions"] == report["missing_event_replays"] == 0
    saved["termination_unconfirmed"] = "false"
    index.write_text(json.dumps(saved))
    with pytest.raises(AuditError, match="runner termination state"):
        audit(4, f"sqlite:///{database}", tasks, sessions)


def test_storage_audit_detects_host_remnants_after_business_project_deletion(storage):
    database, tasks, sessions = storage
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM harness_tasks"))
        connection.execute(text("DELETE FROM projects"))
    engine.dispose()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/audit_harness_project_storage.py"),
                           "--project-id", "4", "--task-root", str(tasks), "--session-root", str(sessions),
                           "--database-url", f"sqlite:///{database}"],
                          cwd=ROOT, capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stderr
    report = json.loads(proc.stdout)
    assert report["status"] == "inconsistent" and not report["project_exists"]
    assert report["host_only_tasks"] == 1 and report["present_sessions"] == 1
    assert "private-requirement.md" not in proc.stdout + proc.stderr


def test_storage_audit_flags_missing_and_shared_sessions(storage):
    database, tasks, sessions = storage
    (tasks / "task-other.json").write_text(json.dumps({
        "id": "task-other", "project_id": 5, "session_ids": ["session-one"],
    }))
    (sessions / "nested/one/session.v4.jsonl").unlink()
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["missing_sessions"] == 1 and report["shared_sessions"] == 1


def test_storage_audit_detects_unattributed_replays_and_sessions(storage):
    database, tasks, sessions = storage
    (tasks / "orphan.events.json").write_text("[]")
    extra = sessions / "nested/two"
    extra.mkdir()
    (extra / "session.jsonl").write_text(json.dumps({"type": "session", "id": "orphan-session"}) + "\n")
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "inconsistent"
    assert report["global_orphan_event_replays"] == 1
    assert report["global_unreferenced_sessions"] == 1


def test_storage_audit_fails_closed_on_symlinks_and_never_creates_a_database(storage, tmp_path):
    database, tasks, sessions = storage
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tasks, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("This host does not permit symlink creation")
    with pytest.raises(AuditError):
        audit(4, f"sqlite:///{database}", alias, sessions)
    alias.unlink()
    (sessions / "nested/one/foreign").symlink_to(database)
    with pytest.raises(AuditError):
        audit(4, f"sqlite:///{database}", tasks, sessions)
    (sessions / "nested/one/foreign").unlink()
    missing = tmp_path / "never-created.db"
    with pytest.raises(FileNotFoundError):
        audit(4, f"sqlite:///{missing}", tasks, sessions)
    assert not missing.exists()


def test_storage_audit_rejects_junctions_before_traversal(storage, monkeypatch):
    database, tasks, sessions = storage
    linked = sessions / "linked"
    linked.mkdir()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == linked or original(self))
    with pytest.raises(AuditError, match="junction"):
        audit(4, f"sqlite:///{database}", tasks, sessions)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction runtime contract")
def test_storage_audit_rejects_real_windows_junction(storage, tmp_path):
    database, tasks, sessions = storage
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = sessions / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert linked.is_junction()
    with pytest.raises(AuditError, match="junction"):
        audit(4, f"sqlite:///{database}", tasks, sessions)


def test_storage_audit_opens_a_database_path_with_spaces_read_only(storage):
    database, tasks, sessions = storage
    renamed = database.with_name("atelier storage.db")
    database.rename(renamed)
    assert audit(4, f"sqlite:///{renamed}", tasks, sessions)["status"] == "consistent"


def test_storage_audit_recognizes_a_task_owned_session_directory(storage):
    database, tasks, sessions = storage
    owned = sessions / "task-one" / "one"
    owned.parent.mkdir()
    (sessions / "nested/one/session.v4.jsonl").rename(owned.parent / "session.v4.jsonl")
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["legacy_layout_sessions"] == 0


def test_storage_audit_does_not_mark_colliding_task_owned_ids_as_legacy(storage):
    database, tasks, sessions = storage
    (sessions / "task-one").mkdir()
    (sessions / "nested/one/session.v4.jsonl").rename(sessions / "task-one/session.v4.jsonl")
    (sessions / "task-two").mkdir()
    (sessions / "task-two/session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "session-one"}) + "\n")
    (tasks / "task-two.json").write_text(json.dumps({
        "id": "task-two", "project_id": 4, "session_ids": ["session-one"],
    }))
    (tasks / "task-two.events.json").write_text("[]")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO harness_tasks VALUES ('task-two', 4)"))
    engine.dispose()
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "consistent" and report["legacy_layout_sessions"] == 0


def test_storage_audit_does_not_count_a_different_tasks_session_as_present(storage):
    database, tasks, sessions = storage
    (sessions / "task-two").mkdir()
    (sessions / "nested/one/session.v4.jsonl").rename(sessions / "task-two/session.v4.jsonl")
    (tasks / "task-two.json").write_text(json.dumps({
        "id": "task-two", "project_id": 5, "session_ids": ["session-one"],
    }))
    (tasks / "task-two.events.json").write_text("[]")
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "inconsistent"
    assert report["missing_sessions"] == 1 and report["present_sessions"] == 0
    assert report["legacy_layout_sessions"] == 0


def test_storage_audit_accepts_completed_deletion_markers_and_flags_partial_cleanup(storage):
    database, tasks, sessions = storage
    (tasks / "task-gone.deleted.json").write_text(json.dumps({
        "id": "task-gone", "project_id": 4, "user_id": 1,
    }))
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "consistent"
    assert report["deleted_task_markers"] == 1 and report["incomplete_deletions"] == 0
    (sessions / "task-gone").mkdir()
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["status"] == "inconsistent" and report["incomplete_deletions"] == 1


def test_storage_audit_rejects_unowned_deletion_marker(storage):
    database, tasks, sessions = storage
    (tasks / "task-gone.deleted.json").write_text(json.dumps({
        "id": "task-gone", "project_id": 4, "user_id": False,
    }))
    with pytest.raises(AuditError, match="invalid ownership"):
        audit(4, f"sqlite:///{database}", tasks, sessions)


def test_storage_audit_keeps_partially_deleted_project_distinct_from_complete_cleanup(storage):
    database, tasks, sessions = storage
    (tasks / "task-one.deleted.json").write_text(json.dumps({
        "id": "task-one", "project_id": 4, "user_id": 1,
    }))
    (tasks / "task-one.json").unlink()
    (tasks / "task-one.events.json").unlink()
    (sessions / "nested/one/session.v4.jsonl").unlink()
    report = audit(4, f"sqlite:///{database}", tasks, sessions)
    assert report["business_only_tasks"] == 1
    assert report["deleted_task_markers"] == 1
    assert report["status"] == "inconsistent"
