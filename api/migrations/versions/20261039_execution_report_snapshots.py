"""Persist completed execution reports before source cases can be deleted."""

import hashlib
import json

from alembic import op
import sqlalchemy as sa

from api.execution_report_core import build_execution_report, canonical_json


revision = "20261039_execution_report_snapshots"
down_revision = "20261038_project_write_fence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("execution_runs") and "frozen_case_count" not in {
        column["name"] for column in sa.inspect(bind).get_columns("execution_runs")
    }:
        op.add_column("execution_runs", sa.Column("frozen_case_count", sa.Integer(), nullable=True))
    if not sa.inspect(bind).has_table("execution_reports"):
        op.create_table(
            "execution_reports",
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("execution_runs.id"), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("completeness", sa.String(24), nullable=False),
            sa.Column("payload_sha256", sa.String(64), nullable=False),
            sa.Column("passed_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("blocked_count", sa.Integer(), nullable=False),
            sa.Column("skipped_count", sa.Integer(), nullable=False),
            sa.Column("not_run_count", sa.Integer(), nullable=False),
            sa.Column("case_executions", sa.Integer(), nullable=False),
            sa.Column("estimated_seconds", sa.Integer(), nullable=False),
            sa.Column("actual_seconds", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_execution_reports_project_id" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("execution_reports")
    }:
        op.create_index("ix_execution_reports_project_id", "execution_reports", ["project_id"])

    dialect = bind.dialect.name
    if dialect == "sqlite":
        for operation in ("INSERT", "UPDATE"):
            op.execute(sa.text(f"""
                CREATE TRIGGER IF NOT EXISTS ta_project_fence_execution_reports_{operation.lower()}
                BEFORE {operation} ON execution_reports
                WHEN NEW.project_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM projects WHERE id = NEW.project_id AND deletion_pending_at IS NULL
                )
                BEGIN SELECT RAISE(ABORT, 'project_deletion_pending'); END
            """))
    elif dialect == "postgresql":
        op.execute(sa.text("""
            CREATE TRIGGER ta_project_fence_execution_reports
            BEFORE INSERT OR UPDATE ON execution_reports
            FOR EACH ROW EXECUTE FUNCTION ta_project_write_fence()
        """))
    else:
        raise RuntimeError(f"Unsupported database for execution report migration: {dialect}")

    reports = sa.table("execution_reports",
                       sa.column("run_id", sa.Integer()), sa.column("project_id", sa.Integer()),
                       sa.column("payload", sa.JSON()), sa.column("completeness", sa.String()),
                       sa.column("payload_sha256", sa.String()),
                       *[sa.column(f"{name}_count", sa.Integer()) for name in
                         ("passed", "failed", "blocked", "skipped", "not_run")],
                       sa.column("case_executions", sa.Integer()),
                       sa.column("estimated_seconds", sa.Integer()), sa.column("actual_seconds", sa.Integer()),
                       sa.column("created_at", sa.String()))
    if not {"execution_runs", "run_cases"} <= set(sa.inspect(bind).get_table_names()):
        return  # Partially initialized legacy development database.
    runs = bind.execute(sa.text("""
        SELECT runs.* FROM execution_runs AS runs
        JOIN projects AS projects ON projects.id = runs.project_id
        LEFT JOIN execution_reports AS reports ON reports.run_id = runs.id
        WHERE runs.status = 'completed' AND runs.completed_at IS NOT NULL
          AND projects.deletion_pending_at IS NULL AND reports.run_id IS NULL
        ORDER BY runs.id
    """))
    for mapping in runs.mappings():
        run = dict(mapping)
        if isinstance(run["required_chains"], str):
            run["required_chains"] = json.loads(run["required_chains"])
        rows = list(bind.execute(sa.text("SELECT * FROM run_cases WHERE run_id = :run_id ORDER BY id"),
                                 {"run_id": run["id"]}).mappings())
        try:
            report = build_execution_report(run, rows)
        except ValueError:
            # Deleted or corrupt legacy cases cannot be reconstructed honestly.
            continue
        # Older runs did not record their initial case count. A surviving row
        # set cannot prove that no case was removed before this migration.
        report["source"]["historical_completeness"] = "unverified"
        report["integrity"]["payload_sha256"] = hashlib.sha256(canonical_json(
            {key: value for key, value in report.items() if key != "integrity"})).hexdigest()
        counts = report["summary"]["results"]
        bind.execute(sa.insert(reports), {"run_id": run["id"], "project_id": run["project_id"],
                                         "completeness": "legacy_unverified",
                                         "payload": report, "payload_sha256": report["integrity"]["payload_sha256"],
                                         **{f"{key}_count": counts[key] for key in counts},
                                         "case_executions": report["summary"]["case_count"],
                                         "estimated_seconds": report["summary"]["estimated_seconds"],
                                         "actual_seconds": report["summary"]["actual_seconds"],
                                         "created_at": run["completed_at"]})


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("insert", "update"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_project_fence_execution_reports_{operation}"))
    elif op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS ta_project_fence_execution_reports ON execution_reports"))
    op.drop_index("ix_execution_reports_project_id", table_name="execution_reports")
    op.drop_table("execution_reports")
    if sa.inspect(op.get_bind()).has_table("execution_runs"):
        op.drop_column("execution_runs", "frozen_case_count")
