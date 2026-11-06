"""The PostgreSQL snapshot gate must not run against local SQLite or without opt-in."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.tests.test_projects import client, login  # noqa: F401
from api import main
from api.db import Base, Case, ImportBatch, Project, User
from scripts import verify_artifact_snapshot_pg


def test_artifact_snapshot_pg_gate_refuses_sqlite(monkeypatch):
    sqlite_engine = create_engine("sqlite://")
    monkeypatch.setattr(verify_artifact_snapshot_pg, "engine", sqlite_engine)
    monkeypatch.setenv("TEST_ATELIER_PG_SMOKE", "1")
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            verify_artifact_snapshot_pg.verify()
    finally:
        sqlite_engine.dispose()


def test_artifact_snapshot_pg_gate_requires_explicit_opt_in(monkeypatch):
    class PostgreSQLDialect:
        name = "postgresql"

    class PostgreSQLEngine:
        dialect = PostgreSQLDialect()

    monkeypatch.setattr(verify_artifact_snapshot_pg, "engine", PostgreSQLEngine())
    monkeypatch.delenv("TEST_ATELIER_PG_SMOKE", raising=False)
    with pytest.raises(RuntimeError, match="explicit TEST_ATELIER_PG_SMOKE"):
        verify_artifact_snapshot_pg.verify()


def test_project_graph_smoke_exercises_actual_route_with_concurrent_sqlite_write(client, tmp_path):
    login(client)
    database = create_engine(f"sqlite:///{tmp_path / 'graph-smoke.db'}", connect_args={"timeout": 2})
    sessions = sessionmaker(bind=database)
    try:
        Base.metadata.create_all(database)
        with database.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        with sessions.begin() as db:
            db.add(User(id=1, username="snapshot-admin", password_hash="not-used", role="admin"))
            project = Project(name="local graph smoke")
            foreign = Project(name="unrelated project")
            db.add_all([project, foreign])
            db.flush()
            batch = ImportBatch(project_id=project.id, filename="cases.xlsx", headers=[], rows=[], warnings=[])
            db.add(batch)
            db.flush()
            case = Case(project_id=project.id, import_id=batch.id, original_id="first", title="原始标题", original=[])
            db.add(case)
            db.add(Case(project_id=foreign.id, import_id=batch.id, original_id="foreign",
                        title="另一个项目", original=[]))
            db.flush()
            project_id, batch_id, case_id = project.id, batch.id, case.id

        def file_database():
            with Session(database) as db:
                yield db

        previous = main.app.dependency_overrides.get(main.get_db)
        main.app.dependency_overrides[main.get_db] = file_database
        try:
            verify_artifact_snapshot_pg.verify_project_graph_endpoint(
                client, sessions, project_id, batch_id, case_id, expected_nodes=2, expected_edges=1)
        finally:
            if previous is None:
                main.app.dependency_overrides.pop(main.get_db, None)
            else:
                main.app.dependency_overrides[main.get_db] = previous
    finally:
        database.dispose()
