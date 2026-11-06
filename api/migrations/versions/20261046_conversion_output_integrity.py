"""Capture creation-time identity for new conversion outputs only.

Historic outputs retain a NULL digest rather than claiming the current file
matches bytes generated before this migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261046_conversion_output_integrity"
down_revision = "20261045_source_document_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database for conversion output integrity")
    if not sa.inspect(bind).has_table("conversion_records"):
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns("conversion_records")}
    if "output_sha256" not in existing:
        op.add_column("conversion_records", sa.Column("output_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("conversion_records"):
        return
    existing = {column["name"] for column in sa.inspect(bind).get_columns("conversion_records")}
    if "output_sha256" in existing:
        op.drop_column("conversion_records", "output_sha256")
