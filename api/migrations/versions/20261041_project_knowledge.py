"""Project knowledge with immutable versions and guarded ownership."""

from alembic import op
import sqlalchemy as sa


revision = "20261041_project_knowledge"
down_revision = "20261040_execution_defects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("knowledge_entries"):
        op.create_table(
            "knowledge_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("source_snapshot", sa.JSON(), nullable=False),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
    if "ix_knowledge_entries_project_id" not in {item["name"] for item in inspector.get_indexes("knowledge_entries")}:
        op.create_index("ix_knowledge_entries_project_id", "knowledge_entries", ["project_id"])
    if not inspector.has_table("knowledge_revisions"):
        op.create_table(
            "knowledge_revisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("entry_id", sa.Integer(), sa.ForeignKey("knowledge_entries.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.UniqueConstraint("entry_id", "version", name="uq_knowledge_entry_version"),
        )
    if "ix_knowledge_revisions_entry_id" not in {item["name"] for item in inspector.get_indexes("knowledge_revisions")}:
        op.create_index("ix_knowledge_revisions_entry_id", "knowledge_revisions", ["entry_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS ta_knowledge_source_immutable
            BEFORE UPDATE ON knowledge_entries
            WHEN NEW.project_id != OLD.project_id OR NEW.source_type != OLD.source_type
              OR NEW.source_id != OLD.source_id OR NEW.source_snapshot != OLD.source_snapshot
            BEGIN SELECT RAISE(ABORT, 'knowledge_source_immutable'); END
        """))
        op.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS ta_knowledge_revision_immutable
            BEFORE UPDATE ON knowledge_revisions
            BEGIN SELECT RAISE(ABORT, 'knowledge_revision_immutable'); END
        """))
        for operation in ("INSERT", "UPDATE"):
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_project_fence_knowledge_entries_{operation.lower()}
                BEFORE {operation} ON knowledge_entries
                WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id AND deletion_pending_at IS NULL)
                BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_project_fence_knowledge_revisions_{operation.lower()}
                BEFORE {operation} ON knowledge_revisions
                WHEN NOT EXISTS (
                    SELECT 1 FROM knowledge_entries AS entries JOIN projects AS projects
                      ON projects.id = entries.project_id AND projects.deletion_pending_at IS NULL
                    WHERE entries.id = NEW.entry_id
                )
                BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
            """))
    elif dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION ta_knowledge_immutability() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_TABLE_NAME = 'knowledge_revisions' THEN
                    RAISE EXCEPTION 'knowledge_revision_immutable' USING ERRCODE = '23514';
                END IF;
                IF NEW.project_id IS DISTINCT FROM OLD.project_id
                   OR NEW.source_type IS DISTINCT FROM OLD.source_type
                   OR NEW.source_id IS DISTINCT FROM OLD.source_id
                   OR NEW.source_snapshot::text IS DISTINCT FROM OLD.source_snapshot::text THEN
                    RAISE EXCEPTION 'knowledge_source_immutable' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_knowledge_source_immutable BEFORE UPDATE ON knowledge_entries
            FOR EACH ROW EXECUTE FUNCTION ta_knowledge_immutability()
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_knowledge_revision_immutable BEFORE UPDATE ON knowledge_revisions
            FOR EACH ROW EXECUTE FUNCTION ta_knowledge_immutability()
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_project_fence_knowledge_entries
            BEFORE INSERT OR UPDATE ON knowledge_entries
            FOR EACH ROW EXECUTE FUNCTION ta_project_write_fence()
        """))
        op.execute(sa.text("""
            CREATE FUNCTION ta_knowledge_revision_fence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE owner_project integer;
            BEGIN
                SELECT entries.project_id INTO owner_project FROM knowledge_entries AS entries
                  WHERE entries.id = NEW.entry_id;
                IF owner_project IS NULL THEN
                    RAISE EXCEPTION 'project_deletion_pending' USING ERRCODE = '23514';
                END IF;
                PERFORM 1 FROM projects WHERE id = owner_project AND deletion_pending_at IS NULL FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'project_deletion_pending' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_project_fence_knowledge_revisions
            BEFORE INSERT OR UPDATE ON knowledge_revisions
            FOR EACH ROW EXECUTE FUNCTION ta_knowledge_revision_fence()
        """))
    else:
        raise RuntimeError(f"Unsupported database for project knowledge: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER ta_knowledge_revision_immutable"))
        op.execute(sa.text("DROP TRIGGER ta_knowledge_source_immutable"))
        for table in ("knowledge_entries", "knowledge_revisions"):
            for operation in ("insert", "update"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_project_fence_{table}_{operation}"))
    elif dialect == "postgresql":
        op.execute(sa.text("DROP TRIGGER ta_knowledge_revision_immutable ON knowledge_revisions"))
        op.execute(sa.text("DROP TRIGGER ta_knowledge_source_immutable ON knowledge_entries"))
        op.execute(sa.text("DROP FUNCTION ta_knowledge_immutability()"))
        op.execute(sa.text("DROP TRIGGER ta_project_fence_knowledge_revisions ON knowledge_revisions"))
        op.execute(sa.text("DROP FUNCTION ta_knowledge_revision_fence()"))
        op.execute(sa.text("DROP TRIGGER ta_project_fence_knowledge_entries ON knowledge_entries"))
    op.drop_index("ix_knowledge_revisions_entry_id", table_name="knowledge_revisions")
    op.drop_table("knowledge_revisions")
    op.drop_index("ix_knowledge_entries_project_id", table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
