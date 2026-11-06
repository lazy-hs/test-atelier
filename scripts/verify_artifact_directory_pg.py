"""Exercise artifact-directory index expressions on a migrated PostgreSQL database."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from api.db import engine


QUERIES = {
    "ix_cases_project_directory_prefix": """SELECT id FROM cases WHERE project_id = 1
        AND substr(directory, 1, 128) = 'synthetic' AND directory = 'synthetic'""",
    "ix_governance_change_project_entity_path": """SELECT id FROM governance_change_logs
        WHERE project_id = 1 AND entity = 'directory' AND entity_id = 'synthetic'""",
}


def verify() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("artifact-directory index smoke requires PostgreSQL")
    with Session(engine) as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        if head != "20261047_auth_session_revocations":
            raise RuntimeError(f"unexpected migration head: {head}")
        # The plan check verifies usable SQL/index expressions, not production selectivity.
        db.execute(text("SET LOCAL enable_seqscan = off"))
        for name, query in QUERIES.items():
            plan = "\n".join(str(row[0]) for row in db.execute(text("EXPLAIN " + query)))
            if name not in plan:
                raise AssertionError(f"PostgreSQL artifact index is not usable: {name}: {plan}")
        db.rollback()


if __name__ == "__main__":
    verify()
