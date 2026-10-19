from pathlib import Path


RULES = Path(__file__).resolve().parents[2] / "ops" / "prometheus" / "test-atelier.rules.yml"


def test_critical_alerts_detect_missing_scrape_series():
    content = RULES.read_text(encoding="utf-8")
    assert 'absent(up{job="test-atelier-api"})' in content
    assert 'absent(probe_success{job="test-atelier-ready"})' in content
    assert "absent(test_atelier_worker_up)" in content


def test_alert_rules_keep_bounded_provider_and_queue_signals():
    content = RULES.read_text(encoding="utf-8")
    assert 'outcome="timeout"' in content
    assert "test_atelier_ai_jobs_active / test_atelier_ai_jobs_capacity" in content
    assert "test_atelier_worker_heartbeat_age_seconds > test_atelier_worker_heartbeat_threshold_seconds" in content


def test_alert_rules_cover_configured_cli_availability():
    content = RULES.read_text(encoding="utf-8")
    assert "TestAtelierCliUnavailable" in content
    assert 'test_atelier_cli_enabled{provider=~"codex_cli|claude_code"} == 1 and on(provider)' in content
    assert 'test_atelier_cli_available{provider=~"codex_cli|claude_code"} == 0' in content
