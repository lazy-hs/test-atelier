"""Track all Harness sessions owned by one bounded task."""

from alembic import op
import sqlalchemy as sa


revision = "20261034_harness_session_ids"
down_revision = "20261033_harness_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("harness_tasks")}
    if "session_ids" not in columns:
        op.add_column(
            "harness_tasks",
            sa.Column("session_ids", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    op.drop_column("harness_tasks", "session_ids")
