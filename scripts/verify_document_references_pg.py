"""Exercise the migrated document-reference triggers on a real PostgreSQL DB."""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.db import AnalysisRun, HarnessTask, Project, User, engine


def verify() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("document-reference PostgreSQL smoke requires PostgreSQL")

    with Session(engine) as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        if head != "20261047_auth_session_revocations":
            raise RuntimeError(f"unexpected migration head: {head}")
        user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
        if user_id is None:
            raise RuntimeError("no user available for scoped document-reference smoke")

        project = Project(name="ci-document-reference-smoke")
        db.add(project)
        db.flush()
        analysis = AnalysisRun(id="ci-document-reference-analysis", project_id=project.id,
                               user_id=user_id, source_ids=["ci-document-a", "ci-document-a", 42], result={})
        task = HarnessTask(id="ci-document-reference-task", project_id=project.id, user_id=user_id,
                           task_type="requirement-analysis", result_schema="requirement-analysis.v1",
                           skill_id="requirement-analysis", source_document_ids=["ci-document-a"],
                           idempotency_key="ci-document-reference-task", result={}, error={})
        db.add_all([analysis, task])
        db.flush()

        def references():
            return set(db.execute(text("SELECT document_id, source_kind, source_id FROM document_references "
                                       "WHERE source_id IN (:analysis, :task)"),
                                  {"analysis": analysis.id, "task": task.id}).all())

        expected = {("ci-document-a", "analysis", analysis.id),
                    ("ci-document-a", "harness_task", task.id)}
        if references() != expected:
            raise AssertionError(f"insert triggers did not deduplicate/filter sources: {references()}")
        analysis.source_ids = ["ci-document-b", 42]
        task.source_document_ids = ["ci-document-b", "ci-document-b"]
        db.flush()
        expected = {("ci-document-b", "analysis", analysis.id),
                    ("ci-document-b", "harness_task", task.id)}
        if references() != expected:
            raise AssertionError(f"update triggers did not replace sources: {references()}")
        db.delete(task)
        db.delete(analysis)
        db.flush()
        if references():
            raise AssertionError(f"delete triggers left orphaned references: {references()}")
        db.rollback()
    print("document-reference PostgreSQL insert/update/delete smoke passed")


if __name__ == "__main__":
    verify()
