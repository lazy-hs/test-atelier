import json

from api import config_file


class Settings:
    local_url = "http://127.0.0.1:11434"
    local_model = "qwen3:8b"
    remote_enabled = False
    remote_url = ""
    remote_model = ""
    remote_api_key = "encrypted-value"
    codex_enabled = True
    codex_command = "codex"
    codex_model = "gpt-test"
    claude_enabled = False
    claude_command = "claude"
    claude_model = ""
    providers_json = json.dumps([{
        "id": "relay", "kind": "remote", "name": "Relay",
        "endpoint": "https://relay.example/v1", "model": "model",
        "enabled": True, "detail": "remote", "protocol": "openai",
        "api_key_encrypted": "catalog-encrypted",
    }])


def test_config_file_persists_models_and_skill_metadata(tmp_path, monkeypatch):
    target = tmp_path / "config" / "settings.json"
    monkeypatch.setenv("AI_STUDIO_CONFIG_FILE", str(target))

    assert config_file.persist_model_settings(Settings()) is True
    assert config_file.persist_skills([{
        "skill_id": "requirement-analysis", "name": "qa-doc-skills-requirement-analysis",
        "label": "需求分析", "path": "skills/requirement-analysis/SKILL.md",
        "description": "metadata only", "enabled_by_default": True,
        "content": "must not be persisted",
    }], root="/tmp/qa-doc-skills") is True

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["models"]["remote"]["api_key_encrypted"] == "encrypted-value"
    assert payload["models"]["providers"][0]["api_key_encrypted"] == "catalog-encrypted"
    assert payload["skills_root"] == "/tmp/qa-doc-skills"
    assert payload["skills"] == [{
        "id": "requirement-analysis", "name": "qa-doc-skills-requirement-analysis",
        "label": "需求分析", "path": "skills/requirement-analysis/SKILL.md",
        "description": "metadata only", "enabled_by_default": True,
    }]
    assert "must not be persisted" not in target.read_text(encoding="utf-8")
    assert oct(target.stat().st_mode & 0o777) == "0o600"
