"""Store the project-level qa-doc-skills registry and activation state."""

import sqlalchemy as sa
from alembic import op


revision = "20261018_project_skills"
down_revision = "20261017_ai_settings"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("project_skills"):
        op.create_table(
            "project_skills",
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("skill_id", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column("source_path", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )


def downgrade():
    raise RuntimeError("Project skill state is additive; restore a database backup instead")
