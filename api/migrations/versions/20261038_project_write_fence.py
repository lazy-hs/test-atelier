"""Reject project-owned writes after a durable project deletion intent.

The API's short-lived locks protect file writes, while these triggers catch
older workers and routes that committed after their initial access check.
Deletes remain allowed so the project cleanup transaction can remove children.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261038_project_write_fence"
down_revision = "20261037_harness_deletion"
branch_labels = None
depends_on = None


PROJECT_TABLES = (
    "ai_jobs", "analysis_runs", "case_drafts", "case_suggestions", "cases",
    "codex_sessions", "conversion_records", "conversion_settings",
    "execution_defects", "execution_reports", "execution_runs", "governance_change_logs", "governance_directories",
    "governance_duplicate_groups", "governance_export_jobs",
    "governance_metric_snapshots", "governance_validation_runs",
    "harness_tasks", "import_batches", "membership_events", "memberships",
    "project_skills", "smoke_suites", "source_documents", "suggestion_jobs",
    "taxonomy_versions", "work_items",
)

# Tables without project_id inherit ownership through the indicated parent.
# The SQL returns the project ID for one new/updated child row, or NULL when
# its parent has disappeared. Multi-parent rows also require both sides to
# belong to the same project; either mismatch fails closed.
INDIRECT_PROJECT_QUERIES = {
    "execution_defect_events": "SELECT project_id FROM execution_defects WHERE id = NEW.defect_id",
    "codex_messages": "SELECT project_id FROM codex_sessions WHERE id = NEW.session_id",
    "review_events": "SELECT project_id FROM cases WHERE id = NEW.case_id",
    "governance_duplicate_members": """SELECT groups.project_id FROM governance_duplicate_groups AS groups
                                        JOIN cases AS cases ON cases.id = NEW.case_id AND cases.project_id = groups.project_id
                                        WHERE groups.id = NEW.group_id""",
    "case_comments": "SELECT project_id FROM cases WHERE id = NEW.case_id",
    "harness_task_events": "SELECT project_id FROM harness_tasks WHERE id = NEW.task_id",
    "smoke_items": """SELECT suites.project_id FROM smoke_suites AS suites
                       JOIN cases AS cases ON cases.id = NEW.case_id AND cases.project_id = suites.project_id
                       WHERE suites.id = NEW.suite_id""",
    "run_cases": """SELECT runs.project_id FROM execution_runs AS runs
                     JOIN cases AS cases ON cases.id = NEW.case_id AND cases.project_id = runs.project_id
                     WHERE runs.id = NEW.run_id""",
    "execution_events": """SELECT runs.project_id FROM run_cases AS cases
                            JOIN execution_runs AS runs ON runs.id = cases.run_id
                            WHERE cases.id = NEW.run_case_id""",
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if dialect == "sqlite":
        for table in PROJECT_TABLES:
            if table not in existing:
                continue  # Partially initialized legacy development databases.
            for operation in ("INSERT", "UPDATE"):
                op.execute(sa.text(f"""
                    CREATE TRIGGER ta_project_fence_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM projects
                        WHERE id = NEW.project_id AND deletion_pending_at IS NULL
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'project_deletion_pending');
                    END
                """))
        for table, owner_query in INDIRECT_PROJECT_QUERIES.items():
            if table not in existing:
                continue
            for operation in ("INSERT", "UPDATE"):
                op.execute(sa.text(f"""
                    CREATE TRIGGER ta_project_fence_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    WHEN NOT EXISTS (
                        SELECT 1 FROM projects
                        WHERE id = ({owner_query}) AND deletion_pending_at IS NULL
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'project_deletion_pending');
                    END
                """))
    elif dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION ta_project_write_fence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.project_id IS NOT NULL THEN
                    PERFORM 1 FROM projects
                    WHERE id = NEW.project_id AND deletion_pending_at IS NULL
                    FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'project_deletion_pending' USING ERRCODE = '23514';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        for table in PROJECT_TABLES:
            if table not in existing:
                continue
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_project_fence_{table}
                BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_project_write_fence()
            """))
        branches = [f"{'IF' if index == 0 else 'ELSIF'} TG_TABLE_NAME = '{table}' THEN\n"
                    f"    owner_project := ({query});"
                    for index, (table, query) in enumerate(INDIRECT_PROJECT_QUERIES.items())]
        op.execute(sa.text(f"""
            CREATE FUNCTION ta_project_indirect_write_fence() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE owner_project integer;
            BEGIN
                {' '.join(branches)}
                ELSE
                    RAISE EXCEPTION 'unexpected project-owned table';
                END IF;
                IF owner_project IS NULL THEN
                    RAISE EXCEPTION 'project_deletion_pending' USING ERRCODE = '23514';
                END IF;
                PERFORM 1 FROM projects
                WHERE id = owner_project AND deletion_pending_at IS NULL
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'project_deletion_pending' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """))
        for table in INDIRECT_PROJECT_QUERIES:
            if table not in existing:
                continue
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_project_fence_{table}
                BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_project_indirect_write_fence()
            """))
    else:
        raise RuntimeError(f"Unsupported database for project write fencing: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (*PROJECT_TABLES, *INDIRECT_PROJECT_QUERIES):
            for operation in ("insert", "update"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_project_fence_{table}_{operation}"))
    elif dialect == "postgresql":
        existing = set(sa.inspect(op.get_bind()).get_table_names())
        for table in (*PROJECT_TABLES, *INDIRECT_PROJECT_QUERIES):
            if table in existing:
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_project_fence_{table} ON {table}"))
        op.execute(sa.text("DROP FUNCTION ta_project_indirect_write_fence()"))
        op.execute(sa.text("DROP FUNCTION ta_project_write_fence()"))
    else:
        raise RuntimeError(f"Unsupported database for project write fencing: {dialect}")
