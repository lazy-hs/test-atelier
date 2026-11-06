from pathlib import Path
import json
import re

import pytest

from scripts.harness_browser_smoke import (
    browser_smoke_environment, is_loopback_mock_model, real_dsh_args, real_smoke_host_environment,
)


ROOT = Path(__file__).resolve().parents[2]


def test_browser_smoke_children_only_inherit_necessary_controls():
    inherited = {
        "PATH": "/usr/bin", "HOME": "/tmp/home", "SystemRoot": "C:\\Windows",
        "PLAYWRIGHT_BROWSERS_PATH": "/tmp/browsers",
        "TA_LOCAL_SMOKE_CHECKPOINT": "1", "HARNESS_ANALYSIS_CHECKPOINTS": "1",
        "TA_LOCAL_SMOKE_CASE_EXCEPTION": "1", "HARNESS_CASE_CHECKPOINTS": "1",
        "TEST_ATELIER_HARNESS_CHUNK_CHARS": "1000",
        "TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS": "50",
        "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES": "65536",
        "OPENAI_API_KEY": "real-key", "ANTHROPIC_API_KEY": "real-key",
        "NODE_OPTIONS": "--require /tmp/evil.js", "PYTHONPATH": "/tmp/evil",
        "DATABASE_URL": "postgresql://secret", "AI_STUDIO_ADMIN_PASSWORD": "secret",
        "TEST_ATELIER_HARNESS_TOKEN": "secret", "AI_STUDIO_E2E_URL": "https://unrelated",
    }
    result = browser_smoke_environment(inherited)
    assert result == {key: inherited[key] for key in (
        "PATH", "HOME", "SystemRoot", "PLAYWRIGHT_BROWSERS_PATH",
        "TA_LOCAL_SMOKE_CHECKPOINT", "HARNESS_ANALYSIS_CHECKPOINTS",
        "TA_LOCAL_SMOKE_CASE_EXCEPTION", "HARNESS_CASE_CHECKPOINTS",
        "TEST_ATELIER_HARNESS_CHUNK_CHARS", "TEST_ATELIER_HARNESS_CHUNK_OVERLAP_CHARS",
        "TEST_ATELIER_HARNESS_MAX_REQUEST_BYTES",
    )}


@pytest.mark.parametrize("base,accepted", [
    ("http://127.0.0.1:8000/v1", True),
    ("http://127.0.0.1:8000@external.example/v1", False),
    ("http://127.0.0.1:bad/v1", False),
    ("http://localhost:8000/v1", False),
    ("https://127.0.0.1:8000/v1", False),
])
def test_browser_smoke_real_dsh_requires_loopback_mock_model(base, accepted):
    assert is_loopback_mock_model({"OPENAI_API_KEY": "local-mock-model-only", "OPENAI_API_BASE": base}) is accepted
    assert not is_loopback_mock_model({"OPENAI_API_KEY": "production-key", "OPENAI_API_BASE": base})


def test_real_smoke_host_receives_only_selected_model_and_ephemeral_credentials(tmp_path):
    source = {
        "PATH": "/usr/bin", "OPENAI_API_KEY": "selected-key", "OPENAI_API_BASE": "http://model/v1",
        "ANTHROPIC_API_KEY": "unrelated-key", "AZURE_OPENAI_API_KEY": "unrelated-key",
        "TEST_ATELIER_SCOPE_SECRET": "caller-scope", "TEST_ATELIER_HARNESS_TOKEN_FILE": "/other/token",
        "AI_STUDIO_ADMIN_PASSWORD": "caller-password", "NODE_OPTIONS": "--require /tmp/preload.js",
        "PYTHONPATH": "/tmp/untrusted", "DATABASE_URL": "postgresql://sensitive",
        "TEST_ATELIER_SKILL_MEMORY_ROOT": str(tmp_path / "memory"),
        "HARNESS_CASE_CHECKPOINTS": "1", "TA_LOCAL_SMOKE_STAGED_CASES": "1",
    }
    result = real_smoke_host_environment(source, tmp_path / "token")
    assert result == {
        "PATH": "/usr/bin", "OPENAI_API_KEY": "selected-key",
        "TEST_ATELIER_HARNESS_TOKEN": "", "TEST_ATELIER_HARNESS_TOKEN_FILE": str(tmp_path / "token"),
        "TEST_ATELIER_SKILL_MEMORY_ROOT": str(tmp_path / "memory"),
        "HARNESS_CASE_CHECKPOINTS": "1", "TA_LOCAL_SMOKE_STAGED_CASES": "1",
    }
    source_code = (ROOT / "scripts" / "harness_real_smoke.py").read_text(encoding="utf-8")
    assert "real_smoke_host_environment(os.environ, token_file)" in source_code
    assert "host_env = os.environ.copy()" not in source_code


def test_real_harness_smoke_uses_cross_platform_profile_installer():
    source = (ROOT / "scripts" / "harness_real_smoke.py").read_text(encoding="utf-8")
    assert "scripts/install_profile.py" in source
    assert '["bash", str(EXTENSIONS / "scripts/install_profile.sh")]' not in source


def test_real_harness_smoke_keeps_model_credential_out_of_persistent_paths():
    source = (ROOT / "scripts" / "harness_real_smoke.py").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY must be set" in source
    assert 'HARNESS_SESSION_ROOT=str(temp / "sessions")' in source
    assert 'host_log = temp / "host.log"' in source
    assert 'token_file = temp / "host-api-token"' in source
    assert 'os.environ["TEST_ATELIER_HARNESS_TOKEN_FILE"] = str(token_file)' in source


def test_fake_browser_dsh_binds_a_real_session_instead_of_the_allocation_placeholder():
    source = (ROOT / "scripts" / "harness_browser_smoke.py").read_text(encoding="utf-8")
    assert "const sessionId = `dsh-${taskId}`" in source
    assert "console.log(JSON.stringify({type:'session', sessionId}))" in source
    assert '"TEST_ATELIER_HARNESS_TOKEN_FILE": str(host_token_file)' in source


def test_local_model_browser_runner_accepts_the_current_recovery_marker():
    browser = (ROOT / "scripts" / "harness_browser_smoke.py").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "harness_local_model_smoke.mjs").read_text(encoding="utf-8")
    marker = re.search(r'print\("(Stopped backup/restore smoke: [^"\n]+)"\)', browser)
    assert marker, "Browser recovery smoke must emit its completion marker"
    assert f"? '{marker.group(1)}'" in runner


def test_local_model_generic_mode_checks_all_project_skills_and_sessions():
    runner = (ROOT / "scripts" / "harness_local_model_smoke.mjs").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "harness_real_smoke.py").read_text(encoding="utf-8")
    assert "--all-generic" in runner and "TA_LOCAL_SMOKE_ALL_GENERIC" in runner
    assert "verify_session_replay(client, project_root, task_id)" in smoke
    for skill_id in ("api-parsing", "dev-doc-analysis", "figma-to-prd"):
        assert skill_id in runner and skill_id in smoke


def test_local_model_browser_runner_checks_every_project_skill_in_the_browser_suite():
    browser = (ROOT / "scripts" / "harness_browser_smoke.py").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "harness_local_model_smoke.mjs").read_text(encoding="utf-8")
    assert '"harness.spec.ts", "historical-boundary.spec.ts", "skills-concurrency.spec.ts", "smoke.spec.ts"' in browser
    assert "allGeneric || browser" in runner
    assert "[...genericSkills, 'requirement-analysis', 'test-cases'].sort()" in runner


def test_browser_smoke_selects_the_profile_and_arguments_for_each_transport(tmp_path):
    checkout = tmp_path / "harness"
    (checkout / "apps/cli/src").mkdir(parents=True)
    (checkout / "apps/cli/src/bin.ts").write_text("", encoding="utf-8")
    (checkout / "package.json").write_text(json.dumps({"scripts": {
        "dsh": "node --import tsx/esm apps/cli/src/bin.ts",
    }}), encoding="utf-8")
    headless = real_dsh_args(checkout, "headless")
    acp = real_dsh_args(checkout, "acp")
    assert headless[-3:] == ["--profile", "test-atelier", "--json"]
    assert acp[-2:] == ["--profile", "test-atelier-acp"]
    assert real_dsh_args(checkout, "headless", platform="win32") == [
        "--import", "tsx/esm", "apps/cli/src/bin.ts", "--profile", "test-atelier", "--json",
    ]
    (checkout / "apps/cli/src/renamed.ts").write_text("", encoding="utf-8")
    (checkout / "package.json").write_text(json.dumps({"scripts": {
        "dsh": "node --import tsx/esm apps/cli/src/renamed.ts",
    }}), encoding="utf-8")
    assert real_dsh_args(checkout, "acp", platform="win32")[2] == "apps/cli/src/renamed.ts"
    (checkout / "package.json").write_text(json.dumps({"scripts": {
        "dsh": "node --import tsx/esm apps/cli/../../outside.ts",
    }}), encoding="utf-8")
    with pytest.raises(ValueError, match="compatibility review"):
        real_dsh_args(checkout, "acp", platform="win32")
    with pytest.raises(ValueError):
        real_dsh_args(checkout, "unknown")
