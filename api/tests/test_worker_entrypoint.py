import json

from api import worker_entrypoint


def test_worker_entrypoint_preflights_before_importing_worker(tmp_path, monkeypatch, capsys):
    skill_root = tmp_path / "skills"
    (skill_root / "skills" / "hub").mkdir(parents=True)
    (skill_root / "skills" / "requirement-analysis").mkdir()
    (skill_root / "skills" / "test-cases").mkdir()
    (skill_root / "AGENTS.md").write_text("# rules", encoding="utf-8")
    for path in (
        skill_root / "skills" / "hub" / "SKILL.md",
        skill_root / "skills" / "requirement-analysis" / "SKILL.md",
        skill_root / "skills" / "test-cases" / "SKILL.md",
    ):
        path.write_text("# skill", encoding="utf-8")
    monkeypatch.setenv("AI_STUDIO_ENV", "production")
    monkeypatch.setenv("AI_STUDIO_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("AI_STUDIO_ADMIN_PASSWORD", "p" * 16)
    monkeypatch.setenv("TEST_ATELIER_SKILL_ROOT", str(skill_root))
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DB_POOL_SIZE", "bad-value")

    assert worker_entrypoint._preflight() is False
    output = capsys.readouterr().out
    assert '"event":"worker.preflight.failed"' in output
    assert "bad-value" not in output


def test_worker_entrypoint_failure_is_structured(monkeypatch, capsys):
    monkeypatch.setattr(worker_entrypoint, "main", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker_entrypoint.run() == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["event"] == "worker.entrypoint.failed"
    assert payload["request_id"] == "-"


def test_worker_startup_log_fields_are_redacted_and_payloads_are_dropped(capsys):
    worker_entrypoint._log(
        "worker.test", "INFO", message="api_key=top-secret", prompts=["sensitive"],
        worker_id="worker-test",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == "api_key=[REDACTED]"
    assert payload["worker_id"] == "worker-test"
    assert "prompts" not in payload


def test_worker_startup_logs_include_configured_release(monkeypatch, capsys):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "release-2026.10.18")
    worker_entrypoint._log("worker.release", "INFO")
    payload = json.loads(capsys.readouterr().out)
    assert payload["release"] == "release-2026.10.18"


def test_worker_entrypoint_skips_http_only_security_checks(monkeypatch):
    calls = {}

    def fake_preflight(env, **kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setenv("AI_STUDIO_ENV", "production")
    monkeypatch.setattr("scripts.preflight_production.run_preflight", fake_preflight)

    assert worker_entrypoint._preflight() is True
    assert calls["require_admin_password"] is False
    assert calls["require_http_security"] is False
