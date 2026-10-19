"""Add expiring leases for durable external-worker jobs."""

import sqlalchemy as sa
from alembic import op


revision = "20261023_job_leases"
down_revision = "20261022_job_status_indexes"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    existing = {item["name"] for item in sa.inspect(bind).get_columns(table)}
    if name not in existing:
        op.add_column(table, column)


def _create_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    """Create the lease scan index without hiding unrelated migration errors."""
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def upgrade():
    for table in ("ai_jobs", "suggestion_jobs"):
        _add_if_missing(table, "lease_owner", sa.Column("lease_owner", sa.String(128), nullable=False, server_default=""))
        _add_if_missing(table, "lease_until", sa.Column("lease_until", sa.Float(), nullable=False, server_default="0"))
        _add_if_missing(table, "lease_heartbeat", sa.Column("lease_heartbeat", sa.Float(), nullable=False, server_default="0"))
        # The status index remains the primary queue scan index. This
        # additional index keeps expired-lease recovery bounded on both
        # PostgreSQL and SQLite without hiding genuine migration failures.
        _create_index_if_missing(table, f"ix_{table}_lease_until", ["lease_until"])


def downgrade():
    raise RuntimeError("Job leases are additive; restore a database backup instead")
