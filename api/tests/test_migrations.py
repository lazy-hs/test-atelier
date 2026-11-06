import os
import hashlib
import json
import sqlite3
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest
from api.db import Base
from api.migrations.legacy_schema import metadata
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]


def test_project_write_fence_covers_every_project_owned_table():
    migration = import_module("api.migrations.versions.20261038_project_write_fence")
    mapped = {table.name for table in Base.metadata.tables.values() if "project_id" in table.c}
    # The original fence migration predates the later defect and knowledge tables.
    later_tables = {"knowledge_entries", "project_templates", "project_template_applications"}
    assert set(migration.PROJECT_TABLES) == mapped - later_tables
    owned = mapped | {"projects"}
    while True:
        discovered = {table.name for table in Base.metadata.tables.values() if table.name not in owned
                      and any(foreign.column.table.name in owned for foreign in table.foreign_keys)}
        if not discovered:
            break
        owned.update(discovered)
    assert set(migration.INDIRECT_PROJECT_QUERIES) == owned - mapped - {"projects", "knowledge_revisions"}


def test_auth_session_revocations_migration_creates_bounded_table(tmp_path):
    upgrade(tmp_path / "session-revocations.db", "head")
    with sqlite3.connect(tmp_path / "session-revocations.db") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(session_revocations)")}
        assert columns == {"nonce_hash", "user_id", "expires_at", "created_at"}
        indexes = {row[1] for row in db.execute("PRAGMA index_list(session_revocations)")}
        assert "ix_session_revocations_expires_at" in indexes


def test_project_write_fence_blocks_late_writes_but_allows_cleanup(tmp_path):
    path = tmp_path / "project-write-fence.db"
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        triggers = {name for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'ta_project_fence_%'")}
        assert len(triggers) == 80
        db.execute("INSERT INTO users(id, username, password_hash, role) VALUES (1, 'owner', 'hash', 'admin')")
        db.execute("INSERT INTO users(id, username, password_hash, role) VALUES (2, 'reader', 'hash', 'reviewer')")
        db.execute("INSERT INTO projects(id, name, created_at) VALUES (1, 'deleting', '2026-10-25')")
        db.execute("INSERT INTO memberships(project_id, user_id, role) VALUES (1, 1, 'manager')")
        db.execute("""INSERT INTO codex_sessions(id,project_id,user_id,title,status,model,working_directory,created_at,updated_at)
                      VALUES ('existing',1,1,'old','ready','','/tmp','','')""")
        db.execute("""INSERT INTO work_items(id, user_id, project_id, kind, title, description, status, created_at, updated_at)
                      VALUES (1, 1, 1, 'other', 'keep personal item', '', 'todo', '', '')""")
        db.execute("""INSERT INTO knowledge_entries(id,project_id,source_type,source_id,source_snapshot,title,body,
                      status,version,created_by,updated_by,created_at,updated_at)
                      VALUES(1,1,'document','source','{}','title','body','active',1,1,1,'','')""")
        db.execute("""INSERT INTO knowledge_revisions(entry_id,version,actor_id,title,body,status,created_at)
                      VALUES(1,1,1,'title','body','active','')""")
        db.execute("""INSERT INTO project_templates(id,project_id,name,snapshot,snapshot_sha256,created_by,created_at)
                      VALUES(1,1,'template','{}','digest',1,'')""")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="knowledge_source_immutable"):
            db.execute("UPDATE knowledge_entries SET source_id = 'other' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="knowledge_revision_immutable"):
            db.execute("UPDATE knowledge_revisions SET body = 'rewritten' WHERE entry_id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="project_template_immutable"):
            db.execute("UPDATE project_templates SET name = 'rewritten' WHERE id = 1")
        db.execute("UPDATE projects SET deletion_pending_at = '2026-10-25' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("INSERT INTO memberships(project_id, user_id, role) VALUES (1, 2, 'reader')")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("UPDATE memberships SET role = 'reader' WHERE project_id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("INSERT INTO codex_messages(session_id,role,content,created_at) VALUES ('existing','user','late','')")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("INSERT INTO codex_messages(session_id,role,content,created_at) VALUES ('missing','user','orphan','')")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("""INSERT INTO execution_reports(run_id,project_id,payload,payload_sha256,passed_count,
                        failed_count,blocked_count,skipped_count,not_run_count,case_executions,
                        estimated_seconds,actual_seconds,created_at)
                      VALUES (1,1,'{}','',0,0,0,0,0,0,0,0,'')""")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("UPDATE knowledge_entries SET body = 'late' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("""INSERT INTO knowledge_revisions(entry_id,version,actor_id,title,body,status,created_at)
                          VALUES(1,2,1,'title','late','active','')""")
        with pytest.raises(sqlite3.IntegrityError, match="project_deletion_pending"):
            db.execute("""INSERT INTO project_template_applications(project_id,source_project_id,template_id,
                          template_name,snapshot_sha256,actor_id,created_at)
                          VALUES(1,1,1,'template','digest',1,'')""")
        db.execute("DELETE FROM project_templates WHERE id = 1")
        db.execute("DELETE FROM knowledge_revisions WHERE entry_id = 1")
        db.execute("DELETE FROM knowledge_entries WHERE id = 1")
        db.execute("UPDATE work_items SET project_id = NULL WHERE id = 1")
        db.execute("DELETE FROM memberships WHERE project_id = 1")
        db.commit()
        assert db.execute("SELECT project_id FROM work_items WHERE id = 1").fetchone() == (None,)

    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "20261037_harness_deletion"],
                            cwd=ROOT, env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'ta_project_fence_%'").fetchall() == []


def test_project_write_fence_readiness_rejects_a_missing_trigger(tmp_path):
    from api.project_write_fence import assert_project_write_fence

    path = tmp_path / "missing-trigger.db"
    upgrade(path, "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with Session(engine) as db:
            assert_project_write_fence(db)
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER ta_project_fence_cases_insert")
        with Session(engine) as db, pytest.raises(RuntimeError, match="触发器缺失"):
            assert_project_write_fence(db)
    finally:
        engine.dispose()


def upgrade(path: Path, revision: str):
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=ROOT,
                            env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_artifact_directory_indexes_are_used_and_reversible(tmp_path):
    path = tmp_path / "artifact-directory-indexes.db"
    upgrade(path, "head")
    plans = {
        "ix_cases_project_directory_prefix": """SELECT id FROM cases WHERE project_id = 1
            AND substr(directory, 1, 128) = '业务' AND directory = '业务' ORDER BY id""",
        "ix_governance_change_project_entity_path": """SELECT id FROM governance_change_logs
            WHERE project_id = 1 AND entity = 'directory' AND entity_id = '业务' ORDER BY id""",
    }
    with sqlite3.connect(path) as db:
        for name, query in plans.items():
            plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + query))
            assert name in plan, plan
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "20261043_document_references"],
                            cwd=ROOT, env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        remaining = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert not set(plans).intersection(remaining)


def test_document_source_index_backfills_and_tracks_insert_update_delete(tmp_path):
    from api.db import AnalysisRun, HarnessTask, Project, User

    path = tmp_path / "document-references.db"
    upgrade(path, "20261042_project_templates")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with Session(engine) as db:
            db.add_all([User(id=1, username="writer", password_hash="hash", role="admin"),
                        Project(id=1, name="sources")])
            db.flush()
            # The fixture is intentionally on an older revision; the current ORM has newer document columns.
            db.connection().exec_driver_sql(
                "INSERT INTO source_documents(id,project_id,user_id,filename,extracted_text,warnings,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("doc-one", 1, 1, "one.md", "one", "[]", "2026-10-25"),
            )
            db.connection().exec_driver_sql(
                "INSERT INTO source_documents(id,project_id,user_id,filename,extracted_text,warnings,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("doc-two", 1, 1, "two.md", "two", "[]", "2026-10-25"),
            )
            db.add_all([
                AnalysisRun(id="old-analysis", project_id=1, user_id=1, source_ids=["doc-one"], result={}),
                HarnessTask(id="old-task", project_id=1, user_id=1, task_type="requirement-analysis",
                            result_schema="requirement-analysis.v1", skill_id="requirement-analysis",
                            source_document_ids=["doc-one"], idempotency_key="old", result={}, error={}),
            ])
            db.commit()
    finally:
        engine.dispose()
    upgrade(path, "head")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.artifact_routes import build_router

    engine = create_engine(f"sqlite:///{path}")
    try:
        def get_db():
            with Session(engine) as db:
                yield db

        def project_access(project_id, _user, _db):
            assert project_id == 1

        app = FastAPI()
        app.include_router(build_router(get_db, lambda: User(id=1, username="writer"),
                                        project_access))
        with TestClient(app) as client:
            indexed = client.get("/api/v1/projects/1/artifacts/document/doc-one/relations")
            assert indexed.status_code == 200, indexed.text
            assert {edge["to"]["id"] for edge in indexed.json()["edges"]} == {"old-analysis", "old-task"}
    finally:
        engine.dispose()
    with sqlite3.connect(path) as db:
        def refs():
            return db.execute("SELECT document_id,source_kind,source_id FROM document_references ORDER BY 1,2,3").fetchall()

        assert refs() == [("doc-one", "analysis", "old-analysis"),
                          ("doc-one", "harness_task", "old-task")]
        plan = db.execute("EXPLAIN QUERY PLAN SELECT source_id FROM document_references "
                          "WHERE document_id = 'doc-one' AND source_kind = 'analysis' ORDER BY source_id").fetchall()
        assert any("SEARCH document_references USING" in row[3] for row in plan)
        db.execute("UPDATE analysis_runs SET source_ids = ? WHERE id = 'old-analysis'", ('["doc-two"]',))
        db.execute("UPDATE harness_tasks SET source_document_ids = ? WHERE id = 'old-task'", ('["doc-two", "doc-two"]',))
        assert refs() == [("doc-two", "analysis", "old-analysis"),
                          ("doc-two", "harness_task", "old-task")]
        db.execute("DELETE FROM harness_tasks WHERE id = 'old-task'")
        db.execute("DELETE FROM analysis_runs WHERE id = 'old-analysis'")
        assert refs() == []
        assert {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ta_document_refs_%'")} == {
            f"ta_document_refs_{kind}_{event}" for kind in ("analysis", "harness_task")
            for event in ("insert", "update", "delete")}


def test_source_document_upload_identity_keeps_legacy_rows_unverified_and_downgrades(tmp_path):
    path = tmp_path / "source-identity.db"
    upgrade(path, "20261044_artifact_directory_indexes")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'owner','hash','admin')")
        db.execute("INSERT INTO projects(id,name,created_at) VALUES(1,'old','2026-10-25')")
        db.execute("""INSERT INTO source_documents(id,project_id,user_id,filename,extracted_text,warnings,created_at)
                      VALUES('legacy',1,1,'legacy.md','body','[]','2026-10-25')""")
        db.commit()
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        columns = {column[1] for column in db.execute("PRAGMA table_info(source_documents)")}
        assert {"source_sha256", "source_size"} <= columns
        assert db.execute("SELECT source_sha256,source_size FROM source_documents WHERE id='legacy'").fetchone() == (None, None)
        db.execute("UPDATE source_documents SET source_sha256=?,source_size=? WHERE id='legacy'", ("a" * 64, 4))
        db.commit()
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "20261044_artifact_directory_indexes"],
                            cwd=ROOT, env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT filename FROM source_documents WHERE id='legacy'").fetchone() == ("legacy.md",)
        assert not {"source_sha256", "source_size"}.intersection(
            {column[1] for column in db.execute("PRAGMA table_info(source_documents)")})
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT source_sha256,source_size FROM source_documents WHERE id='legacy'").fetchone() == (None, None)


def test_conversion_output_creation_identity_preserves_legacy_rows_and_downgrades(tmp_path):
    path = tmp_path / "conversion-identity.db"
    upgrade(path, "20261045_source_document_integrity")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'owner','hash','admin')")
        db.execute("INSERT INTO projects(id,name,created_at) VALUES(1,'old','2026-10-25')")
        db.execute("""INSERT INTO conversion_records(id,project_id,user_id,created_at,source_filename,
                      output_filename,conversion_mode,output_mode,media_type,app_version,row_count,file_size,preview)
                      VALUES('old-conversion',1,1,'2026-10-25','old.xlsx','new.xlsx',
                      'excel_to_excel','after','application/octet-stream','0.1',1,4,'{}')""")
        db.commit()
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT output_sha256 FROM conversion_records WHERE id='old-conversion'").fetchone() == (None,)
        db.execute("UPDATE conversion_records SET output_sha256=? WHERE id='old-conversion'", ("a" * 64,))
        db.commit()
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "20261045_source_document_integrity"],
                            cwd=ROOT, env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as db:
        assert "output_sha256" not in {column[1] for column in db.execute("PRAGMA table_info(conversion_records)")}
        assert db.execute("SELECT output_filename FROM conversion_records WHERE id='old-conversion'").fetchone() == ("new.xlsx",)
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT output_sha256 FROM conversion_records WHERE id='old-conversion'").fetchone() == (None,)


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
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261047_auth_session_revocations"
        assert {column[1] for column in db.execute("PRAGMA table_info(execution_defects)")} >= {
            "project_id", "run_id", "run_case_id", "version", "created_by", "updated_by"}
        assert {column[1] for column in db.execute("PRAGMA table_info(execution_defect_events)")} >= {
            "defect_id", "actor_id", "before", "after"}
        assert db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='ta_project_fence_execution_defects_insert'").fetchone()
        assert {column[1] for column in db.execute("PRAGMA table_info(knowledge_entries)")} >= {
            "project_id", "source_type", "source_id", "source_snapshot", "version"}
        assert {column[1] for column in db.execute("PRAGMA table_info(knowledge_revisions)")} >= {
            "entry_id", "version", "actor_id", "body"}
        assert db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name='ta_project_fence_knowledge_revisions_insert'").fetchone()
        assert {column[1] for column in db.execute("PRAGMA table_info(project_templates)")} >= {
            "project_id", "snapshot", "snapshot_sha256", "created_by"}
        assert {column[1] for column in db.execute("PRAGMA table_info(project_template_applications)")} >= {
            "project_id", "source_project_id", "template_id", "snapshot_sha256"}
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_ai_jobs_status'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_suggestion_jobs_status'").fetchone()
        assert "request_id" in {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")}
        assert "request_id" in {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")}
        assert {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")} >= {"lease_owner", "lease_until", "lease_heartbeat"}
        assert {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")} >= {"lease_owner", "lease_until", "lease_heartbeat"}
        assert {column[1] for column in db.execute("PRAGMA table_info(ai_jobs)")} >= {"queue_reserved"}
        assert {column[1] for column in db.execute("PRAGMA table_info(suggestion_jobs)")} >= {"queue_reserved"}
        assert "assignee_id" in {column[1] for column in db.execute("PRAGMA table_info(cases)")}
        assert {"created_at", "updated_at"} <= {column[1] for column in db.execute("PRAGMA table_info(cases)")}
        assert db.execute("SELECT final_categories,final_taxonomy_version_id FROM cases WHERE id=1").fetchone() == (None, None)
        assert "action" in {column[1] for column in db.execute("PRAGMA table_info(review_events)")}
        assert any(key[2] == "users" and key[3] == "assignee_id" for key in db.execute("PRAGMA foreign_key_list(cases)"))
        assert any(key[2] == "taxonomy_versions" and key[3] == "final_taxonomy_version_id" for key in db.execute("PRAGMA foreign_key_list(cases)"))
        assert db.execute("SELECT name FROM sqlite_master WHERE name='project_skills'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_queue_state'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='governance_duplicate_groups'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='governance_change_logs'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_models'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_tools'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='codex_sessions'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='codex_messages'").fetchone()
        assert "deletion_pending_at" in {column[1] for column in db.execute("PRAGMA table_info(projects)")}
        harness_task_columns = {column[1] for column in db.execute("PRAGMA table_info(harness_tasks)")}
        assert {"projector_owner", "projector_lease_until", "projector_lease_heartbeat", "projector_attempts", "projector_error", "retry_of"} <= harness_task_columns
        harness_event_columns = {column[1] for column in db.execute("PRAGMA table_info(harness_task_events)")}
        assert "remote_sequence" in harness_event_columns
        assert db.execute("SELECT name FROM sqlite_master WHERE name='uq_harness_task_event_remote_sequence'").fetchone()
        model_columns = {column[1] for column in db.execute("PRAGMA table_info(ai_models)")}
        assert {"provider_id", "scope", "endpoint", "model", "api_key_encrypted"} <= model_columns
        tool_columns = {column[1] for column in db.execute("PRAGMA table_info(ai_tools)")}
        assert {"tool_id", "command", "model", "enabled"} <= tool_columns
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
        assert db.execute("SELECT name FROM sqlite_master WHERE name='execution_reports'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='run_cases'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='execution_events'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='governance_validation_runs'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='governance_directories'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_settings'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='project_skills'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_models'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ai_tools'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='codex_sessions'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE name='codex_messages'").fetchone()


def test_completed_execution_report_backfill_preserves_source_and_skips_missing_cases(tmp_path):
    path = tmp_path / "historical-execution.db"
    upgrade(path, "20261038_project_write_fence")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES (1,'owner','hash','admin')")
        db.execute("INSERT INTO projects(id,name,created_at) VALUES (1,'legacy','2026-10-25')")
        db.execute("""INSERT INTO import_batches(id,project_id,filename,headers,rows,warnings,status,created_at,kind)
                      VALUES (1,1,'cases.xlsx','[]','[]','[]','done','2026-10-25','cases')""")
        db.execute("""INSERT INTO cases(id,project_id,import_id,original_id,title,directory,level,smoke,original,
                        review_status,version,governance_status,created_at,updated_at)
                      VALUES (1,1,1,'001','旧用例','','1','是','[]','reviewed',2,'active','2026-10-25','2026-10-25')""")
        db.execute("""INSERT INTO smoke_suites(id,project_id,title,target_version,environment,required_chains,
                        revision,created_by,created_at,updated_at)
                      VALUES (1,1,'旧套件','v1','macOS','["登录"]',2,1,'2026-10-25','2026-10-25')""")
        for run_id in (1, 2):
            db.execute("""INSERT INTO execution_runs(id,project_id,suite_id,suite_revision,title,target_version,
                            environment,required_chains,status,started_by,started_at,completed_by,completed_at)
                          VALUES (?,1,1,2,'旧套件','v1','macOS','["登录"]','completed',1,'2026-10-25',1,'2026-10-25')""",
                       (run_id,))
        db.execute("""INSERT INTO run_cases(id,run_id,case_id,case_version,original_id,title,level,smoke,chain_name,
                        estimated_seconds,result,actual_seconds,note,executed_by,executed_at,version)
                      VALUES (1,1,1,2,'001','冻结标题','1','是','登录',120,'passed',90,'',1,'2026-10-25',2)""")
        db.commit()
    upgrade(path, "head")
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        snapshots = db.execute("SELECT run_id,project_id,payload,payload_sha256,passed_count,case_executions,completeness FROM execution_reports").fetchall()
        assert len(snapshots) == 1 and snapshots[0][:2] == (1, 1)
        payload = json.loads(snapshots[0][2])
        assert payload["cases"][0]["original_id"] == "001" and payload["cases"][0]["title"] == "冻结标题"
        assert snapshots[0][4:] == (1, 1, "legacy_unverified")
        assert payload["source"]["historical_completeness"] == "unverified"
        unsigned = {key: value for key, value in payload.items() if key != "integrity"}
        expected = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        assert snapshots[0][3] == expected == payload["integrity"]["payload_sha256"]


def test_metadata_initialized_harness_tables_are_adopted_without_data_loss(tmp_path):
    """Older dev starts could create Harness tables before Alembic recorded them."""
    path = tmp_path / "metadata-harness.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY);
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE analysis_runs (id VARCHAR(36) PRIMARY KEY);
            CREATE TABLE harness_tasks (
                id VARCHAR(64) PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                task_type VARCHAR(40) NOT NULL,
                task_version VARCHAR(16) NOT NULL DEFAULT 'v1',
                result_schema VARCHAR(64) NOT NULL,
                skill_id VARCHAR(80) NOT NULL,
                source_document_ids JSON NOT NULL,
                analysis_run_id VARCHAR(36) REFERENCES analysis_runs(id),
                session_id VARCHAR(128),
                session_ids JSON NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                sequence INTEGER NOT NULL DEFAULT 0,
                idempotency_key VARCHAR(160) NOT NULL,
                result JSON NOT NULL,
                error JSON NOT NULL,
                projector_owner VARCHAR(128) NOT NULL DEFAULT '',
                projector_lease_until FLOAT NOT NULL DEFAULT 0,
                projector_lease_heartbeat FLOAT NOT NULL DEFAULT 0,
                projector_attempts INTEGER NOT NULL DEFAULT 0,
                projector_error TEXT NOT NULL DEFAULT '',
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                UNIQUE(project_id, user_id, idempotency_key)
            );
            CREATE TABLE harness_task_events (
                id INTEGER PRIMARY KEY,
                task_id VARCHAR(64) NOT NULL REFERENCES harness_tasks(id),
                sequence INTEGER NOT NULL,
                remote_sequence INTEGER,
                event VARCHAR(64) NOT NULL,
                payload JSON NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                UNIQUE(task_id, sequence),
                UNIQUE(task_id, remote_sequence)
            );
        """)
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("INSERT INTO alembic_version(version_num) VALUES ('20261032_case_timestamps')")
        db.execute("INSERT INTO harness_tasks(id,project_id,user_id,task_type,task_version,result_schema,skill_id,source_document_ids,session_ids,status,sequence,idempotency_key,result,error,projector_owner,projector_lease_until,projector_lease_heartbeat,projector_attempts,projector_error,created_at,updated_at) VALUES ('legacy-task',1,1,'requirement-analysis','v1','requirement-analysis.v1','requirement-analysis','[]','[]','failed',1,'legacy-key','{}','{}','',0,0,0,'','','')")
        db.commit()
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261047_auth_session_revocations"
        assert db.execute("SELECT id FROM harness_tasks").fetchone()[0] == "legacy-task"
        assert "retry_of" in {column[1] for column in db.execute("PRAGMA table_info(harness_tasks)")}


def test_partially_initialized_harness_tables_are_completed_without_data_loss(tmp_path):
    """Adoption must handle a task table created before its event table."""
    path = tmp_path / "partial-harness.db"
    upgrade(path, "20261032_case_timestamps")
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE harness_tasks (
                id VARCHAR(64) PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                task_type VARCHAR(40) NOT NULL,
                task_version VARCHAR(16) NOT NULL DEFAULT 'v1',
                result_schema VARCHAR(64) NOT NULL,
                skill_id VARCHAR(80) NOT NULL,
                source_document_ids JSON NOT NULL,
                analysis_run_id VARCHAR(36) REFERENCES analysis_runs(id),
                session_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                sequence INTEGER NOT NULL DEFAULT 0,
                idempotency_key VARCHAR(160) NOT NULL,
                result JSON NOT NULL,
                error JSON NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                UNIQUE(project_id, user_id, idempotency_key)
            );
            INSERT INTO harness_tasks
                (id, project_id, user_id, task_type, task_version, result_schema,
                 skill_id, source_document_ids, status, sequence, idempotency_key,
                 result, error, created_at, updated_at)
            VALUES
                ('partial-task', 1, 1, 'requirement-analysis', 'v1',
                 'requirement-analysis.v1', 'requirement-analysis', '[]',
                 'failed', 1, 'partial-key', '{}', '{}', '', '');
        """)
        db.commit()
    upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id FROM harness_tasks").fetchone() == ("partial-task",)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='harness_task_events'").fetchone()
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_harness_tasks_status'").fetchone()
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261047_auth_session_revocations"


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
