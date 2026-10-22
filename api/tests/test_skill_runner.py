import os
import threading
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("AI_STUDIO_SESSION_SECRET", "test-only-session-secret-32-characters-long")
os.environ.setdefault("AI_STUDIO_ADMIN_PASSWORD", "test-admin-password-123")

from api import ai_engine, main, skill_runner
import pytest


def test_skill_scripts_do_not_receive_application_secrets(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("AI_STUDIO_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db/app")
    monkeypatch.setenv("AI_STUDIO_REMOTE_API_KEY", "remote-secret")
    monkeypatch.setenv("AI_STUDIO_CODEX_ENABLED", "1")
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/tmp/desktop.sock")
    monkeypatch.setenv("CODEX_THREAD_ID", "desktop-thread")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = skill_runner._script_environment()

    assert "AI_STUDIO_SESSION_SECRET" not in environment
    assert "AI_STUDIO_ADMIN_PASSWORD" not in environment
    assert "DATABASE_URL" not in environment
    assert "AI_STUDIO_REMOTE_API_KEY" not in environment
    assert "AI_STUDIO_CODEX_ENABLED" not in environment
    assert "CODEX_APP_TOOLS_PIPE_PATH" not in environment
    assert "CODEX_THREAD_ID" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["PATH"] == "/usr/bin"


def test_skill_script_failure_redacts_credentials(monkeypatch):
    class Result:
        returncode = 1
        stderr = "postgresql://app:db-secret@db/app password=hunter2"
        stdout = ""

    monkeypatch.setattr(skill_runner, "run_script", lambda *args, **kwargs: Result())
    with pytest.raises(skill_runner.SkillRunnerError) as error:
        skill_runner.generate_xmind([], "demo")
    message = str(error.value)
    assert "db-secret" not in message
    assert "hunter2" not in message
    assert "[REDACTED]" in message


def test_project_skill_materialization_returns_sanitized_service_error(monkeypatch):
    monkeypatch.setattr(main, "discover_skills", lambda: (_ for _ in ()).throw(
        skill_runner.SkillRunnerError("技能文件不存在：/Users/private/qa-doc-skills/AGENTS.md")
    ))
    with pytest.raises(main.HTTPException) as error:
        main.ensure_project_skills(1, None)
    assert error.value.status_code == 503
    assert error.value.detail == "项目技能仓不可用"


def test_skill_file_reads_are_bounded(tmp_path):
    source = tmp_path / "large-skill.md"
    source.write_text("x" * 100_000, encoding="utf-8")
    assert len(skill_runner._read(source, 128)) == 128


def test_skill_script_cancellation_terminates_child_process(tmp_path, monkeypatch):
    script_root = tmp_path / "skills"
    script_root.mkdir()
    script = script_root / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setenv("TEST_ATELIER_SKILL_ROOT", str(script_root))
    cancelled = threading.Event()
    monkeypatch.setattr("api.ai_engine.get_cancel_event", lambda: cancelled)

    error: list[BaseException] = []

    def invoke():
        try:
            skill_runner.run_script("sleep.py", [])
        except BaseException as exc:  # the assertion below checks the exact cancellation type
            error.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    time.sleep(0.25)
    cancelled.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(error) == 1
    assert isinstance(error[0], ai_engine.CliCancelled)


def test_codex_requirement_analysis_prompt_follows_skill_phases(monkeypatch):
    captured: list[str] = []

    def fake_context(_skill_id, references=()):
        return "# skill context\n" + "\n".join(references)

    def fake_model(prompt, _schema, _provider):
        captured.append(prompt)
        return {
            "requirements": [{
                "id": "REQ-1", "title": "登录", "summary": "账号登录",
                "source_ids": ["doc-1"], "acceptance": ["正确账号进入首页"], "methods": ["EP"],
            }],
            "ambiguities": [], "risks": ["数据一致性"],
        }

    monkeypatch.setattr(skill_runner, "load_context", fake_context)
    monkeypatch.setattr(skill_runner, "load_memory_context", lambda: "# .memory snapshots")
    monkeypatch.setattr(skill_runner, "skill_root", lambda: Path("/skills"))

    result = skill_runner.analyze([{"id": "doc-1", "filename": "login.md", "text": "# Login"}], "codex_cli", fake_model)

    assert result["requirements"][0]["source_ids"] == ["doc-1"]
    prompt = captured[0]
    assert "Phase 0" in prompt and "Phase 2.8" in prompt and "Phase 4" in prompt
    assert "NIELSEN-HEURISTICS.md" in prompt
    assert ".memory snapshots" in prompt
    assert "不得生成测试用例 JSON、XMind 或 tmp/cases_*.json" in prompt


def test_codex_test_case_prompt_follows_skill_workflow(monkeypatch):
    captured: list[str] = []

    def fake_context(_skill_id, references=()):
        return "# skill context\n" + "\n".join(references)

    def fake_model(prompt, _schema, _provider):
        captured.append(prompt)
        return {"cases": [{
            "module": ["风险点", "数据一致性"], "title": "登录结果与首页状态一致",
            "priority": "P1", "precondition": "存在有效账号",
            "steps": [{"action": "1. 输入有效账号", "expected": "1. 账号输入框显示对应内容"}],
            "requirement_ids": ["REQ-1"], "method": "ST", "label": "Web",
            "remark": "依据需求中的数据一致性风险", "smoke": "",
        }]}

    monkeypatch.setattr(skill_runner, "load_context", fake_context)
    monkeypatch.setattr(skill_runner, "load_memory_context", lambda: "# .memory snapshots")
    monkeypatch.setattr(skill_runner, "skill_root", lambda: Path("/skills"))

    cases = skill_runner.generate_cases({
        "requirements": [{"id": "REQ-1", "title": "登录", "summary": "账号登录",
                          "source_ids": ["doc-1"], "acceptance": ["进入首页"], "methods": ["ST"]}],
        "ambiguities": [], "risks": ["数据一致性"],
        "report_markdown": "# 需求分析\n登录",
    }, "codex_cli", fake_model)

    assert cases[0]["module"][:2] == ["风险点", "数据一致性"]
    prompt = captured[0]
    assert "Phase 0" in prompt and "Phase 2.8" in prompt and "Phase 3" in prompt and "Phase 4" in prompt
    assert "TEST-CASE-RULES.md" in prompt and "INTERACTION-PATTERNS.md" in prompt
    assert ".memory snapshots" in prompt
    assert "[风险点, 风险类别]" in prompt
    assert "不输出解释、Markdown 围栏、XMind 或临时文件" in prompt
