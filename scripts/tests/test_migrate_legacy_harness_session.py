import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import create_engine, text

from scripts.audit_harness_project_storage import AuditError, audit
from scripts.backup_data import create_backup
from scripts.migrate_legacy_harness_session import MigrationError, migrate
from api.harness_bridge import create_scope_token


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def legacy(tmp_path):
    database = tmp_path / "atelier.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE projects (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE harness_tasks (id TEXT PRIMARY KEY, project_id INTEGER, "
                                "user_id INTEGER, status TEXT, session_id TEXT, session_ids TEXT)"))
        connection.execute(text("INSERT INTO projects VALUES (4)"))
        connection.execute(text("INSERT INTO harness_tasks VALUES "
                                "('task-one', 4, 7, 'completed', 'session-one', '[\"session-one\"]')"))
    engine.dispose()
    tasks, sessions, data, backups = (tmp_path / name for name in ("tasks", "sessions", "data", "backups"))
    for root in (tasks, sessions, data, backups):
        root.mkdir()
    (tasks / "task-one.json").write_text(json.dumps({
        "id": "task-one", "project_id": 4, "user_id": 7, "status": "completed",
        "session_id": "session-one", "session_ids": ["session-one"], "sequence": 0,
        "task_type": "requirement-analysis", "skill_id": "requirement-analysis",
    }))
    (tasks / "task-one.events.json").write_text("[]")
    source = sessions / "legacy-one"
    source.mkdir()
    (source / "session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "session-one"}) + "\n" +
                                             json.dumps({"type": "assistant/message", "seq": 1,
                                                         "data": {"text": "legacy replay"}}) + "\n")
    url = f"sqlite:///{database}"

    def backup():
        return create_backup(url, data, backups, tasks, sessions, services_stopped=True)

    return url, tasks, sessions, source, backup


def test_migration_is_preview_first_then_atomic_copy_and_keeps_original(legacy):
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    preview = migrate("task-one", source, url, tasks, sessions, archive)
    assert preview == {"status": "ready", "session_count": 1, "file_count": 1, "source_preserved": True}
    assert not (sessions / "task-one").exists()
    result = migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    assert result["status"] == "copied"
    assert (sessions / "task-one/session.v4.jsonl").read_bytes() == (source / "session.v4.jsonl").read_bytes()
    assert audit(4, url, tasks, sessions)["legacy_layout_sessions"] == 1
    assert audit(4, url, tasks, sessions)["status"] == "consistent"
    with pytest.raises(MigrationError, match="already exists"):
        migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    with pytest.raises(MigrationError, match="Host replay"):
        migrate("task-one", source, url, tasks, sessions, archive, finalize=True)
    finalized = migrate("task-one", source, url, tasks, sessions, archive,
                        finalize=True, replay_verified=True)
    assert finalized["status"] == "finalized" and finalized["remaining_legacy_layout_sessions"] == 0
    assert not source.exists()
    assert (sessions / "task-one/session.v4.jsonl").is_file()
    assert audit(4, url, tasks, sessions)["status"] == "consistent"


def test_migration_rejects_junction_source_before_copy(legacy, monkeypatch):
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == source or original(self))
    with pytest.raises(MigrationError, match="junction"):
        migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    assert not (sessions / "task-one").exists()
    assert (source / "session.v4.jsonl").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction runtime contract")
def test_migration_rejects_real_windows_junction_source(legacy):
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    linked = sessions / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(source)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert linked.is_junction()
    with pytest.raises(MigrationError, match="junction"):
        migrate("task-one", linked, url, tasks, sessions, archive, apply=True)
    assert not (sessions / "task-one").exists()
    assert (source / "session.v4.jsonl").is_file()


@pytest.mark.parametrize("change", ["shared_id", "foreign_file", "foreign_id", "unbound", "index_drift", "boolean_user", "db_drift", "symlink", "hard_link", "fifo"])
def test_migration_fails_closed_on_ambiguous_or_changed_ownership(legacy, change):
    url, tasks, sessions, source, backup = legacy
    if change == "shared_id":
        (tasks / "other.json").write_text(json.dumps({"id": "other", "project_id": 5, "session_ids": ["session-one"]}))
    elif change == "foreign_file":
        (source / "other.txt").write_text("unknown")
    elif change == "foreign_id":
        (source / "extra").mkdir()
        (source / "extra/session.v3.jsonl").write_text(json.dumps({"type": "session", "id": "unknown"}) + "\n")
    elif change == "unbound":
        (source / "session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "other"}) + "\n")
    elif change == "symlink":
        try:
            (source / "extra").symlink_to(tasks, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks are unavailable on this host")
    elif change == "hard_link":
        try:
            os.link(source / "session.v4.jsonl", source / "session.v3.jsonl")
        except OSError:
            pytest.skip("Hard links are unavailable on this host")
    elif change == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are unavailable on this host")
        os.mkfifo(source / "session.v3.jsonl")
    archive = backup() if change not in {"symlink", "fifo"} else None
    if change == "index_drift":
        (tasks / "task-one.json").write_text((tasks / "task-one.json").read_text().replace('"user_id": 7', '"user_id": 8'))
    elif change == "boolean_user":
        (tasks / "task-one.json").write_text((tasks / "task-one.json").read_text().replace('"user_id": 7', '"user_id": true'))
    elif change == "db_drift":
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("UPDATE harness_tasks SET user_id = 8"))
        engine.dispose()
    with pytest.raises((MigrationError, ValueError)):
        migrate("task-one", source, url, tasks, sessions, archive or source, apply=True)
    assert not (sessions / "task-one").exists()


def test_migration_rejects_stale_backup_and_duplicate_artifacts(legacy):
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    (source / "session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "session-one"}) + "\nchanged\n")
    with pytest.raises(MigrationError, match="Backup does not match"):
        migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    archive = backup()
    duplicate = sessions / "other-legacy"
    duplicate.mkdir()
    (duplicate / "session.v4.jsonl").write_bytes((source / "session.v4.jsonl").read_bytes())
    with pytest.raises(MigrationError, match="outside the selected directory"):
        migrate("task-one", source, url, tasks, sessions, archive, apply=True)


def test_finalize_rejects_corrupted_copy_and_can_retry_partial_cleanup(legacy):
    url, tasks, sessions, source, backup = legacy
    original = source / "session.v4.jsonl"
    second = source / "nested" / "session.v3.jsonl"
    second.parent.mkdir()
    second.write_bytes(original.read_bytes())
    archive = backup()
    migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    copied = sessions / "task-one/session.v4.jsonl"
    copied.write_bytes(b"corrupted")
    with pytest.raises((MigrationError, AuditError)):
        migrate("task-one", source, url, tasks, sessions, archive, finalize=True, replay_verified=True)
    assert original.exists() and second.exists()
    copied.write_bytes(original.read_bytes())
    original.unlink()  # Interrupted finalization after only one legacy file was removed.
    finalized = migrate("task-one", source, url, tasks, sessions, archive,
                        finalize=True, replay_verified=True)
    assert finalized["status"] == "finalized" and not source.exists()


def test_cli_requires_explicit_stopped_writers_and_does_not_print_paths(legacy):
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    cmd = [sys.executable, "scripts/migrate_legacy_harness_session.py", "--task-id", "task-one",
           "--source-dir", str(source), "--database-url", url,
           "--task-root", str(tasks), "--session-root", str(sessions), "--backup-archive", str(archive)]
    missing = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert missing.returncode != 0 and not (sessions / "task-one").exists()
    preview = subprocess.run([*cmd, "--services-stopped"], cwd=ROOT, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["status"] == "ready"
    assert str(source) not in preview.stdout and not (sessions / "task-one").exists()


def test_copied_session_replays_in_real_host_before_finalization_and_deletion(legacy, monkeypatch):
    node = shutil.which("node")
    extension = ROOT.parent / "test-atelier-harness-extensions"
    if not node or not (extension / "host/server.mjs").is_file():
        pytest.skip("Independent Host checkout or Node is unavailable")
    url, tasks, sessions, source, backup = legacy
    archive = backup()
    migrate("task-one", source, url, tasks, sessions, archive, apply=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    secret = "migration-test-secret-" + "x" * 32
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", secret)
    env = {**os.environ, "HARNESS_TASK_ROOT": str(tasks), "HARNESS_SESSION_ROOT": str(sessions),
           "TEST_ATELIER_HARNESS_PORT": str(port), "TEST_ATELIER_SCOPE_SECRET": secret,
           "HARNESS_DSH_COMMAND": node, "HARNESS_DSH_ARGS_JSON": "[]"}
    address = f"http://127.0.0.1:{port}/v1/tasks/task-one"

    def request(method, operation, suffix=""):
        claims = {"project_id": 4, "subject_user_id": 7, "task_id": "task-one",
                  "allowed_operation": operation}
        req = Request(address + suffix, method=method, headers={"x-test-atelier-scope": create_scope_token(claims)})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def host_check(expected_delete):
        host = subprocess.Popen([node, "host/server.mjs"], cwd=extension, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                if host.poll() is not None:
                    pytest.fail("Host exited before the Session replay check")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3):
                        break
                except (OSError, TimeoutError):
                    time.sleep(0.1)
            else:
                pytest.fail("Host did not start")
            if expected_delete == 409:
                replay = request("GET", "session.read", "/session?session_id=session-one")
                assert replay[0] == 200 and "legacy replay" in json.dumps(replay[1])
            assert request("DELETE", "task.delete")[0] == expected_delete
        finally:
            host.terminate()
            host.wait(timeout=10)

    host_check(409)
    migrate("task-one", source, url, tasks, sessions, archive, finalize=True, replay_verified=True)
    host_check(200)
    assert not (sessions / "task-one").exists()
