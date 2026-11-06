"""Persist project deletion intent before cleaning the independent Harness Host."""

from alembic import op
import sqlalchemy as sa


revision = "20261037_harness_deletion"
down_revision = "20261036_harness_retry_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")}
    if "deletion_pending_at" not in columns:
        op.add_column("projects", sa.Column("deletion_pending_at", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "deletion_pending_at")
