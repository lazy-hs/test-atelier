import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from api.migrations.legacy_schema import metadata
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[2]


def upgrade(path: Path, revision: str):
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=ROOT,
                            env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_existing_database_roles_are_backfilled_without_data_loss(tmp_path):
    path = tmp_path / "existing.db"
    metadata.create_all(create_engine(f"sqlite:///{path}"))
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO users(id, username, password_hash, role) VALUES(?,?,?,?)", [
            (1, "admin", "hash", "admin"), (2, "reader", "hash", "reader"), (3, "tester", "hash", "reviewer")])
        db.execute("INSERT INTO projects(id, name, created_at) VALUES(1,'existing','2026-10-15')")
        db.executemany("INSERT INTO memberships(project_id, user_id) VALUES(1,?)", [(1,), (2,), (3,)])
        db.execute("""INSERT INTO import_batches(id,project_id,filename,headers,rows,warnings,status,created_at)
                      VALUES(1,1,'source.xlsx','["测试用例ID","用例名称"]','[["001","旧用例"]]','[]','done','2026-10-15')""")
        db.execute("""INSERT INTO cases(id,project_id,import_id,original_id,title,directory,level,smoke,original,
                      final_level,final_smoke,review_status,version,reviewer,reviewed_at)
                      VALUES(1,1,1,'001','旧用例','','1','','["001","旧用例"]','1',NULL,'reviewed',2,'tester','2026-10-15')""")
        db.execute("""INSERT INTO review_events(id,case_id,user_id,before,after,reason,created_at)
                      VALUES(1,1,3,'{"status":"pending"}','{"status":"reviewed"}','人工核对','2026-10-15')""")
        db.commit()
    upgrade(path, "head")
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT user_id, role FROM memberships ORDER BY user_id").fetchall() == [
            (1, "manager"), (2, "reader"), (3, "reviewer")]
        assert db.execute("SELECT username FROM users ORDER BY id").fetchall() == [("admin",), ("reader",), ("tester",)]
        assert db.execute("SELECT original_id,smoke,final_smoke,version FROM cases WHERE id=1").fetchone() == ("001", "", None, 2)
        assert db.execute("SELECT action,reason FROM review_events WHERE id=1").fetchone() == ("review", "人工核对")
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261024_queue_capacity"
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_ai_jobs_status'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_suggestion_jobs_status'").fetchone()
        assert "request_id" in {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")}
        assert "request_id" in {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")}
        assert {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")} >= {"lease_owner", "lease_until", "lease_heartbeat"}
        assert {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")} >= {"lease_owner", "lease_until", "lease_heartbeat"}
        assert {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")} >= {"queue_reserved"}
        assert {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")} >= {"queue_reserved"}
        assert "assignee_id" in {column[1] for column in db.execute("PRAGMA table_info(cases)")}
        assert db.execute("SELECT final_categories,final_taxonomy_version_id FROM cases WHERE id=1").fetchone() == (None, None)
        assert "action" in {column[1] for column in db.execute("PRAGMA table_info(review_events)")}
        assert any(key[2] == "users" and key[3] == "assignee_id" for key in db.execute("PRAGMA foreign_key_list(cases)"))
        assert any(key[2] == "taxonomy_versions" and key[3] == "final_taxonomy_version_id" for key in db.execute("PRAGMA foreign_key_list(cases)"))
        assert db.execute("SELECT name FROM sqlite_master WHERE name='project_skills'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_queue_state'").fetchone()
        assert db.execute("SELECT id,active_jobs FROM ai_queue_state").fetchone() == (1, 0)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_ai_jobs_status'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_suggestion_jobs_status'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_ai_jobs_lease_until'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_suggestion_jobs_lease_until'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_queue_state'").fetchone()


def test_fresh_database_reaches_team_schema(tmp_path):
    path = tmp_path / "fresh.db"
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        columns = {column[1] for column in db.execute("PRAGMA table_info(memberships)")}
        assert "role" in columns
        assert db.execute("SELECT name FROM sqlite_master WHERE name='membership_events'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='case_comments'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='taxonomy_versions'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='case_suggestions'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='smoke_suites'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='smoke_items'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='execution_runs'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='run_cases'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='execution_events'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_settings'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='project_skills'").fetchone()


def test_queue_capacity_migration_recovers_a_partially_created_state_table(tmp_path):
    path = tmp_path / "partial.db"
    upgrade(path, "20261023_job_leases")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ai_queue_state (id INTEGER PRIMARY KEY, active_jobs INTEGER NOT NULL, updated_at TEXT NOT NULL)")
        db.execute("INSERT INTO ai_queue_state(id, active_jobs, updated_at) VALUES (1, 99, '')")
        db.commit()
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id, active_jobs FROM ai_queue_state").fetchone() == (1, 0)
        assert "queue_reserved" in {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")}
