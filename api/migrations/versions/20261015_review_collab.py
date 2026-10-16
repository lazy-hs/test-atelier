"""Case assignment, review action types and comments.

Revision ID: 20261015_review_collab
Revises: 20261015_project_roles
"""

import sqlalchemy as sa
from alembic import op

revision = "20261015_review_collab"
down_revision = "20261015_project_roles"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("cases")}
    foreign_keys = sa.inspect(bind).get_foreign_keys("cases")
    has_assignee_fk = any(key["constrained_columns"] == ["assignee_id"] for key in foreign_keys)
    if "assignee_id" not in columns or not has_assignee_fk:
        with op.batch_alter_table("cases") as batch:
            if "assignee_id" not in columns:
                batch.add_column(sa.Column("assignee_id", sa.Integer(), nullable=True))
            if not has_assignee_fk:
                batch.create_foreign_key("fk_cases_assignee_users", "users", ["assignee_id"], ["id"])
    if "ix_cases_assignee_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("cases")}:
        op.create_index("ix_cases_assignee_id", "cases", ["assignee_id"])
    if "action" not in {column["name"] for column in sa.inspect(bind).get_columns("review_events")}:
        op.add_column("review_events", sa.Column("action", sa.String(20), nullable=False, server_default="review"))
    if not sa.inspect(bind).has_table("case_comments"):
        op.create_table(
            "case_comments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_case_comments_case_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("case_comments")}:
        op.create_index("ix_case_comments_case_id", "case_comments", ["case_id"])


def downgrade():
    raise RuntimeError("审核分派和讨论历史不可无损降级，请恢复升级前的数据库备份")
