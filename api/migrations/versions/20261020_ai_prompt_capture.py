"""Persist prompts sent by requirement and test-case AI jobs."""

import sqlalchemy as sa
from alembic import op


revision = "20261020_ai_prompt_capture"
down_revision = "20261019_cli_providers"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("ai_jobs")}
    columns = [
        ("prompts", sa.JSON(), sa.text("'[]'")),
        ("prompt_length", sa.Integer(), sa.text("0")),
        ("prompt_calls", sa.Integer(), sa.text("0")),
        ("prompt_sha256", sa.String(64), sa.text("''")),
    ]
    for name, type_, default in columns:
        if name not in existing:
            op.add_column("ai_jobs", sa.Column(name, type_, nullable=False, server_default=default))
    # ORM defaults are used for newly-created jobs. SQLite may retain the
    # harmless server default because it cannot alter a column in place.
    if bind.dialect.name != "sqlite":
        for name, _, _ in columns:
            op.alter_column("ai_jobs", name, server_default=None)


def downgrade():
    raise RuntimeError("Prompt history is additive; restore a database backup instead")
