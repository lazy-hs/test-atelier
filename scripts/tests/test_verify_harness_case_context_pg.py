"""Keep the PostgreSQL concurrency drill opt-in and migration-aware."""

import pytest
from sqlalchemy import create_engine

from scripts import verify_harness_case_context_pg


def test_case_context_pg_gate_rejects_sqlite(monkeypatch):
    sqlite_engine = create_engine("sqlite://")
    monkeypatch.setattr(verify_harness_case_context_pg, "engine", sqlite_engine)
    monkeypatch.setenv("TEST_ATELIER_PG_SMOKE", "1")
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            verify_harness_case_context_pg.verify()
    finally:
        sqlite_engine.dispose()


def test_case_context_pg_gate_requires_opt_in(monkeypatch):
    class PostgreSQLDialect:
        name = "postgresql"

    class PostgreSQLEngine:
        dialect = PostgreSQLDialect()

    monkeypatch.setattr(verify_harness_case_context_pg, "engine", PostgreSQLEngine())
    monkeypatch.delenv("TEST_ATELIER_PG_SMOKE", raising=False)
    with pytest.raises(RuntimeError, match="explicit TEST_ATELIER_PG_SMOKE"):
        verify_harness_case_context_pg.verify()
