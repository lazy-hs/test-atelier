"""Add user-owned personal work-board items."""

import sqlalchemy as sa
from alembic import op


revision = "20261029_workboard"
down_revision = "20261028_codex_sessions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("work_items"):
        return
    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="todo"),
        sa.Column("due_at", sa.String(length=40), nullable=True),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(length=40), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_work_items_user_id", "work_items", ["user_id"])
    op.create_index("ix_work_items_project_id", "work_items", ["project_id"])
    op.create_index("ix_work_items_status", "work_items", ["status"])


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("work_items"):
        return
    op.drop_index("ix_work_items_status", table_name="work_items")
    op.drop_index("ix_work_items_project_id", table_name="work_items")
    op.drop_index("ix_work_items_user_id", table_name="work_items")
    op.drop_table("work_items")
