"""Persist embedded Codex sessions and transcripts."""

import sqlalchemy as sa
from alembic import op


revision = "20261028_codex_sessions"
down_revision = "20261027_model_tool_schema"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("codex_sessions"):
        op.create_table(
            "codex_sessions",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False, server_default="新建 Codex 会话"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ready"),
            sa.Column("model", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("working_directory", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("created_at", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("updated_at", sa.String(length=40), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_codex_sessions_project_id", "codex_sessions", ["project_id"])
        op.create_index("ix_codex_sessions_user_id", "codex_sessions", ["user_id"])
    if not inspector.has_table("codex_messages"):
        op.create_table(
            "codex_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(length=40), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(["session_id"], ["codex_sessions.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_codex_messages_session_id", "codex_messages", ["session_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("codex_messages"):
        op.drop_index("ix_codex_messages_session_id", table_name="codex_messages")
        op.drop_table("codex_messages")
    if inspector.has_table("codex_sessions"):
        op.drop_index("ix_codex_sessions_user_id", table_name="codex_sessions")
        op.drop_index("ix_codex_sessions_project_id", table_name="codex_sessions")
        op.drop_table("codex_sessions")
