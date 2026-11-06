"""Record immutable upload identity for new source documents.

Legacy rows stay unverified: migration-time files may be missing or changed,
so deriving a digest now would falsely claim it was captured at upload.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261045_source_document_integrity"
down_revision = "20261044_artifact_directory_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database for source document integrity")
    if not sa.inspect(bind).has_table("source_documents"):
        return
    existing = {item["name"] for item in sa.inspect(bind).get_columns("source_documents")}
    if "source_sha256" not in existing:
        op.add_column("source_documents", sa.Column("source_sha256", sa.String(64), nullable=True))
    if "source_size" not in existing:
        op.add_column("source_documents", sa.Column("source_size", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("source_documents"):
        return
    existing = {item["name"] for item in sa.inspect(bind).get_columns("source_documents")}
    if "source_size" in existing:
        op.drop_column("source_documents", "source_size")
    if "source_sha256" in existing:
        op.drop_column("source_documents", "source_sha256")
