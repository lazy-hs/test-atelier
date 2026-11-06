"""The PostgreSQL export drill cannot accidentally run on a local database."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api import ai_routes
from api.db import Base, Project, User
from scripts import verify_project_export_pg


def test_project_export_pg_gate_refuses_sqlite(monkeypatch):
    sqlite_engine = create_engine("sqlite://")
    monkeypatch.setattr(verify_project_export_pg, "engine", sqlite_engine)
    monkeypatch.setenv("TEST_ATELIER_PG_SMOKE", "1")
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            verify_project_export_pg.verify()
    finally:
        sqlite_engine.dispose()


def test_project_export_pg_gate_requires_explicit_opt_in(monkeypatch):
    class PostgreSQLDialect:
        name = "postgresql"

    class PostgreSQLEngine:
        dialect = PostgreSQLDialect()

    monkeypatch.setattr(verify_project_export_pg, "engine", PostgreSQLEngine())
    monkeypatch.delenv("TEST_ATELIER_PG_SMOKE", raising=False)
    with pytest.raises(RuntimeError, match="explicit TEST_ATELIER_PG_SMOKE"):
        verify_project_export_pg.verify()


def test_evidence_drill_cleans_temporary_project_and_detects_missing_file(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'drill.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        with sessions.begin() as db:
            db.add(User(username="drill-actor", password_hash="not-a-login"))
        root = tmp_path / "documents"
        monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", root)
        verify_project_export_pg._exercise(engine, sessions, root)
        with sessions() as db:
            assert db.scalars(select(Project.id)).all() == []
        assert list(root.glob("**/*.bin")) == []
    finally:
        engine.dispose()
