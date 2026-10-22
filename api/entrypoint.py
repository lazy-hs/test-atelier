"""Run production migrations with structured startup output, then exec Uvicorn."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .observability import deployment_release, redact_sensitive_text, sanitize_log_fields


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _log(event: str, level: str, **fields: object) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": 1,
        "level": level,
        "service": "test-atelier",
        "environment": os.getenv("AI_STUDIO_ENV", "production")[:32],
        "event": event,
        # Startup events have no HTTP request, but keep the envelope aligned
        # with API and worker logs so collectors can rely on one schema.
        "request_id": "-",
        **sanitize_log_fields(fields, allowed={
            "component", "error_type", "failure_count", "warning_count", "failed_checks", "release",
            "revision", "duration_ms", "returncode", "message",
        }),
    }
    release = deployment_release()
    if release:
        payload["release"] = release
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _production_preflight() -> bool:
    """Reject an invalid production environment before migrations or Uvicorn.

    The normal local launcher stays lightweight, while production containers
    get the same bounded configuration checks as the operator-facing script.
    Only check names are emitted so a malformed secret can never be echoed in
    the startup log.
    """
    environment = os.getenv("AI_STUDIO_ENV", "development").strip().lower()
    required = os.getenv("AI_STUDIO_REQUIRE_PREFLIGHT", "0").lower() in {"1", "true", "yes", "on"}
    if environment != "production" and not required:
        return True
    try:
        from scripts.preflight_production import run_preflight
        checks = run_preflight(dict(os.environ), compose=False)
    except Exception as exc:
        _log("production.preflight.failed", "ERROR", error_type=type(exc).__name__, failure_count=1)
        return False
    failures = [check.name for check in checks if check.status == "fail"]
    if failures:
        _log("production.preflight.failed", "ERROR", failure_count=len(failures), failed_checks=failures[:32])
        return False
    _log("production.preflight.completed", "INFO", warning_count=sum(check.status == "warn" for check in checks))
    return True


def main() -> int:
    if not _production_preflight():
        return 78
    started = time.perf_counter()
    try:
        timeout = float(os.getenv("AI_STUDIO_MIGRATION_TIMEOUT", "120"))
        if not math.isfinite(timeout) or timeout < 1:
            raise ValueError
    except (TypeError, ValueError):
        _log("database.migration.failed", "ERROR", error_type="InvalidMigrationTimeout",
             revision=os.getenv("AI_STUDIO_MIGRATION_HEAD", "head"),
             duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return 2
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False, shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _log("database.migration.failed", "ERROR", error_type="MigrationTimeout",
             revision=os.getenv("AI_STUDIO_MIGRATION_HEAD", "head"),
             duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return 124
    except OSError as exc:
        _log("database.migration.failed", "ERROR", error_type="MigrationLaunchError",
             revision=os.getenv("AI_STUDIO_MIGRATION_HEAD", "head"),
             message=redact_sensitive_text(str(exc)),
             duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return 127
    if result.returncode:
        # Do not echo Alembic output: database URLs may contain credentials.
        _log("database.migration.failed", "ERROR", error_type="MigrationError",
             returncode=result.returncode,
             revision=os.getenv("AI_STUDIO_MIGRATION_HEAD", "head"),
             message=redact_sensitive_text(result.stderr or result.stdout or "迁移命令未返回诊断"),
             duration_ms=round((time.perf_counter() - started) * 1000, 2))
        return result.returncode
    _log("database.migration.completed", "INFO",
         revision=os.getenv("AI_STUDIO_MIGRATION_HEAD", "head"),
         duration_ms=round((time.perf_counter() - started) * 1000, 2))
    try:
        port = int(os.getenv("AI_STUDIO_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError
        os.execv(sys.executable, [sys.executable, "-m", "api.uvicorn_entrypoint",
                                  "--host", "0.0.0.0", "--port", str(port)])
    except (TypeError, ValueError):
        _log("application.launch.failed", "ERROR", component="uvicorn", error_type="InvalidPort")
        return 2
    except OSError as exc:
        _log("application.launch.failed", "ERROR", error_type="UvicornLaunchError",
             message=redact_sensitive_text(str(exc)))
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
