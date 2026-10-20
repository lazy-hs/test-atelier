"""Add lifecycle metadata used by project management endpoints."""

import sqlalchemy as sa
from alembic import op


revision = "20261026_project_archiving"
down_revision = "20261024_queue_capacity"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("projects")}
    if "archived_at" not in columns:
        op.add_column("projects", sa.Column("archived_at", sa.String(length=40), nullable=True))


def downgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("projects")}
    if "archived_at" in columns:
        op.drop_column("projects", "archived_at")
