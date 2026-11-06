"""Frozen project configuration templates and creation provenance."""

from alembic import op
import sqlalchemy as sa


revision = "20261042_project_templates"
down_revision = "20261041_project_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("project_templates"):
        op.create_table(
            "project_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("snapshot_sha256", sa.String(64), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_project_templates_project_id" not in {item["name"] for item in inspector.get_indexes("project_templates")}:
        op.create_index("ix_project_templates_project_id", "project_templates", ["project_id"])
    if not inspector.has_table("project_template_applications"):
        op.create_table(
            "project_template_applications",
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), primary_key=True),
            sa.Column("source_project_id", sa.Integer(), nullable=False),
            sa.Column("template_id", sa.Integer(), nullable=False),
            sa.Column("template_name", sa.String(120), nullable=False),
            sa.Column("snapshot_sha256", sa.String(64), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("project_templates", "project_template_applications"):
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_{table}_immutable BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'project_template_immutable'); END
            """))
            for operation in ("INSERT", "UPDATE"):
                op.execute(sa.text(f"""
                    CREATE TRIGGER IF NOT EXISTS ta_project_fence_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id AND deletion_pending_at IS NULL)
                    BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
                """))
    elif dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION ta_project_template_immutable() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'project_template_immutable' USING ERRCODE = '23514'; END;
            $$
        """))
        for table in ("project_templates", "project_template_applications"):
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_{table}_immutable BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_project_template_immutable()
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_project_fence_{table} BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_project_write_fence()
            """))
    else:
        raise RuntimeError(f"Unsupported database for project templates: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("project_templates", "project_template_applications"):
            op.execute(sa.text(f"DROP TRIGGER ta_{table}_immutable"))
            for operation in ("insert", "update"):
                op.execute(sa.text(f"DROP TRIGGER ta_project_fence_{table}_{operation}"))
    elif dialect == "postgresql":
        for table in ("project_templates", "project_template_applications"):
            op.execute(sa.text(f"DROP TRIGGER ta_{table}_immutable ON {table}"))
            op.execute(sa.text(f"DROP TRIGGER ta_project_fence_{table} ON {table}"))
        op.execute(sa.text("DROP FUNCTION ta_project_template_immutable()"))
    op.drop_table("project_template_applications")
    op.drop_index("ix_project_templates_project_id", table_name="project_templates")
    op.drop_table("project_templates")
