"""The PostgreSQL index gate must not silently pass a SQLite deployment."""

import pytest
from sqlalchemy import create_engine

from scripts import verify_artifact_directory_pg


def test_artifact_directory_pg_gate_refuses_sqlite(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(verify_artifact_directory_pg, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            verify_artifact_directory_pg.verify()
    finally:
        engine.dispose()
