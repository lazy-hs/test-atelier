from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _alembic_head() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(line.split()[0] for line in result.stdout.splitlines() if "(head)" in line)


def test_compose_propagates_provider_and_script_timeouts_to_api_and_worker():
    """Production services read the shared JSON instead of interpolation."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "./config/runtime.json:/app/config/runtime.json:ro" in compose
    assert "AI_STUDIO_CLI_TIMEOUT: ${" not in compose
    assert "AI_STUDIO_SKILL_SCRIPT_TIMEOUT: ${" not in compose


def test_compose_propagates_harness_bridge_limits_to_api():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "TEST_ATELIER_HARNESS_ONLY: ${" not in compose
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  harness-projector:\n", 1)[0]
    assert "./config/runtime.json:/app/config/runtime.json:ro" in worker
    assert "http://api:8000/api/v1/health/worker" in worker
    assert "TEST_ATELIER_HARNESS_TIMEOUT: ${" not in compose
    assert "TEST_ATELIER_HARNESS_MAX_RESPONSE_BYTES: ${" not in compose


def test_compose_projector_uses_current_migration_head_and_is_in_web_topology():
    """The durable projector must boot against the same schema as API/worker."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    projector = compose.split("\n  harness-projector:\n", 1)[1].split("\n  web:\n", 1)[0]
    assert "TEST_ATELIER_HARNESS_PROJECTOR_ENABLED: ${" not in projector
    assert "runtime.json:/app/config/runtime.json:ro" in projector
    web = compose.split("\n  web:\n", 1)[1]
    assert "      harness-projector:" in web
    assert "condition: service_healthy" in web


def test_runtime_migration_defaults_match_alembic_head():
    """Prevent a future migration from silently leaving launchers on a stale head."""
    head = _alembic_head()
    targets = [ROOT / "config/runtime.example.json"]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert head in text, f"{path} does not reference current Alembic head {head}"
    defaults = re.findall(r'AI_STUDIO_MIGRATION_HEAD[^\n]*202\d+_[A-Za-z0-9_]+', (ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert head in (ROOT / "config/runtime.example.json").read_text(encoding="utf-8")


def test_ci_worker_and_restored_database_expect_current_migration_head():
    head = _alembic_head()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--require-worker --require-web --expected-harness-mode off" in workflow
    assert f"export AI_STUDIO_MIGRATION_HEAD={head}" in workflow
    assert f"| grep -qx '{head}'" in workflow
    assert "docker compose exec -T api python -m scripts.verify_document_references_pg" in workflow
    assert "docker compose exec -T api python -m scripts.verify_artifact_directory_pg" in workflow


def test_harness_overlay_keeps_host_in_the_extension_compose_contract():
    overlay = (ROOT / "compose.harness-overlay.yaml").read_text(encoding="utf-8")
    assert overlay.count("- harness_scope_secret") == 2
    assert overlay.count("- harness_host_api_token") == 2
    assert "file: ./config/secrets/harness_scope_secret" in overlay
    assert "${" not in overlay


def test_combined_compose_defaults_to_interactive_sdk_acp():
    overlay = (ROOT / "compose.harness-overlay.yaml").read_text(encoding="utf-8")
    assert "HARNESS_DSH_PROTOCOL: ${" not in overlay
    host_path = ROOT.parent / "test-atelier-harness-extensions" / "deploy" / "compose.harness.example.yaml"
    if not host_path.is_file():
        return  # CI checks this independent checkout in its own repository.
    host = host_path.read_text(encoding="utf-8")
    assert "HARNESS_DSH_PROTOCOL: ${" not in host


def test_compose_runs_web_edge_without_root_capabilities():
    """The public edge must not require root or a privileged port in Docker."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    web_block = compose.split("\n  web:\n", 1)[1]
    assert "cap_drop:" in web_block
    assert "      - ALL" in web_block
    assert "127.0.0.1:8080:8080" in web_block
    assert "http://127.0.0.1:8080/" in web_block
    assert "stop_grace_period: 30s" in web_block


def test_production_compose_has_no_operator_environment_interpolation():
    for path in (ROOT / "compose.yaml", ROOT / "compose.harness-overlay.yaml",
                 ROOT.parent / "test-atelier-harness-extensions/deploy/compose.harness.example.yaml"):
        if path.is_file():
            assert "${" not in path.read_text(encoding="utf-8"), path

