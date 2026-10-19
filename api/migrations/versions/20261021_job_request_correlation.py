"""Persist the HTTP request id that enqueued asynchronous jobs."""

import sqlalchemy as sa
from alembic import op


revision = "20261021_job_request_correlation"
down_revision = "20261020_ai_prompt_capture"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, name: str) -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    if name not in existing:
        op.add_column(table, sa.Column(name, sa.String(128), nullable=False, server_default=""))
        # New ORM-created rows use the Python default.  Remove the database
        # default where the backend supports ALTER COLUMN; SQLite may retain it.
        if bind.dialect.name != "sqlite":
            op.alter_column(table, name, server_default=None)


def upgrade():
    _add_if_missing("ai_jobs", "request_id")
    _add_if_missing("suggestion_jobs", "request_id")


def downgrade():
    raise RuntimeError("Job request correlation is additive; restore a database backup instead")
