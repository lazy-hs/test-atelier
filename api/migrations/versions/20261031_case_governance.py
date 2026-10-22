"""Add project-scoped case governance metadata and audit tables."""
from alembic import op
import sqlalchemy as sa

revision = "20261031_case_governance"
down_revision = "20261030_workboard_schedule"
branch_labels = None
depends_on = None


def _add_columns(table, columns):
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_columns(table)}
    for name, column in columns:
        if name not in existing:
            op.add_column(table, column)


def upgrade():
    _add_columns("import_batches", [
        ("kind", sa.Column("kind", sa.String(24), nullable=False, server_default="workspace")),
        ("sha256", sa.Column("sha256", sa.String(64), nullable=True)),
        ("source_sheet", sa.Column("source_sheet", sa.String(120), nullable=True)),
        ("snapshot_path", sa.Column("snapshot_path", sa.String(1024), nullable=True)),
        ("parent_batch_id", sa.Column("parent_batch_id", sa.Integer(), nullable=True)),
        ("rules_hash", sa.Column("rules_hash", sa.String(64), nullable=True)),
        ("row_count", sa.Column("row_count", sa.Integer(), nullable=True)),
    ])
    _add_columns("cases", [
        ("platform_id", sa.Column("platform_id", sa.String(255), nullable=True)),
        ("name_key", sa.Column("name_key", sa.String(512), nullable=True)),
        ("governance_status", sa.Column("governance_status", sa.String(24), nullable=False, server_default="active")),
        ("domain", sa.Column("domain", sa.String(160), nullable=True)),
        ("subdomain", sa.Column("subdomain", sa.String(240), nullable=True)),
        ("endpoint", sa.Column("endpoint", sa.String(40), nullable=True)),
        ("steps", sa.Column("steps", sa.Text(), nullable=True)),
        ("precondition", sa.Column("precondition", sa.Text(), nullable=True)),
        ("expected", sa.Column("expected", sa.Text(), nullable=True)),
        ("remark", sa.Column("remark", sa.Text(), nullable=True)),
        ("tag_iteration", sa.Column("tag_iteration", sa.String(40), nullable=True)),
        ("tag_requirement", sa.Column("tag_requirement", sa.String(160), nullable=True)),
        ("tag_change", sa.Column("tag_change", sa.String(40), nullable=True)),
        ("fingerprint", sa.Column("fingerprint", sa.String(32), nullable=True)),
        ("content_hash", sa.Column("content_hash", sa.String(64), nullable=True)),
        ("merged_into", sa.Column("merged_into", sa.Integer(), nullable=True)),
    ])
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "governance_duplicate_groups" not in tables:
        op.create_table("governance_duplicate_groups",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), nullable=False), sa.Column("gid", sa.String(32), nullable=False),
            sa.Column("name_key", sa.String(512), nullable=False), sa.Column("name", sa.Text(), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"), sa.Column("cluster_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("category", sa.String(8), nullable=False, server_default="C"), sa.Column("suggested_discard_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending"), sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("decided_by", sa.Integer(), nullable=True), sa.Column("decided_at", sa.String(40), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="CASCADE"))
        op.create_index("ix_governance_duplicate_groups_project_id", "governance_duplicate_groups", ["project_id"])
        op.create_index("ix_governance_duplicate_groups_batch_id", "governance_duplicate_groups", ["batch_id"])
        op.create_index("ix_governance_duplicate_groups_name_key", "governance_duplicate_groups", ["name_key"])
        op.create_index("ix_governance_duplicate_groups_status", "governance_duplicate_groups", ["status"])
    if "governance_duplicate_members" not in tables:
        op.create_table("governance_duplicate_members",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.Integer(), nullable=False), sa.Column("cluster_fp", sa.String(32), nullable=False),
            sa.Column("score", sa.Integer(), nullable=False, server_default="0"), sa.Column("decision", sa.String(24), nullable=False, server_default="keep"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(["group_id"], ["governance_duplicate_groups.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"))
        op.create_index("ix_governance_duplicate_members_group_id", "governance_duplicate_members", ["group_id"])
        op.create_index("ix_governance_duplicate_members_case_id", "governance_duplicate_members", ["case_id"])
        op.create_index("ix_governance_duplicate_members_cluster_fp", "governance_duplicate_members", ["cluster_fp"])
    if "governance_validation_runs" not in tables:
        op.create_table("governance_validation_runs",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(255), nullable=False, server_default=""), sa.Column("rows", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False), sa.Column("status", sa.String(24), nullable=False, server_default="blocked"),
            sa.Column("created_by", sa.Integer(), nullable=False), sa.Column("created_at", sa.String(40), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["created_by"], ["users.id"]))
        op.create_index("ix_governance_validation_runs_project_id", "governance_validation_runs", ["project_id"])
        op.create_index("ix_governance_validation_runs_status", "governance_validation_runs", ["status"])
    if "governance_directories" not in tables:
        op.create_table("governance_directories",
            sa.Column("project_id", sa.Integer(), primary_key=True), sa.Column("path", sa.String(1024), primary_key=True),
            sa.Column("parent", sa.String(1024), nullable=False, server_default=""), sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("domain", sa.String(160), nullable=False, server_default=""), sa.Column("subdomain", sa.String(240), nullable=False, server_default=""),
            sa.Column("whitelisted", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"))
    if "governance_change_logs" not in tables:
        op.create_table("governance_change_logs",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("entity", sa.String(64), nullable=False), sa.Column("entity_id", sa.String(128), nullable=False), sa.Column("action", sa.String(32), nullable=False),
            sa.Column("before_json", sa.JSON(), nullable=False), sa.Column("after_json", sa.JSON(), nullable=False), sa.Column("batch_key", sa.String(64), nullable=False),
            sa.Column("operator_id", sa.Integer(), nullable=False), sa.Column("ts", sa.String(40), nullable=False), sa.Column("revert_of", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["operator_id"], ["users.id"]), sa.ForeignKeyConstraint(["revert_of"], ["governance_change_logs.id"]))
        op.create_index("ix_governance_change_logs_project_id", "governance_change_logs", ["project_id"])
        op.create_index("ix_governance_change_logs_batch_key", "governance_change_logs", ["batch_key"])
    if "governance_metric_snapshots" not in tables:
        op.create_table("governance_metric_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), nullable=False), sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(80), nullable=False), sa.Column("value", sa.Float(), nullable=False, server_default="0"), sa.Column("created_at", sa.String(40), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"], ondelete="CASCADE"))
        op.create_index("ix_governance_metric_snapshots_project_id", "governance_metric_snapshots", ["project_id"])
        op.create_index("ix_governance_metric_snapshots_batch_id", "governance_metric_snapshots", ["batch_id"])
    if "governance_export_jobs" not in tables:
        op.create_table("governance_export_jobs",
            sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), nullable=False), sa.Column("kind", sa.String(32), nullable=False, server_default="cases"),
            sa.Column("format", sa.String(8), nullable=False, server_default="xlsx"), sa.Column("path", sa.String(1024), nullable=False, server_default=""),
            sa.Column("rows", sa.Integer(), nullable=False, server_default="0"), sa.Column("selfcheck", sa.JSON(), nullable=False), sa.Column("created_by", sa.Integer(), nullable=False), sa.Column("created_at", sa.String(40), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["created_by"], ["users.id"]))
        op.create_index("ix_governance_export_jobs_project_id", "governance_export_jobs", ["project_id"])


def downgrade():
    # Governance data is audit history; restore from a database backup rather
    # than silently dropping it during a normal application downgrade.
    raise RuntimeError("Downgrading case governance would remove audit data; restore a database backup instead")

