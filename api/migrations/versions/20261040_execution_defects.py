"""Human-authored defects referencing frozen execution reports, not live cases."""

from alembic import op
import sqlalchemy as sa


revision = "20261040_execution_defects"
down_revision = "20261039_execution_report_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("execution_defects"):
        op.create_table(
            "execution_defects",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("execution_reports.run_id"), nullable=False),
            sa.Column("run_case_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("resolution", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
    if "ix_execution_defects_project_id" not in {item["name"] for item in inspector.get_indexes("execution_defects")}:
        op.create_index("ix_execution_defects_project_id", "execution_defects", ["project_id"])
    if "ix_execution_defects_run_id" not in {item["name"] for item in inspector.get_indexes("execution_defects")}:
        op.create_index("ix_execution_defects_run_id", "execution_defects", ["run_id"])
    if not inspector.has_table("execution_defect_events"):
        op.create_table(
            "execution_defect_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("defect_id", sa.Integer(), sa.ForeignKey("execution_defects.id"), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("before", sa.JSON(), nullable=False),
            sa.Column("after", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_execution_defect_events_defect_id" not in {item["name"] for item in inspector.get_indexes("execution_defect_events")}:
        op.create_index("ix_execution_defect_events_defect_id", "execution_defect_events", ["defect_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for operation in ("INSERT", "UPDATE"):
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_project_fence_execution_defects_{operation.lower()}
                BEFORE {operation} ON execution_defects
                WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id AND deletion_pending_at IS NULL)
                     OR NOT EXISTS (SELECT 1 FROM execution_reports
                                    WHERE run_id = NEW.run_id AND project_id = NEW.project_id)
                BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_project_fence_execution_defect_events_{operation.lower()}
                BEFORE {operation} ON execution_defect_events
                WHEN NOT EXISTS (
                    SELECT 1 FROM execution_defects AS defects JOIN projects AS projects
                      ON projects.id = defects.project_id AND projects.deletion_pending_at IS NULL
                    WHERE defects.id = NEW.defect_id
                )
                BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
            """))
    elif dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION ta_execution_defect_source_fence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM 1 FROM execution_reports
                 WHERE run_id = NEW.run_id AND project_id = NEW.project_id FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'defect_report_project_mismatch' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_project_fence_execution_defects
            BEFORE INSERT OR UPDATE ON execution_defects
            FOR EACH ROW EXECUTE FUNCTION ta_project_write_fence()
        """))
        op.execute(sa.text("""
            CREATE TRIGGER ta_execution_defect_source_guard
            BEFORE INSERT OR UPDATE ON execution_defects
            FOR EACH ROW EXECUTE FUNCTION ta_execution_defect_source_fence()
        """))
        op.execute(sa.text("""
            CREATE FUNCTION ta_execution_defect_event_fence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE owner_project integer;
            BEGIN
                SELECT defects.project_id INTO owner_project FROM execution_defects AS defects
                  WHERE defects.id = NEW.defect_id;
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
            CREATE TRIGGER ta_project_fence_execution_defect_events
            BEFORE INSERT OR UPDATE ON execution_defect_events
            FOR EACH ROW EXECUTE FUNCTION ta_execution_defect_event_fence()
        """))
    else:
        raise RuntimeError(f"Unsupported database for execution defect migration: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("execution_defects", "execution_defect_events"):
            for operation in ("insert", "update"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_project_fence_{table}_{operation}"))
    elif dialect == "postgresql":
        op.execute(sa.text("DROP TRIGGER ta_project_fence_execution_defect_events ON execution_defect_events"))
        op.execute(sa.text("DROP FUNCTION ta_execution_defect_event_fence()"))
        op.execute(sa.text("DROP TRIGGER ta_project_fence_execution_defects ON execution_defects"))
        op.execute(sa.text("DROP TRIGGER ta_execution_defect_source_guard ON execution_defects"))
        op.execute(sa.text("DROP FUNCTION ta_execution_defect_source_fence()"))
    op.drop_index("ix_execution_defect_events_defect_id", table_name="execution_defect_events")
    op.drop_table("execution_defect_events")
    op.drop_index("ix_execution_defects_run_id", table_name="execution_defects")
    op.drop_index("ix_execution_defects_project_id", table_name="execution_defects")
    op.drop_table("execution_defects")
