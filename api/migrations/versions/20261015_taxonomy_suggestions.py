"""Versioned classification and separate rule/model suggestions.

Revision ID: 20261015_taxonomy_suggestions
Revises: 20261015_review_collab
"""

import sqlalchemy as sa
from alembic import op

revision = "20261015_taxonomy_suggestions"
down_revision = "20261015_review_collab"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("taxonomy_versions"):
        op.create_table(
            "taxonomy_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("definitions", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("published_at", sa.String(40)),
            sa.UniqueConstraint("project_id", "number", name="uq_taxonomy_project_number"),
        )
    if "ix_taxonomy_versions_project_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("taxonomy_versions")}:
        op.create_index("ix_taxonomy_versions_project_id", "taxonomy_versions", ["project_id"])
    columns = {column["name"] for column in sa.inspect(bind).get_columns("cases")}
    if "final_categories" not in columns:
        op.add_column("cases", sa.Column("final_categories", sa.JSON(), nullable=True))
    has_version_fk = any(key["constrained_columns"] == ["final_taxonomy_version_id"]
                         for key in sa.inspect(bind).get_foreign_keys("cases"))
    if "final_taxonomy_version_id" not in columns or not has_version_fk:
        with op.batch_alter_table("cases") as batch:
            if "final_taxonomy_version_id" not in columns:
                batch.add_column(sa.Column("final_taxonomy_version_id", sa.Integer(), nullable=True))
            if not has_version_fk:
                batch.create_foreign_key("fk_cases_final_taxonomy_version", "taxonomy_versions",
                                         ["final_taxonomy_version_id"], ["id"])
    if not sa.inspect(bind).has_table("suggestion_jobs"):
        op.create_table(
            "suggestion_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("taxonomy_version_id", sa.Integer(), sa.ForeignKey("taxonomy_versions.id")),
            sa.Column("provider", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("case_ids", sa.JSON(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column("completed", sa.Integer(), nullable=False),
            sa.Column("errors", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
        )
    if "ix_suggestion_jobs_project_id" not in {index["name"] for index in sa.inspect(bind).get_indexes("suggestion_jobs")}:
        op.create_index("ix_suggestion_jobs_project_id", "suggestion_jobs", ["project_id"])
    if not sa.inspect(bind).has_table("case_suggestions"):
        op.create_table(
            "case_suggestions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("taxonomy_version_id", sa.Integer(), sa.ForeignKey("taxonomy_versions.id")),
            sa.Column("job_id", sa.String(36), sa.ForeignKey("suggestion_jobs.id")),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("case_version", sa.Integer(), nullable=False),
            sa.Column("categories", sa.JSON(), nullable=False),
            sa.Column("level", sa.String(10)),
            sa.Column("smoke", sa.String(10)),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("questions", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("decided_at", sa.String(40)),
            sa.Column("decision_reason", sa.Text()),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("case_suggestions")}
    for name, column in (("ix_case_suggestions_project_id", "project_id"), ("ix_case_suggestions_case_id", "case_id")):
        if name not in existing:
            op.create_index(name, "case_suggestions", [column])


def downgrade():
    raise RuntimeError("建议与分类历史不可无损降级，请恢复升级前的数据库备份")
