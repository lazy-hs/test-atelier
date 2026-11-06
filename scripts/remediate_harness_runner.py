#!/usr/bin/env python3
"""Clear one Host runner quarantine after an offline, backed-up operator review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_harness_project_storage import TASK_ID, _task_index, audit
from scripts.backup_data import _fsync_directory, is_link_like
from scripts.deployment_secret_file import database_url as configured_database_url
from scripts.migrate_legacy_harness_session import (
    _backup_database_matches, _checked_files, _db_task, _files_match_backup,
)


class RemediationError(ValueError):
    pass


def _index_and_replay(task_id: str, task_root: Path) -> tuple[dict, bytes]:
    index_path = task_root / f"{task_id}.json"
    replay_path = task_root / f"{task_id}.events.json"
    if is_link_like(index_path) or is_link_like(replay_path):
        raise RemediationError("Task index and replay must be regular private files")
    indexed, project_id, bound, unconfirmed = _task_index(index_path)
    if indexed != task_id or not unconfirmed:
        raise RemediationError("Task is not in runner quarantine")
    original = index_path.read_bytes()
    task = json.loads(original)
    if "events" in task:
        raise RemediationError("Upgrade the legacy task index through Host recovery first")
    if type(task.get("user_id")) is not int or task["user_id"] < 1:
        raise RemediationError("Task owner is invalid")
    status = task.get("status")
    if status not in {"completed", "failed", "cancelled"}:
        raise RemediationError("Task is not terminal")
    if replay_path.stat().st_size > 128 * 1024 * 1024:
        raise RemediationError("Task event replay exceeds the review limit")
    events = json.loads(replay_path.read_text(encoding="utf-8"))
    if (type(task.get("sequence")) is not int or not isinstance(events, list)
            or len(events) != task["sequence"] or not events):
        raise RemediationError("Task event replay and index sequence disagree")
    for sequence, event in enumerate(events, 1):
        if (not isinstance(event, dict) or type(event.get("sequence")) is not int
                or event["sequence"] != sequence
                or not isinstance(event.get("payload"), dict)
                or event["payload"].get("task_id") != task_id
                or type(event["payload"].get("sequence")) is not int
                or event["payload"]["sequence"] != sequence):
            raise RemediationError("Task event replay is invalid")
    if events[-1].get("event") != f"task.{status}":
        raise RemediationError("Task event replay is not terminal")
    return task, original


def remediate(task_id: str, database_url: str, task_root: Path, session_root: Path,
              archive: Path, *, apply: bool = False, runner_tree_verified: bool = False,
              restored_database_url: str | None = None) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise RemediationError("Invalid task ID")
    if apply and not runner_tree_verified:
        raise RemediationError("An operator must verify the old runner tree has stopped")
    if any(is_link_like(root) for root in (task_root, session_root)):
        raise RemediationError("Host storage roots must not be symlinks or junctions")
    task_root = task_root.resolve(strict=True)
    session_root = session_root.resolve(strict=True)
    if task_root == session_root or task_root in session_root.parents or session_root in task_root.parents:
        raise RemediationError("Host storage roots must be separate")
    list(_checked_files(task_root))
    list(_checked_files(session_root))
    owned = session_root / task_id
    if is_link_like(owned) or not owned.is_dir():
        raise RemediationError("A task-owned Session directory is required")
    if (task_root / f"{task_id}.deleted.json").exists():
        raise RemediationError("A pending task deletion cannot be remediated")
    task, original = _index_and_replay(task_id, task_root)
    business = _db_task(database_url, task_id)
    _indexed, project_id, bound, _unconfirmed = _task_index(task_root / f"{task_id}.json")
    if (business[:3] != (project_id, task["user_id"], task["status"])
            or business[3] - {f"ta-{task_id}"} != bound):
        raise RemediationError("Business task and Host ownership or Session bindings differ")
    report = audit(project_id, database_url, task_root, session_root)
    if (report["status"] != "inconsistent" or report["unconfirmed_runner_stops"] < 1
            or any(report[key] for key in (
                "host_only_tasks", "business_only_tasks", "incomplete_deletions", "missing_sessions",
                "shared_sessions", "missing_event_replays", "global_orphan_event_replays",
                "global_unreferenced_sessions",
            ))):
        raise RemediationError("Storage reconciliation has unrelated unresolved differences")
    manifest = _files_match_backup(archive, task_root, session_root)
    _backup_database_matches(archive, manifest, database_url, task_id, business, restored_database_url)
    result = {"task_id": task_id, "project_id": project_id,
              "status": "ready" if not apply else "cleared",
              "other_quarantined_tasks": report["unconfirmed_runner_stops"] - 1}
    if not apply:
        return result

    # Recheck the exact stopped snapshot and index immediately before replacement.
    _files_match_backup(archive, task_root, session_root)
    if _db_task(database_url, task_id) != business or _index_and_replay(task_id, task_root)[1] != original:
        raise RemediationError("Task or business row changed during review")
    updated = dict(task)
    updated["termination_unconfirmed"] = False
    updated["runner_remediation"] = {
        "method": "offline_verified_stop",
        "at": datetime.now(timezone.utc).isoformat(),
        "backup_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    descriptor, temporary = tempfile.mkstemp(prefix=f".{task_id}-remediation-", suffix=".tmp", dir=task_root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if (task_root / f"{task_id}.json").read_bytes() != original:
            raise RemediationError("Task index changed before replacement")
        os.replace(temporary, task_root / f"{task_id}.json")
        _fsync_directory(task_root)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    if sys.platform not in {"darwin", "win32", "linux"}:
        parser.error("Runner remediation is supported only on macOS, Windows and Linux")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--backup-archive", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or None)
    parser.add_argument("--services-stopped", action="store_true")
    parser.add_argument("--runner-tree-verified", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-restore-database-url", default=os.getenv("TEST_ATELIER_VERIFIED_RESTORE_DATABASE_URL"))
    args = parser.parse_args()
    if not args.services_stopped:
        parser.error("Stop all writers and pass --services-stopped before reviewing Host storage")
    if not args.database_url and os.getenv("AI_STUDIO_DATABASE_PASSWORD_FILE"):
        try:
            args.database_url = configured_database_url()
        except ValueError:
            print(json.dumps({"status": "error", "error_type": "InvalidDatabaseConfiguration"}), file=sys.stderr)
            return 1
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    try:
        result = remediate(args.task_id, args.database_url, args.task_root, args.session_root,
                           args.backup_archive, apply=args.apply,
                           runner_tree_verified=args.runner_tree_verified,
                           restored_database_url=args.verified_restore_database_url)
    except Exception as exc:
        # Database and filesystem errors may contain private locations or credentials.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
