"""Persist global model provider settings without exposing API keys."""

import sqlalchemy as sa
from alembic import op

revision = "20261017_ai_settings"
down_revision = "20261016_smoke_runs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("ai_settings"):
        op.create_table(
            "ai_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("local_url", sa.String(255), nullable=False),
            sa.Column("local_model", sa.String(120), nullable=False),
            sa.Column("remote_enabled", sa.Boolean(), nullable=False),
            sa.Column("remote_url", sa.String(255), nullable=False),
            sa.Column("remote_model", sa.String(120), nullable=False),
            sa.Column("remote_api_key", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
        )
    count = bind.execute(sa.text("select count(*) from ai_settings")).scalar_one()
    if not count:
        bind.execute(sa.text("insert into ai_settings (id, local_url, local_model, remote_enabled, remote_url, remote_model, remote_api_key, updated_at) values (1, :url, :model, :enabled, '', '', '', :updated)"),
                     {"url": "http://127.0.0.1:11434", "model": "qwen3:8b", "enabled": False,
                      "updated": "2026-10-16T00:00:00+00:00"})


def downgrade():
    raise RuntimeError("AI 配置包含运行时密钥，不支持无备份降级")
