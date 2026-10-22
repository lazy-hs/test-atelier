"""Add optional start times to personal work-board items."""

from alembic import op
import sqlalchemy as sa


revision = "20261030_workboard_schedule"
down_revision = "20261029_workboard"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("work_items")}
    if "start_at" not in columns:
        op.add_column("work_items", sa.Column("start_at", sa.String(length=40), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("work_items")}
    if "start_at" in columns:
        op.drop_column("work_items", "start_at")
