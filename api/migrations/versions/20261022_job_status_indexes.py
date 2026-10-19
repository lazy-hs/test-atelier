"""Index durable job status scans used by the external worker."""

import sqlalchemy as sa
from alembic import op


revision = "20261022_job_status_indexes"
down_revision = "20261021_job_request_correlation"
branch_labels = None
depends_on = None


def _create_if_missing(table: str, name: str, columns: list[str]) -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def upgrade():
    _create_if_missing("ai_jobs", "ix_ai_jobs_status", ["status"])
    _create_if_missing("suggestion_jobs", "ix_suggestion_jobs_status", ["status"])


def downgrade():
    raise RuntimeError("Job status indexes are additive; restore a database backup instead")
