"""Exercise a complete evidence ZIP against a concurrent PostgreSQL write."""

import json
import hashlib
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, select, text

from api import ai_routes
from api.db import Case, ImportBatch, Project, SessionLocal, SourceDocument, User, engine
from api.project_export_routes import _export
from api.read_snapshot import consistent_read
from api.storage import remove_private_file, write_private_atomic
from scripts.verify_project_export import verify as verify_archive


def verify() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("project export smoke requires PostgreSQL")
    if os.getenv("TEST_ATELIER_PG_SMOKE") != "1":
        raise RuntimeError("project export smoke requires explicit TEST_ATELIER_PG_SMOKE=1")
    with SessionLocal() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        if head != "20261047_auth_session_revocations":
            raise RuntimeError(f"unexpected migration head: {head}")
    _exercise(engine, SessionLocal, ai_routes.DOCUMENT_DIR)


def _exercise(database, sessions, document_root: Path) -> None:
    project_id = None
    document_path = None
    try:
        with sessions.begin() as db:
            actor_id = db.scalar(select(User.id).order_by(User.id).limit(1))
            if actor_id is None:
                raise RuntimeError("project export smoke requires a seeded user")
            project = Project(name="ci-evidence-export-snapshot")
            db.add(project)
            db.flush()
            project_id = project.id
            batch = ImportBatch(project_id=project_id, filename="smoke.xlsx", headers=[],
                                rows=[], warnings=[], status="done")
            db.add(batch)
            db.flush()
            case = Case(project_id=project_id, import_id=batch.id, original_id="TC-001",
                        title="before concurrent commit", original=[])
            db.add(case)
            db.flush()
            batch_id, case_id = batch.id, case.id
            material = b"# postgres evidence snapshot fixture\n"
            document = SourceDocument(id=uuid.uuid4().hex, project_id=project_id, user_id=actor_id,
                                      filename="fixture.md", extracted_text=material.decode(), warnings=[],
                                      source_sha256=hashlib.sha256(material).hexdigest(), source_size=len(material))
            db.add(document)
            document_path = document_root / str(project_id) / f"{document.id}.bin"
            write_private_atomic(document_path, material, root=document_root)

        with tempfile.TemporaryDirectory(prefix="ta-export-pg-") as temp:
            archive_path = Path(temp) / "project-evidence.zip"
            with consistent_read(database) as snapshot:
                assert snapshot.scalar(select(Case.title).where(Case.id == case_id)) == "before concurrent commit"
                with sessions.begin() as writer:
                    writer.get(Case, case_id).title = "after concurrent commit"
                    new_case = Case(project_id=project_id, import_id=batch_id, original_id="TC-002",
                                    title="new case after snapshot", original=[])
                    writer.add(new_case)
                    writer.flush()
                    new_case_id = new_case.id
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    _export(project_id, snapshot, archive)

            result = verify_archive(archive_path)
            assert result["counts"]["case"] == 1
            assert result["counts"]["document"] == 1
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                case_bytes = archive.read(f"case/{case_id}.json")
                assert manifest["project_id"] == project_id
                assert json.loads(case_bytes)["title"] == "before concurrent commit"
                assert f"case/{new_case_id}.json" not in archive.namelist()
                assert archive.read(f"document-files/{document.id}.bin") == material

            remove_private_file(document_path, root=document_root)
            with consistent_read(database) as snapshot:
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    try:
                        _export(project_id, snapshot, archive)
                    except HTTPException as exc:
                        if exc.status_code != 409:
                            raise
                    else:
                        raise AssertionError("missing source file must fail the evidence export")
        with sessions() as db:
            titles = db.scalars(select(Case.title).where(Case.project_id == project_id)
                                .order_by(Case.id)).all()
            assert titles == ["after concurrent commit", "new case after snapshot"]
    finally:
        if project_id is not None:
            with sessions.begin() as db:
                db.execute(delete(SourceDocument).where(SourceDocument.project_id == project_id))
                db.execute(delete(Case).where(Case.project_id == project_id))
                db.execute(delete(ImportBatch).where(ImportBatch.project_id == project_id))
                db.execute(delete(Project).where(Project.id == project_id))
        if document_path is not None:
            remove_private_file(document_path, root=document_root)
            if document_path.parent.is_dir():
                document_path.parent.rmdir()


if __name__ == "__main__":
    verify()
