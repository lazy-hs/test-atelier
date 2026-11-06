"""Portable Harness storage recovery smoke for macOS, Windows, and Linux."""

import sqlite3

import pytest

from scripts.backup_data import create_backup, restore_sqlite, verify_archive


def test_platform_round_trip_nested_sqlite_and_harness_volumes(tmp_path):
    source_data = tmp_path / "source" / "data"
    source_data.mkdir(parents=True)
    source_db = source_data / "ai_studio.db"
    with sqlite3.connect(source_db) as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('ci-head')")
    (source_data / "document.txt").write_text("source document", encoding="utf-8")
    tasks = tmp_path / "tasks"
    sessions = tmp_path / "sessions"
    tasks.mkdir()
    sessions.mkdir()
    (tasks / "task.events.jsonl").write_text('{"result":"ci fixture"}\n', encoding="utf-8")
    (sessions / "session.jsonl").write_text('{"event":"done"}\n', encoding="utf-8")

    archive = create_backup(f"sqlite:///{source_db}", source_data, tmp_path / "backups",
                            tasks, sessions, services_stopped=True)
    manifest = verify_archive(archive)
    assert manifest["harness_storage"] == "task-session-v1"
    assert not any(item["path"].startswith("data/ai_studio.db") for item in manifest["files"])

    target_data = tmp_path / "target" / "data"
    restore_sqlite(archive, target_data / "ai_studio.db", target_data, False,
                   tmp_path / "target" / "tasks", tmp_path / "target" / "sessions",
                   services_stopped=True)
    with sqlite3.connect(target_data / "ai_studio.db") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "ci-head"
    assert (target_data / "document.txt").read_text(encoding="utf-8") == "source document"
    assert (tmp_path / "target" / "tasks" / "task.events.jsonl").read_text() == '{"result":"ci fixture"}\n'
    assert (tmp_path / "target" / "sessions" / "session.jsonl").read_text() == '{"event":"done"}\n'


def test_platform_restore_requires_stopped_writers(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('ci-head')")
    tasks = tmp_path / "tasks"
    sessions = tmp_path / "sessions"
    tasks.mkdir()
    sessions.mkdir()
    with pytest.raises(ValueError, match="停机"):
        create_backup(f"sqlite:///{source}", tmp_path / "data", tmp_path / "backups",
                      tasks, sessions)
    archive = create_backup(f"sqlite:///{source}", tmp_path / "data", tmp_path / "backups",
                            tasks, sessions, services_stopped=True)
    with pytest.raises(ValueError, match="停机"):
        restore_sqlite(archive, tmp_path / "target" / "db", tmp_path / "target" / "data", False,
                       tmp_path / "target" / "tasks", tmp_path / "target" / "sessions")
    assert not (tmp_path / "target").exists()
