import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.preflight_production import HARNESS_SKILL_FILES, Check, check_compose_runtime, main, run_preflight


@pytest.mark.parametrize("invocation", [
    ["scripts/preflight_production.py"],
    ["-m", "scripts.preflight_production"],
])
def test_preflight_public_cli_from_repository_root(invocation):
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, *invocation, "--help"],
        cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--compose" in result.stdout


@pytest.fixture(autouse=True)
def patched_cryptography_baseline(monkeypatch):
    """Keep preflight tests independent of the host virtualenv version."""
    monkeypatch.setattr("scripts.preflight_production.importlib.metadata.version", lambda name: "50.0.2")


def valid_env(tmp_path: Path) -> dict[str, str]:
    skill_root = tmp_path / "qa-doc-skills"
    (skill_root / "skills" / "hub").mkdir(parents=True)
    (skill_root / "skills" / "requirement-analysis").mkdir()
    (skill_root / "skills" / "test-cases").mkdir()
    (skill_root / "AGENTS.md").write_text("# rules", encoding="utf-8")
    (skill_root / "skills" / "hub" / "SKILL.md").write_text("# hub", encoding="utf-8")
    (skill_root / "skills" / "requirement-analysis" / "SKILL.md").write_text("# analysis", encoding="utf-8")
    (skill_root / "skills" / "test-cases" / "SKILL.md").write_text("# cases", encoding="utf-8")
    for name in HARNESS_SKILL_FILES:
        path = skill_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# harness skill source", encoding="utf-8")
    return {
        "AI_STUDIO_SESSION_SECRET": "s" * 40,
        "AI_STUDIO_ADMIN_PASSWORD": "p" * 16,
        "POSTGRES_PASSWORD": "d" * 16,
        "DATABASE_URL": "postgresql+psycopg://app:secret@db:5432/atelier",
        "TEST_ATELIER_SKILL_ROOT": str(skill_root),
        "AI_STUDIO_DATA_DIR": str(tmp_path / "data"),
        "AI_STUDIO_EXTERNAL_WORKER": "1",
        "AI_STUDIO_CODEX_ENABLED": "0",
        "AI_STUDIO_CLAUDE_ENABLED": "0",
        "COOKIE_SECURE": "1",
        "METRICS_TOKEN": "metrics-secret-123456",
    }


def test_preflight_script_executes_jsonl_from_repository_root(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(valid_env(tmp_path))
    result = subprocess.run(
        [sys.executable, "scripts/preflight_production.py", "--jsonl"],
        cwd=root, env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    payload = json.loads(result.stdout)
    assert payload["event"].startswith("production.preflight.")
    assert payload["check_count"] > 0
    assert not result.stderr


def by_name(results):
    return {item.name: item for item in results}


def test_compose_runtime_reports_missing_docker(monkeypatch):
    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda _name: None)
    check = check_compose_runtime()
    assert check.name == "DOCKER_COMPOSE"
    assert check.status == "fail"
    assert "Docker CLI" in check.detail


def test_preflight_rejects_platforms_outside_supported_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.preflight_production.sys.platform", "freebsd")
    results = by_name(run_preflight(valid_env(tmp_path)))
    assert results["SUPPORTED_PLATFORM"].status == "fail"


def test_compose_runtime_uses_argv_without_shell(monkeypatch):
    calls = {}

    class Result:
        returncode = 0

    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda _name: "/usr/bin/docker")

    def run(args, **kwargs):
        calls.setdefault("calls", []).append((args, kwargs))
        return Result()

    monkeypatch.setattr("scripts.preflight_production.subprocess.run", run)
    check = check_compose_runtime()
    assert check.status == "ok"
    assert calls["calls"][0][0] == ["/usr/bin/docker", "compose", "version"]
    assert calls["calls"][0][1]["shell"] is False
    assert calls["calls"][0][1]["timeout"] == 5
    assert calls["calls"][1][0] == ["/usr/bin/docker", "compose", "config", "--quiet"]
    assert calls["calls"][1][1]["shell"] is False
    assert calls["calls"][1][1]["timeout"] == 10


def test_compose_runtime_rejects_invalid_interpolation_without_echoing_output(monkeypatch):
    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stderr = "POSTGRES_PASSWORD=should-not-be-echoed"

    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda _name: "/usr/bin/docker")
    results = iter((Result(0), Result(1)))
    monkeypatch.setattr("scripts.preflight_production.subprocess.run", lambda *_args, **_kwargs: next(results))
    check = check_compose_runtime()
    assert check.status == "fail"
    assert "配置无法解析" in check.detail
    assert "should-not-be-echoed" not in check.detail


def test_compose_runtime_reports_unavailable_plugin(monkeypatch):
    class Result:
        returncode = 1

    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("scripts.preflight_production.subprocess.run", lambda *_args, **_kwargs: Result())
    check = check_compose_runtime()
    assert check.status == "fail"
    assert "插件不可用" in check.detail


def test_compose_runtime_reports_timeout_without_echoing_command_output(monkeypatch):
    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda _name: "/usr/bin/docker")

    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["docker", "compose", "version"], 5,
                                        stderr="password=should-not-be-echoed")

    monkeypatch.setattr("scripts.preflight_production.subprocess.run", run)
    check = check_compose_runtime()
    assert check.status == "fail"
    assert "TimeoutExpired" in check.detail
    assert "should-not-be-echoed" not in check.detail


def test_preflight_accepts_compose_baseline(tmp_path):
    results = by_name(run_preflight(valid_env(tmp_path), compose=True))
    assert all(item.status != "fail" for item in results.values())
    assert results["TEST_ATELIER_SKILL_ROOT"].status == "ok"


def test_preflight_surfaces_legacy_intel_mac_cryptography_as_warning(tmp_path, monkeypatch):
    env = valid_env(tmp_path)
    env["AI_STUDIO_ENV"] = "development"
    monkeypatch.setattr("scripts.preflight_production.importlib.metadata.version", lambda name: "48.0.1")
    monkeypatch.setattr("scripts.preflight_production.sys.platform", "darwin")
    monkeypatch.setattr("scripts.preflight_production.platform.machine", lambda: "x86_64")

    results = by_name(run_preflight(env, compose=True))

    assert results["cryptography"].status == "warn"
    assert "Intel macOS" in results["cryptography"].detail


def test_preflight_rejects_legacy_intel_mac_cryptography_in_production(tmp_path, monkeypatch):
    env = valid_env(tmp_path)
    env["AI_STUDIO_ENV"] = "production"
    monkeypatch.setattr("scripts.preflight_production.importlib.metadata.version", lambda name: "48.0.1")
    monkeypatch.setattr("scripts.preflight_production.sys.platform", "darwin")
    monkeypatch.setattr("scripts.preflight_production.platform.machine", lambda: "x86_64")

    results = by_name(run_preflight(env, compose=True))

    assert results["cryptography"].status == "fail"


def test_preflight_accepts_patched_cryptography_baseline(tmp_path, monkeypatch):
    env = valid_env(tmp_path)
    monkeypatch.setattr("scripts.preflight_production.importlib.metadata.version", lambda name: "50.0.2")

    results = by_name(run_preflight(env, compose=True))

    assert results["cryptography"].status == "ok"


def test_preflight_warns_when_harness_is_not_configured(tmp_path):
    results = by_name(run_preflight(valid_env(tmp_path)))
    assert results["TEST_ATELIER_HARNESS_URL"].status == "warn"
    assert results["TEST_ATELIER_HARNESS_TOKEN"].status == "warn"
    assert "TEST_ATELIER_HARNESS_SKILL_ROOT" not in results


def test_preflight_fails_member_web_shell_without_pilot_or_restricted_profile(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"] = "1"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"].status == "fail"
    env["TEST_ATELIER_WEB_SHELL_PILOT"] = "1"
    assert by_name(run_preflight(env))["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"].status == "fail"


def test_preflight_accepts_member_web_shell_only_with_restricted_profile(tmp_path):
    env = valid_env(tmp_path)
    env.update(
        TEST_ATELIER_WEB_SHELL_PILOT="1",
        TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS="1",
        TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE="1",
    )
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"].status == "ok"
    assert results["TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE"].status == "ok"


def test_preflight_keeps_member_web_shell_out_of_production_until_proxy_review(tmp_path):
    env = valid_env(tmp_path)
    env.update(
        AI_STUDIO_ENV="production",
        TEST_ATELIER_WEB_SHELL_PILOT="1",
        TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS="1",
        TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE="1",
    )
    result = by_name(run_preflight(env))["TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS"]
    assert result.status == "fail"


@pytest.mark.parametrize("missing", HARNESS_SKILL_FILES)
def test_preflight_rejects_missing_enabled_harness_skill_source(tmp_path, missing):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_URL"] = "http://harness:8787"
    source = Path(env["TEST_ATELIER_SKILL_ROOT"]) / missing
    source.unlink()
    result = by_name(run_preflight(env))["TEST_ATELIER_HARNESS_SKILL_ROOT"]
    assert result.status == "fail"
    assert missing in result.detail
    env.pop("TEST_ATELIER_HARNESS_URL")
    assert "TEST_ATELIER_HARNESS_SKILL_ROOT" not in by_name(run_preflight(env))


def test_preflight_rejects_oversized_enabled_harness_reference(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_URL"] = "http://harness:8787"
    (Path(env["TEST_ATELIER_SKILL_ROOT"]) / "references/PRD-GENERATION-TEMPLATE.md").write_bytes(
        b"x" * (64 * 1024 + 1)
    )
    result = by_name(run_preflight(env))["TEST_ATELIER_HARNESS_SKILL_ROOT"]
    assert result.status == "fail"
    assert "PRD-GENERATION-TEMPLATE.md" in result.detail


def test_preflight_rejects_enabled_harness_reference_outside_skill_root(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_URL"] = "http://harness:8787"
    outside = tmp_path / "outside.md"
    outside.write_text("untrusted", encoding="utf-8")
    reference = Path(env["TEST_ATELIER_SKILL_ROOT"]) / "references/PRD-GENERATION-TEMPLATE.md"
    reference.unlink()
    try:
        reference.symlink_to(outside)
    except OSError:
        pytest.skip("this Windows host cannot create file symlinks")
    result = by_name(run_preflight(env))["TEST_ATELIER_HARNESS_SKILL_ROOT"]
    assert result.status == "fail"
    assert "PRD-GENERATION-TEMPLATE.md" in result.detail


def test_preflight_checks_harness_skills_in_harness_only_mode(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_ONLY"] = "1"
    assert by_name(run_preflight(env))["TEST_ATELIER_HARNESS_SKILL_ROOT"].status == "ok"


def test_preflight_rejects_relative_harness_skill_root(tmp_path, monkeypatch):
    env = valid_env(tmp_path)
    monkeypatch.chdir(tmp_path)
    env["TEST_ATELIER_SKILL_ROOT"] = "qa-doc-skills"
    env["TEST_ATELIER_HARNESS_URL"] = "http://harness:8787"
    result = by_name(run_preflight(env))["TEST_ATELIER_HARNESS_SKILL_ROOT"]
    assert result.status == "fail"
    assert "绝对路径" in result.detail


def test_preflight_rejects_harness_only_without_host(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_ONLY"] = "1"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_ONLY"].status == "ok"
    assert results["TEST_ATELIER_HARNESS_URL"].status == "fail"
    assert results["TEST_ATELIER_SCOPE_SECRET"].status == "fail"


def test_preflight_accepts_harness_only_with_host_and_scope(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_ONLY": "true",
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
    })
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_ONLY"].status == "ok"
    assert results["TEST_ATELIER_HARNESS_URL"].status == "ok"
    assert results["TEST_ATELIER_SCOPE_SECRET"].status == "ok"
    assert all(item.status != "fail" for item in results.values())


def test_preflight_accepts_scope_secret_file_and_rejects_conflicting_inline_value(tmp_path):
    env = valid_env(tmp_path)
    secret = tmp_path / "scope-key"
    secret.write_text("f" * 40 + "\n", encoding="utf-8")
    env.update(TEST_ATELIER_HARNESS_ONLY="1", TEST_ATELIER_HARNESS_URL="http://harness:8787",
               TEST_ATELIER_SCOPE_SECRET_FILE=str(secret))
    assert by_name(run_preflight(env))["TEST_ATELIER_SCOPE_SECRET"].status == "ok"
    env["TEST_ATELIER_SCOPE_SECRET"] = "s" * 40
    assert by_name(run_preflight(env))["TEST_ATELIER_SCOPE_SECRET"].status == "fail"


def test_preflight_accepts_file_backed_host_token_and_rejects_conflict(tmp_path):
    env = valid_env(tmp_path)
    secret = tmp_path / "host-token"
    secret.write_bytes(b"h" * 24 + b"\r\n")
    env["TEST_ATELIER_HARNESS_TOKEN_FILE"] = str(secret)
    assert by_name(run_preflight(env))["TEST_ATELIER_HARNESS_TOKEN"].status == "ok"
    env["TEST_ATELIER_HARNESS_TOKEN"] = "other-valid-host-token"
    assert by_name(run_preflight(env))["TEST_ATELIER_HARNESS_TOKEN_FILE"].status == "fail"
    env.pop("TEST_ATELIER_HARNESS_TOKEN")
    secret.write_text("short", encoding="utf-8")
    assert by_name(run_preflight(env))["TEST_ATELIER_HARNESS_TOKEN_FILE"].status == "fail"


def test_preflight_reads_session_and_admin_files_without_inline_values(tmp_path):
    env = valid_env(tmp_path)
    for name, content in (("AI_STUDIO_SESSION_SECRET", "s" * 40),
                          ("AI_STUDIO_ADMIN_PASSWORD", "p" * 16)):
        env.pop(name)
        secret_file = tmp_path / name
        secret_file.write_text(content + "\n", encoding="utf-8")
        env[f"{name}_FILE"] = str(secret_file)
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_SESSION_SECRET"].status == "ok"
    assert results["AI_STUDIO_ADMIN_PASSWORD"].status == "ok"
    env["AI_STUDIO_SESSION_SECRET"] = "s" * 40
    assert by_name(run_preflight(env))["AI_STUDIO_SESSION_SECRET"].status == "fail"


def test_preflight_accepts_database_password_file_and_rejects_inline_conflicts(tmp_path):
    env = valid_env(tmp_path)
    secret_file = tmp_path / "database-key"
    secret_file.write_text("database-secret-12345\n", encoding="utf-8")
    env.pop("POSTGRES_PASSWORD")
    env.pop("DATABASE_URL")
    env["AI_STUDIO_DATABASE_PASSWORD_FILE"] = str(secret_file)
    results = by_name(run_preflight(env, compose=True))
    assert results["POSTGRES_PASSWORD"].status == "ok"
    assert results["DATABASE_URL"].status == "ok"
    env["DATABASE_URL"] = "postgresql+psycopg://app:duplicate@db/db"
    assert by_name(run_preflight(env, compose=True))["DATABASE_URL"].status == "fail"


def test_base_compose_preflight_infers_container_database_and_worker(tmp_path):
    env = valid_env(tmp_path)
    env.pop("DATABASE_URL")
    env.pop("AI_STUDIO_EXTERNAL_WORKER")
    results = by_name(run_preflight(env, compose=True))
    assert results["POSTGRES_PASSWORD"].status == "ok"
    assert results["DATABASE_URL"].status == "ok"
    assert results["AI_STUDIO_EXTERNAL_WORKER"].status == "ok"
    env.pop("POSTGRES_PASSWORD")
    assert by_name(run_preflight(env, compose=True))["POSTGRES_PASSWORD"].status == "fail"


def test_preflight_rejects_harness_url_with_embedded_credentials(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_URL"] = "https://user:password@example.test/v1?token=secret"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_URL"].status == "fail"


def test_preflight_accepts_bounded_harness_config(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_HARNESS_TIMEOUT": "45",
        "TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES": str(8 * 1024 * 1024),
        "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES": str(4 * 1024 * 1024),
        "TEST_ATELIER_HARNESS_TOKEN": "scope-token-123456",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
    })
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_URL"].status == "ok"
    assert results["TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES"].status == "ok"
    assert results["TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES"].status == "ok"
    assert results["TEST_ATELIER_SCOPE_SECRET"].status == "ok"
    assert all(item.status != "fail" for item in results.values())


def test_preflight_rejects_production_harness_argv_mode(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "AI_STUDIO_ENV": "production",
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "HARNESS_DSH_INPUT_MODE": "argv",
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_DSH_INPUT_MODE_PRODUCTION"].status == "fail"


def test_preflight_accepts_production_harness_stdin_mode(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "AI_STUDIO_ENV": "production",
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "HARNESS_DSH_INPUT_MODE": "stdin",
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_DSH_INPUT_MODE_PRODUCTION"].status == "ok"


def test_preflight_rejects_enabled_harness_without_scope_secret(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_URL"] = "http://harness:8787"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_SCOPE_SECRET"].status == "fail"


def test_preflight_rejects_unbounded_harness_timeout(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_TIMEOUT"] = "301"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_TIMEOUT_LIMIT"].status == "fail"


def test_preflight_rejects_unbounded_harness_request_size(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES"] = str(33 * 1024 * 1024)
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES_LIMIT"].status == "fail"


def test_preflight_rejects_invalid_harness_chunk_budget(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_CHUNK_CHARS"] = "999"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_CHUNK_CHARS"].status == "fail"


def test_preflight_rejects_chunk_overlap_larger_than_half_window(tmp_path):
    env = valid_env(tmp_path)
    env["TEST_ATELIER_HARNESS_CHUNK_CHARS"] = "1000"
    env["TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS"] = "501"
    results = by_name(run_preflight(env))
    assert results["TEST_ATELIER_HARNESS_CHUNK_OVERLAP_LIMIT"].status == "fail"


def test_preflight_rejects_unbounded_harness_reduce_budget(tmp_path):
    env = valid_env(tmp_path)
    env["HARNESS_REDUCE_BATCH_CHARS"] = "256001"
    env["HARNESS_REDUCE_BATCH_SIZE"] = "129"
    results = by_name(run_preflight(env))
    assert results["HARNESS_REDUCE_BATCH_CHARS_LIMIT"].status == "fail"
    assert results["HARNESS_REDUCE_BATCH_SIZE_LIMIT"].status == "fail"


def test_preflight_rejects_single_result_reduce_batches(tmp_path):
    env = valid_env(tmp_path)
    env["HARNESS_REDUCE_BATCH_SIZE"] = "1"
    results = by_name(run_preflight(env))
    assert results["HARNESS_REDUCE_BATCH_SIZE"].status == "fail"


def test_preflight_rejects_unbounded_harness_output(tmp_path):
    env = valid_env(tmp_path)
    env["HARNESS_MAX_OUTPUT_BYTES"] = str(65 * 1024 * 1024)
    results = by_name(run_preflight(env))
    assert results["HARNESS_MAX_OUTPUT_BYTES_LIMIT"].status == "fail"


def test_preflight_rejects_unknown_harness_input_mode(tmp_path):
    env = valid_env(tmp_path)
    env["HARNESS_DSH_INPUT_MODE"] = "environment"
    results = by_name(run_preflight(env))
    assert results["HARNESS_DSH_INPUT_MODE"].status == "fail"


def test_preflight_rejects_headless_confirmation_transport(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "HARNESS_CONFIRMATION_TRANSPORT": "stdin-lines",
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_CONFIRMATION_TRANSPORT"].status == "fail"


def test_preflight_accepts_acp_confirmation_transport(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "AI_STUDIO_ENV": "production",
        "HARNESS_DSH_PROTOCOL": "acp",
        "HARNESS_DSH_INPUT_MODE": "argv",
        "HARNESS_CONFIRMATION_TRANSPORT": "none",
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_DSH_PROTOCOL"].status == "ok"
    assert results["HARNESS_DSH_INPUT_MODE_PRODUCTION"].status == "ok"
    assert results["HARNESS_CONFIRMATION_TRANSPORT"].status == "ok"


def test_preflight_rejects_headless_json_flag_in_acp_mode(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "HARNESS_DSH_PROTOCOL": "acp",
        "HARNESS_DSH_ARGS_JSON": '["dsh", "--profile", "test-atelier-acp", "--json"]',
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_DSH_ARGS_ACP"].status == "fail"


def test_preflight_rejects_stdin_confirmation_in_acp_mode(tmp_path):
    env = valid_env(tmp_path)
    env.update({
        "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
        "TEST_ATELIER_SCOPE_SECRET": "h" * 40,
        "HARNESS_DSH_PROTOCOL": "acp",
        "HARNESS_CONFIRMATION_TRANSPORT": "stdin-lines",
    })
    results = by_name(run_preflight(env))
    assert results["HARNESS_CONFIRMATION_TRANSPORT"].status == "fail"


def test_preflight_warns_when_production_release_is_missing(tmp_path):
    env = valid_env(tmp_path)
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_RELEASE"].status == "warn"


def test_preflight_rejects_an_unbounded_release_identifier(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_RELEASE"] = "r" * 129
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_RELEASE"].status == "fail"


def test_worker_preflight_does_not_require_admin_password(tmp_path):
    env = valid_env(tmp_path)
    env.pop("AI_STUDIO_ADMIN_PASSWORD")
    results = by_name(run_preflight(env, require_admin_password=False))
    assert results["AI_STUDIO_ADMIN_PASSWORD"].status == "warn"
    assert all(item.status != "fail" for item in results.values())


def test_preflight_rejects_sqlite_external_worker_and_missing_skill(tmp_path):
    env = valid_env(tmp_path)
    env["DATABASE_URL"] = "sqlite:///./ai_studio.db"
    env["TEST_ATELIER_SKILL_ROOT"] = str(tmp_path / "missing")
    results = by_name(run_preflight(env, compose=False))
    assert results["DATABASE_URL"].status == "fail"
    assert results["TEST_ATELIER_SKILL_ROOT"].status == "fail"


def test_preflight_rejects_compose_without_external_worker(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_EXTERNAL_WORKER"] = "0"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_EXTERNAL_WORKER"].status == "fail"


def test_preflight_reports_disabled_cli_without_requiring_binary(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CODEX_ENABLED"] = "0"
    results = by_name(run_preflight(env))
    assert results["Codex CLI"].status == "ok"
    assert results["Codex CLI"].detail == "未启用"


def test_preflight_uses_provider_default_cli_command(tmp_path, monkeypatch):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CODEX_ENABLED"] = "1"
    monkeypatch.setattr("scripts.preflight_production.shutil.which", lambda command: "/usr/local/bin/codex" if command == "codex" else None)
    results = by_name(run_preflight(env))
    assert results["Codex CLI"].status == "ok"


def test_preflight_rejects_non_structured_production_logs(tmp_path):
    env = valid_env(tmp_path)
    env["LOG_FORMAT"] = "text"
    results = by_name(run_preflight(env))
    assert results["LOG_FORMAT"].status == "fail"


def test_preflight_rejects_unknown_log_level(tmp_path):
    env = valid_env(tmp_path)
    env["LOG_LEVEL"] = "verbose"
    results = by_name(run_preflight(env))
    assert results["LOG_LEVEL"].status == "fail"


def test_preflight_rejects_documented_password_placeholder(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_ADMIN_PASSWORD"] = "replace-with-strong-admin-password"
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_ADMIN_PASSWORD"].status == "fail"


def test_preflight_rejects_invalid_migration_timeout(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_MIGRATION_TIMEOUT"] = "not-a-number"
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_MIGRATION_TIMEOUT"].status == "fail"


def test_preflight_rejects_unknown_migration_head(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_MIGRATION_HEAD"] = "missing-revision"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_MIGRATION_HEAD"].status == "fail"


def test_preflight_rejects_known_but_stale_migration_head(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_MIGRATION_HEAD"] = "20261039_execution_report_snapshots"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_MIGRATION_HEAD"].status == "fail"


def test_preflight_rejects_invalid_runtime_limits(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_MAX_REQUEST_BYTES"] = "not-a-number"
    env["AI_STUDIO_WORKER_LEASE_SECONDS"] = "10"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_MAX_REQUEST_BYTES"].status == "fail"
    assert results["AI_STUDIO_WORKER_LEASE_SECONDS"].status == "fail"


def test_preflight_rejects_non_finite_numeric_limits(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CLI_TIMEOUT"] = "inf"
    env["AI_STUDIO_MIGRATION_TIMEOUT"] = "nan"
    env["AI_STUDIO_WORKER_POLL_SECONDS"] = "Infinity"

    results = by_name(run_preflight(env, compose=True))

    assert results["AI_STUDIO_CLI_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_MIGRATION_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_WORKER_POLL_SECONDS"].status == "fail"


def test_preflight_rejects_invalid_database_and_login_limits(tmp_path):
    env = valid_env(tmp_path)
    env["DB_POOL_SIZE"] = "0"
    env["DB_MAX_OVERFLOW"] = "not-a-number"
    env["DB_POOL_TIMEOUT"] = "0"
    env["AI_STUDIO_SQLITE_TIMEOUT"] = "invalid"
    env["AI_STUDIO_LOGIN_FAILURE_LIMIT"] = "0"
    env["AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS"] = "0"
    env["AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS"] = "0"
    results = by_name(run_preflight(env, compose=True))
    assert results["DB_POOL_SIZE"].status == "fail"
    assert results["DB_MAX_OVERFLOW"].status == "fail"
    assert results["DB_POOL_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_SQLITE_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_LOGIN_FAILURE_LIMIT"].status == "fail"
    assert results["AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS"].status == "fail"
    assert results["AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS"].status == "fail"


def test_preflight_rejects_invalid_cli_timeouts(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CLI_TIMEOUT"] = "0"
    env["AI_STUDIO_CLI_TEST_TIMEOUT"] = "not-a-number"
    env["AI_STUDIO_SKILL_SCRIPT_TIMEOUT"] = "0"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_CLI_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_CLI_TEST_TIMEOUT"].status == "fail"
    assert results["AI_STUDIO_SKILL_SCRIPT_TIMEOUT"].status == "fail"


def test_preflight_rejects_connection_timeout_longer_than_task_timeout(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CLI_TIMEOUT"] = "30"
    env["AI_STUDIO_CLI_TEST_TIMEOUT"] = "45"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_CLI_TEST_TIMEOUT_ORDER"].status == "fail"


def test_preflight_accepts_connection_timeout_before_task_timeout(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CLI_TIMEOUT"] = "60"
    env["AI_STUDIO_CLI_TEST_TIMEOUT"] = "15"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_CLI_TEST_TIMEOUT_ORDER"].status == "ok"


def test_preflight_rejects_provider_timeout_longer_than_worker_lease(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_CLI_TIMEOUT"] = "901"
    env["AI_STUDIO_WORKER_LEASE_SECONDS"] = "900"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_CLI_TIMEOUT_ORDER"].status == "fail"


def test_preflight_rejects_skill_script_timeout_longer_than_worker_lease(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_SKILL_SCRIPT_TIMEOUT"] = "901"
    env["AI_STUDIO_WORKER_LEASE_SECONDS"] = "900"
    results = by_name(run_preflight(env, compose=True))
    assert results["AI_STUDIO_SKILL_SCRIPT_TIMEOUT_ORDER"].status == "fail"


def test_preflight_rejects_invalid_backup_timeout(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_BACKUP_TIMEOUT"] = "0"
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_BACKUP_TIMEOUT"].status == "fail"


def test_preflight_rejects_short_session_lifetime(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_SESSION_MAX_AGE"] = "60"
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_SESSION_MAX_AGE"].status == "fail"


def test_preflight_requires_secure_cookies_and_metrics_token_in_production(tmp_path):
    env = valid_env(tmp_path)
    env["COOKIE_SECURE"] = "0"
    env["METRICS_TOKEN"] = "short"
    results = by_name(run_preflight(env, compose=True))
    assert results["COOKIE_SECURE"].status == "fail"
    assert results["METRICS_TOKEN"].status == "fail"


def test_preflight_accepts_metrics_file_and_rejects_inline_conflict(tmp_path):
    env = valid_env(tmp_path)
    env.pop("METRICS_TOKEN")
    secret = tmp_path / "metrics-token"
    secret.write_bytes(b"metrics-file-token-123456\r\n")
    env["METRICS_TOKEN_FILE"] = str(secret)
    assert by_name(run_preflight(env, compose=True))["METRICS_TOKEN"].status == "ok"
    env["METRICS_TOKEN"] = "inline-metrics-token-123456"
    assert by_name(run_preflight(env, compose=True))["METRICS_TOKEN"].status == "fail"


def test_preflight_rejects_disabled_migrations_in_production(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_ENV"] = "production"
    env["AI_STUDIO_REQUIRE_MIGRATIONS"] = "0"
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_REQUIRE_MIGRATIONS"].status == "fail"


def test_development_preflight_keeps_insecure_cookie_warning(tmp_path):
    env = valid_env(tmp_path)
    env["COOKIE_SECURE"] = "0"
    env["METRICS_TOKEN"] = "short"
    results = by_name(run_preflight(env, compose=False))
    assert results["COOKIE_SECURE"].status == "warn"
    assert results["METRICS_TOKEN"].status == "warn"


def test_worker_preflight_does_not_require_http_only_secrets(tmp_path):
    env = valid_env(tmp_path)
    env["AI_STUDIO_ENV"] = "production"
    env.pop("COOKIE_SECURE")
    env.pop("METRICS_TOKEN")
    results = by_name(run_preflight(env, require_admin_password=False, require_http_security=False))
    assert results["COOKIE_SECURE"].status == "warn"
    assert results["METRICS_TOKEN"].status == "warn"
    assert all(item.status != "fail" for item in results.values())


def test_preflight_rejects_postgres_password_that_breaks_compose_url(tmp_path):
    env = valid_env(tmp_path)
    env["POSTGRES_PASSWORD"] = "unsafe@password!"
    results = by_name(run_preflight(env, compose=True))
    assert results["POSTGRES_PASSWORD"].status == "fail"
    assert "DATABASE_URL" in results["POSTGRES_PASSWORD"].detail


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions are not portable on Windows")
def test_preflight_rejects_symlinked_data_root(tmp_path):
    env = valid_env(tmp_path)
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "data-link"
    linked_data.symlink_to(real_data, target_is_directory=True)
    env["AI_STUDIO_DATA_DIR"] = str(linked_data)
    results = by_name(run_preflight(env))
    assert results["AI_STUDIO_DATA_DIR"].status == "fail"


def test_preflight_jsonl_emits_safe_log_contract(monkeypatch, capsys):
    monkeypatch.setattr("scripts.preflight_production.run_preflight", lambda **_kwargs: [
        Check("SESSION", "ok", "configured"),
        Check("DATA_DIR", "fail", "/private/user/path"),
        Check("RELEASE", "warn", "missing"),
    ])

    assert main(["--jsonl"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["event"] == "production.preflight.failed"
    assert payload["request_id"] == "-"
    assert payload["failed_checks"] == ["DATA_DIR"]
    assert payload["warning_count"] == 1
    assert payload["checks"] == [
        {"name": "SESSION", "status": "ok"},
        {"name": "DATA_DIR", "status": "fail"},
        {"name": "RELEASE", "status": "warn"},
    ]
    assert "/private/user/path" not in output


def test_preflight_jsonl_serializes_operator_cancellation(monkeypatch, capsys):
    monkeypatch.setattr("scripts.preflight_production.run_preflight", lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert main(["--jsonl"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "production.preflight.cancelled"
    assert payload["request_id"] == "-"
    assert payload["checks"] == []
