"""Validate production configuration before importing the database worker."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .observability import deployment_release, sanitize_log_fields


def _log(event: str, level: str, **fields: object) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": 1,
        "level": level,
        "service": "test-atelier",
        "environment": os.getenv("AI_STUDIO_ENV", "production")[:32],
        "event": event,
        # Startup failures are not request-scoped; use the same sentinel as
        # the application formatter for schema consistency.
        "request_id": "-",
        **sanitize_log_fields(fields, allowed={
            "component", "error_type", "failure_count", "warning_count", "failed_checks", "release",
            "message", "worker_id",
        }),
    }
    release = deployment_release()
    if release:
        payload["release"] = release
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _preflight() -> bool:
    environment = os.getenv("AI_STUDIO_ENV", "development").strip().lower()
    required = os.getenv("AI_STUDIO_REQUIRE_PREFLIGHT", "0").lower() in {"1", "true", "yes", "on"}
    if environment != "production" and not required:
        return True
    try:
        from scripts.preflight_production import run_preflight
        checks = run_preflight(
            dict(os.environ), compose=False,
            require_admin_password=False,
            # The worker has no HTTP listener; Cookie and metrics-token
            # requirements belong to the API process only.
            require_http_security=False,
        )
    except Exception as exc:
        _log("worker.preflight.failed", "ERROR", error_type=type(exc).__name__, failure_count=1)
        return False
    failures = [check.name for check in checks if check.status == "fail"]
    if failures:
        _log("worker.preflight.failed", "ERROR", failure_count=len(failures), failed_checks=failures[:32])
        return False
    _log("worker.preflight.completed", "INFO", warning_count=sum(check.status == "warn" for check in checks))
    return True


def main() -> int:
    if not _preflight():
        return 78
    # Tell api.worker not to repeat a check after this wrapper has validated
    # the environment before importing SQLAlchemy and its numeric settings.
    os.environ["AI_STUDIO_PREFLIGHT_COMPLETED"] = "1"
    from . import worker
    worker.run()
    return 0


def run() -> int:
    """Convert wrapper-level import/runtime failures into one JSON event."""
    try:
        return main()
    except Exception as exc:
        # api.worker emits the structured startup event for failures after
        # import; this guard keeps the wrapper's preflight path deterministic.
        _log("worker.entrypoint.failed", "ERROR", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
