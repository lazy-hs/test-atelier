"""Link Harness retry Attempts to their previous task."""

from alembic import op
import sqlalchemy as sa


revision = "20261036_harness_retry_attempts"
down_revision = "20261035_harness_projector_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("harness_tasks")}
    if "retry_of" not in columns:
        # Batch mode keeps the migration usable on SQLite as well as
        # PostgreSQL and preserves existing task rows.
        with op.batch_alter_table("harness_tasks", recreate="always") as batch:
            batch.add_column(sa.Column("retry_of", sa.String(length=64), nullable=True))
            batch.create_foreign_key(
                "fk_harness_tasks_retry_of",
                "harness_tasks",
                ["retry_of"],
                ["id"],
                ondelete="SET NULL",
            )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("harness_tasks")}
    if "ix_harness_tasks_retry_of" not in indexes:
        op.create_index("ix_harness_tasks_retry_of", "harness_tasks", ["retry_of"])


def downgrade() -> None:
    op.drop_index("ix_harness_tasks_retry_of", table_name="harness_tasks")
    with op.batch_alter_table("harness_tasks", recreate="always") as batch:
        batch.drop_constraint("fk_harness_tasks_retry_of", type_="foreignkey")
        batch.drop_column("retry_of")
