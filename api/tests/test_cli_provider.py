import json

import pytest

from api import ai_engine


def test_cli_provider_uses_argv_and_parses_jsonl(monkeypatch):
    calls = {}

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"type": "item.completed", "item": {"text": json.dumps({"ok": True})}})

    def run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(ai_engine.subprocess, "run", run)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    result = ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex", "gpt-test")
    assert result == {"ok": True}
    assert calls["args"][:5] == ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox"]
    assert "--model" in calls["args"] and "gpt-test" in calls["args"]
    assert calls["args"][-1] == "-"
    assert calls["kwargs"]["input"] == "hello"
    assert calls["kwargs"]["shell"] is not True


def test_cli_provider_rejects_shell_syntax():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(RuntimeError, match="单个可执行"):
        ai_engine._request_cli_structured("hello", schema, "codex_cli", "codex --danger", "")


def test_cli_provider_disabled(monkeypatch):
    monkeypatch.setattr(ai_engine, "CODEX_ENABLED", False)
    with pytest.raises(RuntimeError, match="Codex CLI"):
        ai_engine.request_structured("hello", {}, "codex_cli")
