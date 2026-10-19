"""Persist optional local Codex CLI and Claude Code provider settings."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_cli_providers"
down_revision = "20261018_project_skills"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("ai_settings")}
    columns = [
        ("codex_enabled", sa.Boolean(), False),
        ("codex_command", sa.String(255), "codex"),
        ("codex_model", sa.String(120), ""),
        ("claude_enabled", sa.Boolean(), False),
        ("claude_command", sa.String(255), "claude"),
        ("claude_model", sa.String(120), ""),
    ]
    for name, type_, default in columns:
        if name not in existing:
            op.add_column("ai_settings", sa.Column(name, type_, nullable=False,
                                                     server_default=sa.false() if isinstance(type_, sa.Boolean) else sa.text("''")))
    # Defaults are only needed while adding columns; ORM supplies them
    # afterwards. SQLite cannot alter column defaults in place, so retain the
    # harmless default there while surfacing real PostgreSQL migration errors.
    if bind.dialect.name != "sqlite":
        for name, _, _ in columns:
            op.alter_column("ai_settings", name, server_default=None)


def downgrade():
    raise RuntimeError("CLI provider configuration is intentionally not downgradable without a backup")
