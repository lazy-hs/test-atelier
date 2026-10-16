"""Project-scoped roles and membership audit trail.

Revision ID: 20261015_project_roles
Revises: 20261015_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "20261015_project_roles"
down_revision = "20261015_initial"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "role" not in {column["name"] for column in inspector.get_columns("memberships")}:
        op.add_column("memberships", sa.Column("role", sa.String(20), nullable=False, server_default="reviewer"))
    op.execute(sa.text("""UPDATE memberships SET role = CASE
        (SELECT users.role FROM users WHERE users.id = memberships.user_id)
        WHEN 'admin' THEN 'manager'
        WHEN 'reader' THEN 'reader'
        ELSE 'reviewer' END WHERE role = 'reviewer'"""))
    if not inspector.has_table("membership_events"):
        op.create_table(
            "membership_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("before_role", sa.String(20)),
            sa.Column("after_role", sa.String(20)),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_membership_events_project_id" not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("membership_events")}:
        op.create_index("ix_membership_events_project_id", "membership_events", ["project_id"])


def downgrade():
    raise RuntimeError("项目角色历史不可无损降级，请恢复升级前的数据库备份")
