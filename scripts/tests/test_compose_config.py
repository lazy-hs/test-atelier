from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_propagates_provider_and_script_timeouts_to_api_and_worker():
    """Keep API/worker timeout budgets aligned in the production topology."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count("AI_STUDIO_CLI_TIMEOUT: ${AI_STUDIO_CLI_TIMEOUT:-600}") == 2
    assert compose.count("AI_STUDIO_SKILL_SCRIPT_TIMEOUT: ${AI_STUDIO_SKILL_SCRIPT_TIMEOUT:-120}") == 2
    # Connection tests are handled by the API settings endpoint; the worker
    # only needs the full task timeout and skill-script cancellation budget.
    assert sum(line.strip().startswith("AI_STUDIO_CLI_TEST_TIMEOUT:") for line in compose.splitlines()) == 1


def test_compose_runs_web_edge_without_root_capabilities():
    """The public edge must not require root or a privileged port in Docker."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    web_block = compose.split("\n  web:\n", 1)[1]
    assert "cap_drop:" in web_block
    assert "      - ALL" in web_block
    assert "127.0.0.1:8080:8080" in web_block
    assert "http://127.0.0.1:8080/" in web_block
    assert "stop_grace_period: 30s" in web_block

