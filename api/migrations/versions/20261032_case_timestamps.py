"""Add creation and last-modification timestamps to cases."""
from alembic import op
import sqlalchemy as sa

revision = "20261032_case_timestamps"
down_revision = "20261031_case_governance"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("cases")}
    if "created_at" not in columns:
        op.add_column("cases", sa.Column("created_at", sa.String(40), nullable=False, server_default=""))
    if "updated_at" not in columns:
        op.add_column("cases", sa.Column("updated_at", sa.String(40), nullable=False, server_default=""))
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE cases SET
          created_at = COALESCE(NULLIF(created_at, ''), (SELECT created_at FROM import_batches WHERE import_batches.id = cases.import_id), CURRENT_TIMESTAMP),
          updated_at = COALESCE(NULLIF(updated_at, ''), (SELECT created_at FROM import_batches WHERE import_batches.id = cases.import_id), CURRENT_TIMESTAMP)
    """))


def downgrade():
    op.drop_column("cases", "updated_at")
    op.drop_column("cases", "created_at")
