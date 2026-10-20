"""Normalize model endpoints and separate installed AI tools."""

import json

import sqlalchemy as sa
from alembic import op


revision = "20261027_model_tool_schema"
down_revision = "20261026_project_archiving"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ai_models"):
        op.create_table(
            "ai_models",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_id", sa.String(80), nullable=False, unique=True),
            sa.Column("name", sa.String(120), nullable=False),
            # Explicit deployment marker; do not infer local/remote from URL.
            sa.Column("scope", sa.String(16), nullable=False, server_default="remote"),
            sa.Column("endpoint", sa.String(255), nullable=False, server_default=""),
            sa.Column("model", sa.String(120), nullable=False, server_default=""),
            sa.Column("protocol", sa.String(32), nullable=False, server_default="openai"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("detail", sa.String(160), nullable=False, server_default=""),
            sa.Column("api_key_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.String(40), nullable=False, server_default=""),
            sa.Column("updated_at", sa.String(40), nullable=False, server_default=""),
            sa.CheckConstraint("scope IN ('local', 'remote')", name="ck_ai_models_scope"),
        )
    if not inspector.has_table("ai_tools"):
        op.create_table(
            "ai_tools",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tool_id", sa.String(40), nullable=False, unique=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("command", sa.String(255), nullable=False, server_default=""),
            sa.Column("model", sa.String(120), nullable=False, server_default=""),
            sa.Column("detail", sa.String(160), nullable=False, server_default=""),
            sa.Column("created_at", sa.String(40), nullable=False, server_default=""),
            sa.Column("updated_at", sa.String(40), nullable=False, server_default=""),
        )

    # Backfill model catalog rows from the additive JSON catalog and legacy
    # singleton fields. Existing rows are left untouched for idempotency.
    rows = bind.execute(sa.text(
        "select id, local_url, local_model, remote_enabled, remote_url, remote_model, "
        "providers_json, codex_enabled, codex_command, codex_model, "
        "claude_enabled, claude_command, claude_model from ai_settings"
    )).mappings().all()
    for settings in rows:
        catalog = []
        try:
            catalog = json.loads(settings.get("providers_json") or "[]")
        except (TypeError, ValueError):
            pass
        if not isinstance(catalog, list):
            catalog = []
        if not any(isinstance(item, dict) and item.get("id") == "local" for item in catalog):
            catalog.insert(0, {"id": "local", "kind": "local", "name": "本机 Ollama",
                               "endpoint": settings.get("local_url") or "", "model": settings.get("local_model") or "qwen3:8b", "enabled": True})
        if settings.get("remote_enabled") and not any(isinstance(item, dict) and item.get("id") == "remote" for item in catalog):
            catalog.append({"id": "remote", "kind": "remote", "name": "远程模型",
                            "endpoint": settings.get("remote_url") or "", "model": settings.get("remote_model") or "", "enabled": True})
        for item in catalog:
            if not isinstance(item, dict) or item.get("kind") not in {"local", "remote"} or not item.get("id"):
                continue
            provider_id = str(item["id"])[:80]
            exists = bind.execute(sa.text("select 1 from ai_models where provider_id=:id"), {"id": provider_id}).first()
            if exists:
                continue
            bind.execute(sa.text("insert into ai_models (provider_id,name,scope,endpoint,model,protocol,enabled,detail,api_key_encrypted,created_at,updated_at) values (:id,:name,:scope,:endpoint,:model,:protocol,:enabled,:detail,:secret,:created,:updated)"), {
                "id": provider_id, "name": str(item.get("name") or provider_id)[:120], "scope": str(item.get("kind") or "remote"),
                "endpoint": str(item.get("endpoint") or "")[:255], "model": str(item.get("model") or "")[:120], "protocol": str(item.get("protocol") or "openai")[:32],
                "enabled": bool(item.get("enabled", False)), "detail": str(item.get("detail") or "")[:160],
                "secret": str(item.get("api_key_encrypted") or ""), "created": "", "updated": "",
            })
        for tool_id, name, enabled, command, model in (("codex_cli", "Codex CLI", settings.get("codex_enabled", False), settings.get("codex_command") or "codex", settings.get("codex_model") or ""), ("claude_code", "Claude Code", settings.get("claude_enabled", False), settings.get("claude_command") or "claude", settings.get("claude_model") or "")):
            if not bind.execute(sa.text("select 1 from ai_tools where tool_id=:id"), {"id": tool_id}).first():
                bind.execute(sa.text("insert into ai_tools (tool_id,name,enabled,command,model,detail,created_at,updated_at) values (:id,:name,:enabled,:command,:model,'','','')"), {"id": tool_id, "name": name, "enabled": bool(enabled), "command": command, "model": model})


def downgrade():
    raise RuntimeError("AI model/tool configuration is intentionally not downgradable without a backup")
