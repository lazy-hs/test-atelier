"""Regression coverage for the normalized model/tool configuration schema.

The model catalog and installed AI tools intentionally have different storage
contracts.  Keep these checks separate from the broad settings workflow tests
so a future migration cannot silently merge CLI tools back into ``ai_models``.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from sqlalchemy import select  # noqa: E402

from api.db import AiModel, AiSettings, AiTool, Base, SessionLocal, engine  # noqa: E402
from api.tests.test_ai_settings import context  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[2]


def test_model_and_tool_catalogs_are_persisted_in_separate_tables(context):
    client, headers, root = context
    secret = "sk-normalized-secret"
    payload = {
        "local_url": "http://127.0.0.1:11434",
        "local_model": "qwen3:8b",
        "remote_enabled": True,
        "remote_url": "https://relay.example/v1",
        "remote_model": "relay-model",
        "remote_api_key": secret,
        "codex_enabled": True,
        "codex_command": "codex",
        "codex_model": "gpt-test",
        "claude_enabled": False,
        "claude_command": "claude",
        "claude_model": "sonnet-test",
        "providers": [
            {"id": "local", "kind": "local", "name": "本机 Ollama",
             "endpoint": "http://127.0.0.1:11434", "model": "qwen3:8b",
             "enabled": True, "protocol": "openai"},
            {"id": "relay", "kind": "remote", "name": "Relay",
             "endpoint": "https://relay.example/v1", "model": "relay-model",
             "enabled": True, "protocol": "openai", "api_key": secret},
            # A CLI entry is legacy-compatible JSON metadata only.  It must
            # never become a model endpoint row.
            {"id": "codex_cli", "kind": "cli", "name": "Codex CLI",
             "endpoint": "", "model": "gpt-test", "enabled": True,
             "protocol": "system-one"},
        ],
    }
    response = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    with SessionLocal() as db:
        models = db.scalars(select(AiModel).order_by(AiModel.provider_id)).all()
        tools = db.scalars(select(AiTool).order_by(AiTool.tool_id)).all()
        settings = db.get(AiSettings, 1)
        assert settings is not None
        assert {row.provider_id for row in models} == {"local", "relay"}
        assert {row.scope for row in models} == {"local", "remote"}
        assert all(row.provider_id not in {"codex_cli", "claude_code"} for row in models)
        assert {row.tool_id for row in tools} == {"codex_cli", "claude_code"}
        codex = next(row for row in tools if row.tool_id == "codex_cli")
        assert codex.enabled is True and codex.command == "codex" and codex.model == "gpt-test"
        assert secret not in (settings.providers_json or "")
        relay = next(row for row in models if row.provider_id == "relay")
        assert relay.api_key_encrypted and relay.api_key_encrypted != secret

    assert {row["id"] for row in body["models"]} == {"local", "relay"}
    assert {row["id"] for row in body["tools"]} == {"codex_cli", "claude_code"}
    assert secret not in response.text


def test_legacy_settings_payload_still_materializes_model_rows(context):
    """Development databases created with ``create_all`` have no migration seed."""
    client, headers, root = context
    payload = {
        "local_url": "http://127.0.0.1:11434",
        "local_model": "qwen3:8b",
        "local_enabled": False,
        "remote_enabled": True,
        "remote_url": "https://relay.example/v1",
        "remote_model": "relay-model",
        "remote_api_key": "sk-legacy-payload",
    }
    response = client.put(f"{root}/ai/admin-config", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        rows = db.scalars(select(AiModel).order_by(AiModel.provider_id)).all()
        assert {row.provider_id: row.scope for row in rows} == {"local": "local", "remote": "remote"}
        assert next(row for row in rows if row.provider_id == "local").enabled is False


def test_fresh_schema_seeds_local_model_and_cli_tools_without_merging_them(tmp_path):
    path = tmp_path / "fresh-model-tools.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20261027_model_tool_schema"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{path}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(path) as db:
        models = db.execute(
            "SELECT provider_id, scope FROM ai_models ORDER BY provider_id"
        ).fetchall()
        tools = db.execute(
            "SELECT tool_id, enabled, command FROM ai_tools ORDER BY tool_id"
        ).fetchall()
        assert models == [("local", "local")]
        assert [row[0] for row in tools] == ["claude_code", "codex_cli"]
        assert all(row[0] not in {"claude_code", "codex_cli"} for row in models)


def test_migration_backfills_legacy_catalog_and_cli_columns(tmp_path):
    path = tmp_path / "legacy-model-tools.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{path}"}
    for revision in ("20261026_project_archiving", "20261027_model_tool_schema"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        if revision == "20261026_project_archiving":
            with sqlite3.connect(path) as db:
                db.execute(
                    """UPDATE ai_settings SET providers_json=?, codex_enabled=1,
                    codex_command=?, codex_model=?, claude_enabled=1,
                    claude_command=?, claude_model=? WHERE id=1""",
                    (
                        '[{"id":"legacy-remote","kind":"remote","name":"Legacy Relay",'
                        '"endpoint":"https://legacy.example/v1","model":"legacy-model",'
                        '"enabled":true,"protocol":"openai"}]',
                        "codex",
                        "gpt-legacy",
                        "claude",
                        "sonnet-legacy",
                    ),
                )
                db.commit()
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT provider_id, scope, endpoint FROM ai_models ORDER BY provider_id"
        ).fetchall() == [
            ("legacy-remote", "remote", "https://legacy.example/v1"),
            ("local", "local", "http://127.0.0.1:11434"),
        ]
        assert db.execute(
            "SELECT tool_id, enabled, command, model FROM ai_tools ORDER BY tool_id"
        ).fetchall() == [
            ("claude_code", 1, "claude", "sonnet-legacy"),
            ("codex_cli", 1, "codex", "gpt-legacy"),
        ]

