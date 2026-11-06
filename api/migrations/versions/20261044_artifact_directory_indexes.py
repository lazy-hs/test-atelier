"""Index project-scoped directory edges without indexing unbounded path bytes."""

from alembic import op
import sqlalchemy as sa


revision = "20261044_artifact_directory_indexes"
down_revision = "20261043_document_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Unsupported database for artifact directory indexes: {dialect}")
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    # Prefix expressions bound PostgreSQL index entry size for long UTF-8 paths.
    if "cases" in tables:
        op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_cases_project_directory_prefix "
                           "ON cases (project_id, substr(directory, 1, 128))"))
    if "governance_change_logs" in tables:
        op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_governance_change_project_entity_path "
                           "ON governance_change_logs (project_id, entity, entity_id)"))


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "governance_change_logs" in tables:
        op.drop_index("ix_governance_change_project_entity_path", table_name="governance_change_logs")
    if "cases" in tables:
        op.drop_index("ix_cases_project_directory_prefix", table_name="cases")
