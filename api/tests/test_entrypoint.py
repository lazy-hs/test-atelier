import json
from types import SimpleNamespace

from pathlib import Path

from api import entrypoint


def _production_env(tmp_path: Path, monkeypatch):
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
    monkeypatch.setenv("COOKIE_SECURE", "1")
    monkeypatch.setenv("METRICS_TOKEN", "production-metrics-token-123456")
    monkeypatch.setenv("TEST_ATELIER_SKILL_ROOT", str(skill_root))
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path / "data"))


def test_production_preflight_runs_before_startup(tmp_path, monkeypatch, capsys):
    _production_env(tmp_path, monkeypatch)
    monkeypatch.setattr("scripts.preflight_production.importlib.metadata.version", lambda name: "50.0.2")
    assert entrypoint._production_preflight() is True
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["request_id"] == "-"
    assert payload["event"] == "production.preflight.completed"


def test_production_preflight_hides_invalid_configuration_details(tmp_path, monkeypatch, capsys):
    _production_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DB_POOL_SIZE", "invalid-secret-looking-value")
    assert entrypoint._production_preflight() is False
    output = capsys.readouterr().out
    assert '"event":"production.preflight.failed"' in output
    assert "invalid-secret-looking-value" not in output


def test_startup_log_fields_are_redacted_and_payloads_are_dropped(capsys):
    entrypoint._log(
        "startup.test", "INFO", message="password=top-secret", prompt="sensitive prompt",
        component="test", database_url="postgresql://app:top-secret@db/app",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == "password=[REDACTED]"
    assert "prompt" not in payload
    assert "database_url" not in payload


def test_startup_logs_include_configured_release(monkeypatch, capsys):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "release-2026.10.18")
    entrypoint._log("startup.release", "INFO")
    payload = json.loads(capsys.readouterr().out)
    assert payload["release"] == "release-2026.10.18"


def test_entrypoint_logs_migration_success_before_exec(monkeypatch, capsys):
    calls = {}
    run_kwargs = {}

    def run(*args, **kwargs):
        run_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(entrypoint.subprocess, "run", run)

    def execv(path, args):
        calls["path"] = path
        calls["args"] = args
        raise SystemExit(0)

    monkeypatch.setattr(entrypoint.os, "execv", execv)
    try:
        entrypoint.main()
    except SystemExit as exc:
        assert exc.code == 0
    assert calls["args"][1:3] == ["-m", "api.uvicorn_entrypoint"]
    assert run_kwargs["cwd"] == str(entrypoint.PROJECT_ROOT)
    assert '"event":"database.migration.completed"' in capsys.readouterr().out


def test_entrypoint_does_not_echo_migration_output_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout="postgresql://app:secret@db/atelier", stderr="password=secret",
    ))
    assert entrypoint.main() == 1
    output = capsys.readouterr().out
    assert '"event":"database.migration.failed"' in output
    assert "secret" not in output
    assert "[REDACTED]" in output


def test_entrypoint_bounds_migration_wait(monkeypatch, capsys):
    def timeout(*_args, **_kwargs):
        raise entrypoint.subprocess.TimeoutExpired("alembic", 1)

    monkeypatch.setattr(entrypoint.subprocess, "run", timeout)
    monkeypatch.setenv("AI_STUDIO_MIGRATION_TIMEOUT", "1")
    assert entrypoint.main() == 124
    assert '"error_type":"MigrationTimeout"' in capsys.readouterr().out


def test_entrypoint_reports_migration_launch_failure(monkeypatch, capsys):
    def launch_failure(*_args, **_kwargs):
        raise OSError("alembic unavailable at postgresql://app:secret@db/atelier")

    monkeypatch.setattr(entrypoint.subprocess, "run", launch_failure)
    assert entrypoint.main() == 127
    output = capsys.readouterr().out
    assert '"error_type":"MigrationLaunchError"' in output
    assert "secret" not in output


def test_entrypoint_reports_invalid_migration_timeout(monkeypatch, capsys):
    monkeypatch.setenv("AI_STUDIO_MIGRATION_TIMEOUT", "invalid")
    assert entrypoint.main() == 2
    assert '"error_type":"InvalidMigrationTimeout"' in capsys.readouterr().out


def test_entrypoint_rejects_non_finite_migration_timeout(monkeypatch, capsys):
    monkeypatch.setenv("AI_STUDIO_MIGRATION_TIMEOUT", "nan")
    assert entrypoint.main() == 2
    assert '"error_type":"InvalidMigrationTimeout"' in capsys.readouterr().out


def test_entrypoint_reports_server_launch_failure(monkeypatch, capsys):
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(entrypoint.os, "execv", lambda *_args: (_ for _ in ()).throw(OSError("server unavailable")))
    assert entrypoint.main() == 127
    output = capsys.readouterr().out
    assert '"event":"application.launch.failed"' in output
    assert '"error_type":"UvicornLaunchError"' in output
