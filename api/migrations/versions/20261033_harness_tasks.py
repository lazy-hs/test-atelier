"""Add the Test Atelier projection for DeepSeek Harness tasks."""

from alembic import op
import sqlalchemy as sa


revision = "20261033_harness_tasks"
down_revision = "20261032_case_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some development databases were initialized by SQLAlchemy metadata
    # before the Alembic revisions were introduced.  Treat those tables as an
    # already-created baseline so upgrading them remains non-destructive.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("harness_tasks"):
        op.create_table(
            "harness_tasks",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_type", sa.String(length=40), nullable=False),
            sa.Column("task_version", sa.String(length=16), nullable=False, server_default="v1"),
            sa.Column("result_schema", sa.String(length=64), nullable=False),
            sa.Column("skill_id", sa.String(length=80), nullable=False),
            sa.Column("source_document_ids", sa.JSON(), nullable=False),
            sa.Column("analysis_run_id", sa.String(length=36), sa.ForeignKey("analysis_runs.id"), nullable=True),
            sa.Column("session_id", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(length=160), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("error", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("updated_at", sa.String(length=40), nullable=False),
            sa.UniqueConstraint("project_id", "user_id", "idempotency_key", name="uq_harness_task_idempotency"),
        )
    if not inspector.has_table("harness_task_events"):
        op.create_table(
            "harness_task_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.String(length=64), sa.ForeignKey("harness_tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.UniqueConstraint("task_id", "sequence", name="uq_harness_task_event_sequence"),
        )
    # Older metadata-created tables can exist without the ORM indexes. Add
    # only missing indexes so this adoption path remains idempotent.
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("harness_tasks")}
    for name, column in (
        ("ix_harness_tasks_project_id", "project_id"),
        ("ix_harness_tasks_user_id", "user_id"),
        ("ix_harness_tasks_analysis_run_id", "analysis_run_id"),
        ("ix_harness_tasks_session_id", "session_id"),
        ("ix_harness_tasks_status", "status"),
    ):
        if name not in indexes:
            op.create_index(name, "harness_tasks", [column])
    event_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("harness_task_events")}
    if "ix_harness_task_events_task_id" not in event_indexes:
        op.create_index("ix_harness_task_events_task_id", "harness_task_events", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_harness_task_events_task_id", table_name="harness_task_events")
    op.drop_table("harness_task_events")
    for index in (
        "ix_harness_tasks_status",
        "ix_harness_tasks_session_id",
        "ix_harness_tasks_analysis_run_id",
        "ix_harness_tasks_user_id",
        "ix_harness_tasks_project_id",
    ):
        op.drop_index(index, table_name="harness_tasks")
    op.drop_table("harness_tasks")
