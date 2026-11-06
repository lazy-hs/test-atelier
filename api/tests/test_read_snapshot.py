"""Evidence reads must retain one database view while writes continue."""

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from api.artifact_routes import _case_catalog_pages, _verified_import_pages
from api.db import Base, Case, ImportBatch, Project
from api.read_snapshot import consistent_read


def test_file_backed_sqlite_read_transaction_survives_concurrent_commit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}", connect_args={"timeout": 2})
    records = Table("records", MetaData(), Column("id", Integer, primary_key=True), Column("title", String(40)))
    records.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    with engine.begin() as connection:
        connection.execute(insert(records).values(id=1, title="original"))

    with consistent_read(engine) as snapshot:
        assert snapshot.scalar(select(records.c.title).where(records.c.id == 1)) == "original"
        with engine.begin() as writer:
            writer.execute(update(records).where(records.c.id == 1).values(title="changed"))
            writer.execute(insert(records).values(id=2, title="new"))
        assert snapshot.scalars(select(records.c.title).order_by(records.c.id)).all() == ["original"]
    with engine.connect() as connection:
        assert connection.execute(select(records.c.title).order_by(records.c.id)).scalars().all() == ["changed", "new"]
    engine.dispose()


def test_shared_cache_isolation_is_restored_after_failure(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'isolation.db'}", poolclass=QueuePool, pool_size=1)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA read_uncommitted=1")
    with pytest.raises(RuntimeError, match="failed read"):
        with consistent_read(engine) as snapshot:
            assert snapshot.connection().exec_driver_sql("PRAGMA read_uncommitted").scalar() == 0
            raise RuntimeError("failed read")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA read_uncommitted").scalar() == 1
    engine.dispose()


def test_artifact_projections_share_one_file_backed_read_view_during_commit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'artifact-snapshot.db'}", connect_args={"timeout": 2})
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        with Session(engine) as writer:
            project = Project(name="snapshot-projection")
            writer.add(project)
            writer.flush()
            batch = ImportBatch(project_id=project.id, filename="cases.xlsx", headers=[],
                                rows=[], warnings=[], status="done")
            writer.add(batch)
            writer.flush()
            writer.add_all(Case(project_id=project.id, import_id=batch.id, original_id=str(index),
                                title=f"原标题 {index}", original=[]) for index in range(3))
            writer.flush()
            project_id, batch_id = project.id, batch.id
            first_case_id = writer.scalar(select(Case.id).where(Case.project_id == project_id)
                                          .order_by(Case.id).limit(1))
            writer.commit()

        with consistent_read(engine) as snapshot:
            catalog = list(_case_catalog_pages(project_id, snapshot))
            relations = _verified_import_pages(project_id, str(batch_id), snapshot)
            with Session(engine) as writer:
                writer.execute(update(Case).where(Case.id == first_case_id).values(title="并发改写"))
                writer.add(Case(project_id=project_id, import_id=batch_id, original_id="new",
                                title="并发新增", original=[]))
                writer.commit()
            assert list(_case_catalog_pages(project_id, snapshot)) == catalog
            assert _verified_import_pages(project_id, str(batch_id), snapshot) == relations

        with consistent_read(engine) as fresh:
            current_catalog = list(_case_catalog_pages(project_id, fresh))
            current_relations = _verified_import_pages(project_id, str(batch_id), fresh)
            assert current_catalog != catalog and current_relations != relations
            assert len(current_catalog[0][1]["items"]) == 4
            assert len(current_relations[0]["edges"]) == 4
    finally:
        engine.dispose()
