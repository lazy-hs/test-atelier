"""Add leases and remote cursors for the independent Harness projector."""

from alembic import op
import sqlalchemy as sa


revision = "20261035_harness_projector_leases"
down_revision = "20261034_harness_session_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("harness_tasks")}
    for name, column in (
        ("projector_owner", sa.Column("projector_owner", sa.String(length=128), nullable=False, server_default="")),
        ("projector_lease_until", sa.Column("projector_lease_until", sa.Float(), nullable=False, server_default="0")),
        ("projector_lease_heartbeat", sa.Column("projector_lease_heartbeat", sa.Float(), nullable=False, server_default="0")),
        ("projector_attempts", sa.Column("projector_attempts", sa.Integer(), nullable=False, server_default="0")),
        ("projector_error", sa.Column("projector_error", sa.Text(), nullable=False, server_default="")),
    ):
        if name not in columns:
            op.add_column("harness_tasks", column)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_tasks")}
    for name, column in (
        ("ix_harness_tasks_projector_owner", "projector_owner"),
        ("ix_harness_tasks_projector_lease_until", "projector_lease_until"),
    ):
        if name not in indexes:
            op.create_index(name, "harness_tasks", [column])
    event_columns = {column["name"] for column in sa.inspect(bind).get_columns("harness_task_events")}
    if "remote_sequence" not in event_columns:
        op.add_column("harness_task_events", sa.Column("remote_sequence", sa.Integer(), nullable=True))
    # SQLite cannot ALTER TABLE to add a constraint; a unique index has the
    # same idempotency guarantee and works on SQLite and PostgreSQL.
    event_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("harness_task_events")}
    if "uq_harness_task_event_remote_sequence" not in event_indexes:
        op.create_index("uq_harness_task_event_remote_sequence", "harness_task_events",
                        ["task_id", "remote_sequence"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_harness_task_event_remote_sequence", table_name="harness_task_events")
    op.drop_column("harness_task_events", "remote_sequence")
    op.drop_index("ix_harness_tasks_projector_lease_until", table_name="harness_tasks")
    op.drop_index("ix_harness_tasks_projector_owner", table_name="harness_tasks")
    for column in (
        "projector_error",
        "projector_attempts",
        "projector_lease_heartbeat",
        "projector_lease_until",
        "projector_owner",
    ):
        op.drop_column("harness_tasks", column)
