import json
import io

from scripts.validate_log_stream import main, validate_lines


def _entry(**overrides):
    payload = {
        "timestamp": "2026-10-17T00:00:00.000+00:00",
        "schema_version": 1,
        "level": "INFO",
        "service": "test-atelier",
        "environment": "test",
        "request_id": "request-1",
        "event": "request.completed",
    }
    payload.update(overrides)
    return payload


def test_validate_lines_accepts_api_worker_and_nginx_entries():
    lines = [
        json.dumps(_entry(service="test-atelier")),
        json.dumps(_entry(service="test-atelier-web", request_id="abc", status=200)),
    ]

    count, errors = validate_lines(lines)

    assert count == 2
    assert errors == []


def test_validate_lines_rejects_missing_envelope_field():
    payload = _entry()
    del payload["request_id"]

    count, errors = validate_lines([json.dumps(payload)])

    assert count == 1
    assert errors == ["line 1: missing fields request_id"]


def test_validate_lines_rejects_duplicate_json_keys():
    line = '{"timestamp":"2026-10-17T00:00:00+00:00","schema_version":1,"level":"INFO","service":"test-atelier","environment":"test","request_id":"r1","event":"ok","event":"prompt"}'

    count, errors = validate_lines([line])

    assert count == 1
    assert errors == ["line 1: duplicate JSON key event"]


def test_validate_lines_requires_environment_and_event_or_message():
    payload = _entry()
    del payload["environment"]
    del payload["event"]

    count, errors = validate_lines([json.dumps(payload)])

    assert count == 1
    assert errors == [
        "line 1: missing fields environment",
        "line 1: missing non-empty event or message",
    ]


def test_validate_lines_rejects_payload_fields_even_when_nested():
    count, errors = validate_lines([json.dumps(_entry(context={"prompt": "secret"}))])

    assert count == 1
    assert errors == ["line 1: forbidden field prompt"]


def test_validate_lines_rejects_prefixed_prompt_and_model_payload_keys():
    payload = _entry(model_prompt="sensitive requirement text")

    count, errors = validate_lines([json.dumps(payload)])

    assert count == 1
    assert errors == ["line 1: forbidden field model_prompt"]


def test_validate_lines_rejects_document_payload_fields():
    count, errors = validate_lines([json.dumps(_entry(document_text="private requirement"))])

    assert count == 1
    assert errors == ["line 1: forbidden field document_text"]


def test_validate_lines_rejects_connection_and_runtime_secret_fields():
    count, errors = validate_lines([json.dumps(_entry(database_url="sqlite:///private.db"))])

    assert count == 1
    assert errors == ["line 1: forbidden field database_url"]


def test_validate_lines_rejects_set_cookie_fields():
    count, errors = validate_lines([json.dumps(_entry(context={"set-cookie": "session=secret"}))])

    assert count == 1
    assert errors == [
        "line 1: forbidden field set-cookie",
        "line 1: possible credential in log text",
    ]


def test_validate_lines_rejects_credentials_in_text():
    count, errors = validate_lines([json.dumps(_entry(message="Authorization: Bearer abc.def"))])

    assert count == 1
    assert errors == ["line 1: possible credential in log text"]


def test_validate_lines_rejects_quoted_json_credentials_in_text():
    count, errors = validate_lines([json.dumps(_entry(message='provider response {"token":"session-secret"}'))])

    assert count == 1
    assert errors == ["line 1: possible credential in log text"]


def test_validate_lines_rejects_cookie_headers_in_text():
    count, errors = validate_lines([json.dumps(_entry(message="Cookie: session=signed-session"))])

    assert count == 1
    assert errors == ["line 1: possible credential in log text"]


def test_validate_lines_rejects_uppercase_environment_credentials():
    count, errors = validate_lines([json.dumps(_entry(message=(
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz AWS_SECRET_ACCESS_KEY=secret"
    )))])

    assert count == 1
    assert errors == ["line 1: possible credential in log text"]


def test_validate_lines_allows_formatter_redaction_placeholder():
    count, errors = validate_lines([json.dumps(_entry(message="password=[REDACTED] Cookie: [REDACTED]"))])

    assert count == 1
    assert errors == []


def test_validate_lines_rejects_common_cloud_tokens_and_jwt():
    count, errors = validate_lines([json.dumps(_entry(message=(
        "sk-proj-1234567890abcdef AKIAIOSFODNN7EXAMPLE ghp_123456789012345678901234567890 "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature123456"
    )))])

    assert count == 1
    assert errors == ["line 1: possible credential in log text"]


def test_validate_lines_rejects_invalid_timestamp_and_level():
    count, errors = validate_lines([json.dumps(_entry(timestamp="not-a-date", level="NOTICE"))])

    assert count == 1
    assert errors == [
        "line 1: timestamp must be an ISO-8601 string",
        "line 1: invalid level",
    ]


def test_validate_lines_requires_timestamp_timezone():
    count, errors = validate_lines([json.dumps(_entry(timestamp="2026-10-17T00:00:00"))])

    assert count == 1
    assert errors == ["line 1: timestamp must be an ISO-8601 string"]


def test_validate_lines_rejects_oversized_log_line():
    oversized = json.dumps(_entry(message="x" * (64 * 1024)))

    count, errors = validate_lines([oversized])

    assert count == 1
    assert errors == ["line 1: log line exceeds 65536 bytes"]


def test_validate_lines_rejects_excessive_nesting():
    value = "leaf"
    for _ in range(14):
        value = {"context": value}

    count, errors = validate_lines([json.dumps(_entry(context=value))])

    assert count == 1
    assert errors == ["line 1: forbidden field <nesting-depth>"]


def test_validate_lines_rejects_nonfinite_numeric_values():
    count, errors = validate_lines([json.dumps(_entry(duration_ms=float("nan")))])

    assert count == 1
    assert errors == ["line 1: non-finite numeric value"]


def test_main_prints_a_structured_validation_event(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main([]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["event"] == "log_stream.validated"
    assert output["schema_version"] == 1
    assert output["entry_count"] == 0
