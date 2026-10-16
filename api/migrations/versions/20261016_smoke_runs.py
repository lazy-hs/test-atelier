"""Smoke suites and frozen execution runs.

Revision ID: 20261016_smoke_runs
Revises: 20261015_taxonomy_suggestions
"""

import sqlalchemy as sa
from alembic import op

revision = "20261016_smoke_runs"
down_revision = "20261015_taxonomy_suggestions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("smoke_suites"):
        op.create_table(
            "smoke_suites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("target_version", sa.String(80), nullable=False),
            sa.Column("environment", sa.String(80), nullable=False),
            sa.Column("required_chains", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
    if "ix_smoke_suites_project_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("smoke_suites")}:
        op.create_index("ix_smoke_suites_project_id", "smoke_suites", ["project_id"])
    if not sa.inspect(bind).has_table("smoke_items"):
        op.create_table(
            "smoke_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("suite_id", sa.Integer(), sa.ForeignKey("smoke_suites.id"), nullable=False),
            sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("case_version", sa.Integer(), nullable=False),
            sa.Column("chain_name", sa.String(120), nullable=False),
            sa.Column("estimated_seconds", sa.Integer(), nullable=False),
            sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("confirmed_at", sa.String(40), nullable=False),
            sa.UniqueConstraint("suite_id", "case_id", name="uq_smoke_suite_case"),
        )
    if "ix_smoke_items_suite_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("smoke_items")}:
        op.create_index("ix_smoke_items_suite_id", "smoke_items", ["suite_id"])
    if not sa.inspect(bind).has_table("execution_runs"):
        op.create_table(
            "execution_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("suite_id", sa.Integer(), sa.ForeignKey("smoke_suites.id"), nullable=False),
            sa.Column("suite_revision", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("target_version", sa.String(80), nullable=False),
            sa.Column("environment", sa.String(80), nullable=False),
            sa.Column("required_chains", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("started_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("started_at", sa.String(40), nullable=False),
            sa.Column("completed_by", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("completed_at", sa.String(40)),
        )
    if "ix_execution_runs_project_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("execution_runs")}:
        op.create_index("ix_execution_runs_project_id", "execution_runs", ["project_id"])
    if not sa.inspect(bind).has_table("run_cases"):
        op.create_table(
            "run_cases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("execution_runs.id"), nullable=False),
            sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("case_version", sa.Integer(), nullable=False),
            sa.Column("original_id", sa.String(255), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("level", sa.String(10), nullable=False),
            sa.Column("smoke", sa.String(10), nullable=False),
            sa.Column("chain_name", sa.String(120), nullable=False),
            sa.Column("estimated_seconds", sa.Integer(), nullable=False),
            sa.Column("result", sa.String(20), nullable=False),
            sa.Column("actual_seconds", sa.Integer()),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("executed_by", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("executed_at", sa.String(40)),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint("run_id", "case_id", name="uq_run_case"),
        )
    if "ix_run_cases_run_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("run_cases")}:
        op.create_index("ix_run_cases_run_id", "run_cases", ["run_id"])
    if not sa.inspect(bind).has_table("execution_events"):
        op.create_table(
            "execution_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_case_id", sa.Integer(), sa.ForeignKey("run_cases.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("before", sa.JSON(), nullable=False),
            sa.Column("after", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_execution_events_run_case_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("execution_events")}:
        op.create_index("ix_execution_events_run_case_id", "execution_events", ["run_case_id"])


def downgrade():
    raise RuntimeError("执行历史不可无损降级，请恢复升级前的数据库备份")
