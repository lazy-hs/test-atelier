from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.runtime_config import RuntimeConfigError, environment_from_config, load_runtime_config


def test_runtime_config_flattens_file_backed_secrets(tmp_path: Path):
    key = tmp_path / "model-key"
    key.write_text("model-key-value-1234\n", encoding="utf-8")
    scope = tmp_path / "scope"
    scope.write_text("s" * 40 + "\n", encoding="utf-8")
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "paths": {"skill_root": str(tmp_path)},
        "server": {"api_port": 8010},
        "harness": {"port": 8890, "bind_host": "127.0.0.1", "harness_only": True},
        "model": {"api_base": "http://model.invalid/v1", "model": "test", "api_key_file": str(key)},
        "secrets": {"scope_secret_file": str(scope)},
    }), encoding="utf-8")

    document = load_runtime_config(config, required=True)
    values = environment_from_config(document)
    assert values["AI_STUDIO_PORT"] == "8010"
    assert "TEST_ATELIER_HARNESS_URL" not in values
    assert values["OPENAI_API_KEY_FILE"] == str(key)
    assert values["OPENAI_API_KEY"] == "model-key-value-1234"
    assert values["TEST_ATELIER_SCOPE_SECRET_FILE"] == str(scope)
    assert "s" * 40 not in json.dumps(document)


def test_runtime_config_rejects_unknown_schema(tmp_path: Path):
    config = tmp_path / "runtime.json"
    config.write_text('{"schema_version": 99}', encoding="utf-8")
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(config, required=True)


def test_runtime_config_keeps_web_shell_disabled_by_default(tmp_path: Path):
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"schema_version": 1, "harness": {}}), encoding="utf-8")
    values = environment_from_config(load_runtime_config(config, required=True))
    assert values["TEST_ATELIER_WEB_SHELL_PILOT"] == "0"
    assert values["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"] == "0"
    assert values["TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE"] == "1"


def test_runtime_config_enables_web_shell_from_json(tmp_path: Path):
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"schema_version": 1, "harness": {"web_shell": {
        "enabled": True, "member_access": True, "restricted_profile": True,
    }}}), encoding="utf-8")
    values = environment_from_config(load_runtime_config(config, required=True))
    assert values["TEST_ATELIER_WEB_SHELL_PILOT"] == "1"
    assert values["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"] == "1"


def test_runtime_config_requires_postgres_password_file(tmp_path: Path):
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "database": {"engine": "postgresql", "password_file": str(tmp_path / "missing")},
    }), encoding="utf-8")
    with pytest.raises(RuntimeConfigError):
        environment_from_config(load_runtime_config(config, required=True))
