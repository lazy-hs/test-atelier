#!/usr/bin/env python3
"""Validate the JSON log contract emitted by API, worker and Nginx.

The command is intentionally dependency-free so it can run in CI or on an
operator workstation against ``docker compose logs`` without importing the
application. It validates the envelope and rejects common payload/credential
fields before logs are shipped to a collector.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_FIELDS = ("timestamp", "schema_version", "level", "service", "environment", "request_id")
FORBIDDEN_KEYS = {
    "body", "request_body", "response_body", "prompt", "prompts", "model_response",
    "model_prompt", "prompt_text", "raw_prompt", "raw_response", "model_output", "response_text",
    "content", "document", "document_text", "file_name", "filename", "rows", "result", "output",
    "authorization", "cookie", "set_cookie", "set-cookie", "password", "secret", "api_key", "access_token",
    "refresh_token", "client_secret", "database_url", "session_secret", "metrics_token",
    "remote_api_key", "private_key", "token",
}
SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|\\?[\"']?(?:authorization|cookie|set[-_ ]?cookie|password|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|secret|token)\\?[\"']?\s*[:=]\s*\\?[\"']?[^\\\"'\s,;&}]+\\?[\"']?|"
    r"(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AZURE_OPENAI_API_KEY|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|"
    r"GOOGLE_API_KEY|HF_TOKEN|GITHUB_TOKEN|NPM_TOKEN|DOCKER_AUTH_CONFIG|DATABASE_URL|POSTGRES_PASSWORD|METRICS_TOKEN|"
    r"AI_STUDIO_SESSION_SECRET|AI_STUDIO_ADMIN_PASSWORD)\s*=\s*[^\s,;&}]+|"
    r"(?:postgres(?:ql)?(?:\+\w+)?|mysql(?:\+\w+)?|redis(?:s)?|mongodb(?:\+srv)?)://[^\s/@]+:[^\s@]+@|"
    r"sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----)"
)
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
MAX_LOG_LINE_BYTES = 64 * 1024
MAX_NESTING_DEPTH = 12


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key and could hide a payload."""


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(str(key))
        value[key] = item
    return value


def _has_unredacted_secret(text: str) -> bool:
    """Detect credentials while allowing the formatter's safe placeholder."""
    return any("[REDACTED]" not in match.group(0).upper() for match in SECRET_PATTERN.finditer(text))


def _contains_forbidden_key(value: object, depth: int = 0) -> str | None:
    if depth > MAX_NESTING_DEPTH:
        return "<nesting-depth>"
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                return str(key)
            found = _contains_forbidden_key(child, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_forbidden_key(child, depth + 1)
            if found:
                return found
    return None


def _contains_nonfinite_number(value: object, depth: int = 0) -> bool:
    """Reject JSON numbers that cannot be represented by strict collectors."""
    if depth > MAX_NESTING_DEPTH:
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite_number(child, depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(_contains_nonfinite_number(child, depth + 1) for child in value)
    return False


def validate_lines(lines) -> tuple[int, list[str]]:
    count = 0
    errors: list[str] = []
    for line_number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        count += 1
        if len(raw.encode("utf-8", errors="replace")) > MAX_LOG_LINE_BYTES:
            errors.append(f"line {line_number}: log line exceeds {MAX_LOG_LINE_BYTES} bytes")
            continue
        try:
            payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except DuplicateKeyError as exc:
            errors.append(f"line {line_number}: duplicate JSON key {exc}")
            continue
        except json.JSONDecodeError:
            errors.append(f"line {line_number}: invalid JSON")
            continue
        if not isinstance(payload, dict):
            errors.append(f"line {line_number}: log entry must be an object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in payload]
        if missing:
            errors.append(f"line {line_number}: missing fields {','.join(missing)}")
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
            errors.append(f"line {line_number}: schema_version must be 1")
        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, str):
            errors.append(f"line {line_number}: timestamp must be an ISO-8601 string")
        else:
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed_timestamp.tzinfo is None:
                    raise ValueError
            except ValueError:
                errors.append(f"line {line_number}: timestamp must be an ISO-8601 string")
        if payload.get("level") not in VALID_LEVELS:
            errors.append(f"line {line_number}: invalid level")
        for field in ("service", "environment", "request_id"):
            if field in payload and (not isinstance(payload[field], str) or not payload[field]):
                errors.append(f"line {line_number}: {field} must be a non-empty string")
        has_event = isinstance(payload.get("event"), str) and bool(payload["event"])
        has_message = isinstance(payload.get("message"), str) and bool(payload["message"])
        if not (has_event or has_message):
            errors.append(f"line {line_number}: missing non-empty event or message")
        forbidden = _contains_forbidden_key(payload)
        if forbidden:
            errors.append(f"line {line_number}: forbidden field {forbidden}")
        if _contains_nonfinite_number(payload):
            errors.append(f"line {line_number}: non-finite numeric value")
        if _has_unredacted_secret(text):
            errors.append(f"line {line_number}: possible credential in log text")
    return count, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="JSONL file; omit to read stdin")
    args = parser.parse_args(argv)
    stream = args.path.open(encoding="utf-8") if args.path else sys.stdin
    try:
        count, errors = validate_lines(stream)
    finally:
        if args.path:
            stream.close()
    if errors:
        for error in errors[:32]:
            print(error, file=sys.stderr)
        if len(errors) > 32:
            print(f"... and {len(errors) - 32} more errors", file=sys.stderr)
        return 1
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": 1, "level": "INFO", "service": "test-atelier",
        "environment": "validation", "event": "log_stream.validated", "request_id": "-",
        "entry_count": count,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
