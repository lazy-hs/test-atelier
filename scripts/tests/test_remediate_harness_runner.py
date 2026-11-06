import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import create_engine, text

from scripts.audit_harness_project_storage import AuditError, audit
from scripts.backup_data import create_backup
from scripts.migrate_legacy_harness_session import MigrationError
from scripts.remediate_harness_runner import RemediationError, remediate
from api.harness_bridge import create_scope_token


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def quarantined(tmp_path):
    database = tmp_path / "atelier.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE projects (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE harness_tasks (id TEXT PRIMARY KEY, project_id INTEGER, "
                                "user_id INTEGER, status TEXT, session_id TEXT, session_ids TEXT)"))
        connection.execute(text("INSERT INTO projects VALUES (4)"))
        connection.execute(text("INSERT INTO harness_tasks VALUES "
                                "('task-one', 4, 7, 'failed', 'session-one', '[\"session-one\"]')"))
    engine.dispose()
    tasks, sessions, data, backups = (tmp_path / name for name in ("tasks", "sessions", "data", "backups"))
    for directory in (tasks, sessions, data, backups):
        directory.mkdir()
    private = sessions / "task-one" / "session"
    private.mkdir(parents=True)
    (private / "session.v4.jsonl").write_text(json.dumps({"type": "session", "id": "session-one"}) + "\n")
    index = tasks / "task-one.json"
    index.write_text(json.dumps({
        "id": "task-one", "project_id": 4, "user_id": 7, "status": "failed", "sequence": 2,
        "session_id": "session-one", "session_ids": ["session-one"],
        "termination_unconfirmed": True, "runner_spawn_intent": True,
    }))
    replay = tasks / "task-one.events.json"
    replay.write_text(json.dumps([
        {"event": "task.started", "sequence": 1, "payload": {"task_id": "task-one", "sequence": 1}},
        {"event": "task.failed", "sequence": 2, "payload": {"task_id": "task-one", "sequence": 2,
         "error": {"code": "host_restarted"}}},
    ]))
    url = f"sqlite:///{database}"
    archive = create_backup(url, data, backups, tasks, sessions, services_stopped=True)
    return url, tasks, sessions, archive, index, replay


def _review(quarantined, **kwargs):
    url, tasks, sessions, archive, _index, _replay = quarantined
    return remediate("task-one", url, tasks, sessions, archive, **kwargs)


def test_remediation_previews_then_requires_explicit_runner_stop_review(quarantined):
    url, tasks, sessions, archive, index, _replay = quarantined
    assert audit(4, url, tasks, sessions)["unconfirmed_runner_stops"] == 1
    assert _review(quarantined)["status"] == "ready"
    assert json.loads(index.read_text())["termination_unconfirmed"] is True
    with pytest.raises(RemediationError, match="operator must verify"):
        _review(quarantined, apply=True)
    result = _review(quarantined, apply=True, runner_tree_verified=True)
    assert result["status"] == "cleared" and result["other_quarantined_tasks"] == 0
    updated = json.loads(index.read_text())
    assert updated["termination_unconfirmed"] is False
    assert updated["runner_remediation"]["method"] == "offline_verified_stop"
    assert len(updated["runner_remediation"]["backup_manifest_sha256"]) == 64
    assert audit(4, url, tasks, sessions)["status"] == "consistent"
    assert (sessions / "task-one/session/session.v4.jsonl").is_file()
    with pytest.raises(RemediationError, match="not in runner quarantine"):
        _review(quarantined, apply=True, runner_tree_verified=True)


@pytest.mark.parametrize("change", ["backup_drift", "db_drift", "replay_drift", "index_drift", "link"])
def test_remediation_rejects_unverified_or_ambiguous_state(quarantined, change, tmp_path):
    url, tasks, sessions, archive, index, replay = quarantined
    if change == "backup_drift":
        (sessions / "task-one/session/session.v4.jsonl").write_text("tampered\n")
    elif change == "db_drift":
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("UPDATE harness_tasks SET user_id = 8"))
        engine.dispose()
    elif change == "replay_drift":
        events = json.loads(replay.read_text())
        events[-1]["event"] = "task.completed"
        replay.write_text(json.dumps(events))
    elif change == "index_drift":
        saved = json.loads(index.read_text())
        saved["session_ids"] = ["other-session"]
        index.write_text(json.dumps(saved))
    elif change == "link":
        outside = tmp_path / "outside"
        outside.mkdir()
        link = sessions / "task-one" / "redirect"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks are unavailable")
    with pytest.raises((RemediationError, AuditError, MigrationError, ValueError)):
        _review(quarantined, apply=True, runner_tree_verified=True)
    assert json.loads(index.read_text())["termination_unconfirmed"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction runtime contract")
def test_remediation_rejects_real_windows_junction(quarantined, tmp_path):
    _url, _tasks, sessions, _archive, index, _replay = quarantined
    external = tmp_path / "external"
    external.mkdir()
    redirect = sessions / "task-one" / "redirect"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(redirect), str(external)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    with pytest.raises((RemediationError, AuditError, MigrationError)):
        _review(quarantined, apply=True, runner_tree_verified=True)
    assert json.loads(index.read_text())["termination_unconfirmed"] is True


def test_remediation_cli_is_preview_first_and_redacts_errors(quarantined):
    url, tasks, sessions, archive, index, _replay = quarantined
    command = [sys.executable, str(ROOT / "scripts/remediate_harness_runner.py"),
               "--task-id", "task-one", "--task-root", str(tasks), "--session-root", str(sessions),
               "--backup-archive", str(archive), "--database-url", url]
    missing_stop = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, check=False)
    assert missing_stop.returncode != 0
    preview = subprocess.run(command + ["--services-stopped"], capture_output=True, text=True, cwd=ROOT, check=False)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["status"] == "ready"
    denied = subprocess.run(command + ["--services-stopped", "--apply"],
                            capture_output=True, text=True, cwd=ROOT, check=False)
    assert denied.returncode == 1
    assert json.loads(denied.stderr)["error_type"] == "RemediationError"
    assert json.loads(index.read_text())["termination_unconfirmed"] is True


def test_remediated_index_reloads_and_allows_host_scoped_deletion(quarantined, monkeypatch):
    extensions = ROOT.parent / "test-atelier-harness-extensions"
    if not (extensions / "host/server.mjs").is_file() or not shutil.which("node"):
        pytest.skip("Independent Extension Host and Node are needed for integration")
    url, tasks, sessions, _archive, _index, _replay = quarantined
    _review(quarantined, apply=True, runner_tree_verified=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    secret = "r" * 32
    monkeypatch.setenv("TEST_ATELIER_SCOPE_SECRET", secret)
    env = {**os.environ, "TEST_ATELIER_HARNESS_PORT": str(port), "TEST_ATELIER_SCOPE_SECRET": secret,
           "HARNESS_DSH_COMMAND": sys.executable, "HARNESS_DSH_ARGS_JSON": "[]",
           "HARNESS_TASK_ROOT": str(tasks), "HARNESS_SESSION_ROOT": str(sessions)}
    host = subprocess.Popen(["node", "host/server.mjs"], cwd=extensions, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if host.poll() is not None:
                raise AssertionError(f"Host exited during startup: {host.returncode}")
            try:
                with urlopen(f"{base}/health", timeout=0.2) as response:
                    assert json.load(response)["unconfirmed_runner_stops"] == 0
                    break
            except URLError:
                time.sleep(0.05)
        else:
            raise AssertionError("Host failed to become ready")
        scope = create_scope_token({"project_id": 4, "subject_user_id": 7,
                                    "task_id": "task-one", "allowed_operation": "task.delete"})
        request = Request(f"{base}/v1/tasks/task-one", method="DELETE",
                          headers={"X-Test-Atelier-Scope": scope})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["status"] == "deleted"
        assert not (sessions / "task-one").exists()
        assert not (tasks / "task-one.json").exists()
    finally:
        host.terminate()
        host.communicate(timeout=10)
