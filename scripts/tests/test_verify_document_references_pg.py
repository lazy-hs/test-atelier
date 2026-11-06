import pytest
from sqlalchemy import create_engine

from scripts import verify_document_references_pg


def test_postgresql_document_reference_smoke_refuses_sqlite(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(verify_document_references_pg, "engine", engine)
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            verify_document_references_pg.verify()
    finally:
        engine.dispose()
