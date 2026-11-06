from pathlib import Path

import pytest

from scripts import harness_staged_transport_smoke
from scripts.harness_staged_transport_smoke import run


@pytest.mark.parametrize("size", [9, 100 * 1024 * 1024 + 10])
def test_disposable_staged_transport_requires_a_bounded_exact_size(size):
    with pytest.raises(ValueError, match="multiple of 10"):
        run(size)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "test-atelier-harness-extensions" / "host" / "server.mjs").is_file(),
    reason="independent Extension checkout is not installed on this runner",
)
def test_disposable_host_uploads_and_removes_json_escaped_input(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-only-model-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-parent-only-anthropic-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-parent-only-google-key")
    monkeypatch.setenv("NODE_OPTIONS", "--require=synthetic-parent-preload")
    original = harness_staged_transport_smoke.spawn_isolated

    def isolated(command, **kwargs):
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "NODE_OPTIONS"):
            assert name not in kwargs["env"]
        assert kwargs["env"]["HARNESS_DSH_PROTOCOL"] == "headless"
        return original(command, **kwargs)

    monkeypatch.setattr(harness_staged_transport_smoke, "spawn_isolated", isolated)
    result = run(655360, control=True)
    assert result["bytes"] == 655360
    assert result["chunks"] > 0


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "test-atelier-harness-extensions" / "host" / "server.mjs").is_file(),
    reason="independent Extension checkout is not installed on this runner",
)
@pytest.mark.parametrize("control", [False, True])
def test_disposable_host_rejects_modified_staged_input_before_runner_start(control):
    result = run(655360, control=control, tamper=True)
    assert result["bytes"] == 655360
    assert result["chunks"] > 0
