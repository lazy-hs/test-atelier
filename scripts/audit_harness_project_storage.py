#!/usr/bin/env python3
"""Read-only reconciliation of one project's database and Harness Host storage."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backup_data import is_link_like
from scripts.deployment_secret_file import database_url as configured_database_url


TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
SESSION_FILE = re.compile(r"session(?:\.v[1-9][0-9]*)?\.jsonl")
MAX_INDEX_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_FILES = 200_000


class AuditError(ValueError):
    pass


def _files(root: Path):
    if not root.is_dir() or is_link_like(root):
        raise AuditError("Storage root is missing or is a symlink or junction")
    scanned = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            scanned += 1
            if scanned > MAX_FILES:
                raise AuditError("Storage inventory exceeds the file limit")
            if is_link_like(Path(parent) / name):
                raise AuditError("Storage contains a symlink or junction; inventory is incomplete")
        for name in files:
            yield Path(parent) / name


def _header_id(path: Path) -> str:
    with path.open("rb") as handle:
        first = handle.readline(MAX_HEADER_BYTES + 1)
    if len(first) > MAX_HEADER_BYTES:
        raise AuditError("Session header exceeds the size limit")
    try:
        header = json.loads(first)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("Session header is invalid") from exc
    value = header.get("id") if isinstance(header, dict) and header.get("type") == "session" else None
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise AuditError("Session header has an invalid ID")
    return value


def _task_index(path: Path) -> tuple[str, int, set[str], bool]:
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise AuditError("Host task index exceeds the size limit")
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("Host task index is invalid") from exc
    task_id = path.stem
    if not isinstance(task, dict) or task.get("id") != task_id or not TASK_ID.fullmatch(task_id):
        raise AuditError("Host task index ID does not match its filename")
    project_id = task.get("project_id")
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 1:
        raise AuditError("Host task index has an invalid project ID")
    ids = task.get("session_ids", [])
    primary = task.get("session_id")
    if not isinstance(ids, list) or any(not isinstance(value, str) or not SESSION_ID.fullmatch(value) for value in ids):
        raise AuditError("Host task index has invalid Session IDs")
    if primary is not None and (not isinstance(primary, str) or not SESSION_ID.fullmatch(primary)):
        raise AuditError("Host task index has an invalid primary Session ID")
    unconfirmed = task.get("termination_unconfirmed", False)
    if type(unconfirmed) is not bool:
        raise AuditError("Host task index has an invalid runner termination state")
    values = {*ids, *([primary] if primary else [])}
    values.discard(f"ta-{task_id}")
    return task_id, project_id, values, unconfirmed


def audit(project_id: int, database_url: str, task_root: Path, session_root: Path) -> dict:
    if project_id < 1:
        raise AuditError("Project ID must be positive")
    if is_link_like(task_root) or is_link_like(session_root):
        raise AuditError("Storage root is a symlink or junction")
    task_root = task_root.resolve(strict=True)
    session_root = session_root.resolve(strict=True)
    if task_root == session_root or task_root in session_root.parents or session_root in task_root.parents:
        raise AuditError("Task and Session roots must be separate")

    host_tasks: dict[str, tuple[int, set[str]]] = {}
    tombstones: dict[str, int] = {}
    owners: dict[str, set[int]] = defaultdict(set)
    missing_replays = 0
    unconfirmed_stops = 0
    replay_ids: set[str] = set()
    for path in _files(task_root):
        if path.parent == task_root and path.name.endswith(".deleted.json"):
            if path.stat().st_size > MAX_INDEX_BYTES:
                raise AuditError("Host deletion marker exceeds the size limit")
            try:
                marker = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AuditError("Host deletion marker is invalid") from exc
            task_id = path.name.removesuffix(".deleted.json")
            if (not TASK_ID.fullmatch(task_id) or not isinstance(marker, dict) or marker.get("id") != task_id
                    or isinstance(marker.get("project_id"), bool) or not isinstance(marker.get("project_id"), int)
                    or marker["project_id"] < 1
                    or isinstance(marker.get("user_id"), bool) or not isinstance(marker.get("user_id"), int)
                    or marker["user_id"] < 1):
                raise AuditError("Host deletion marker has invalid ownership")
            tombstones[task_id] = marker["project_id"]
            continue
        if path.parent == task_root and path.name.endswith(".events.json"):
            replay_ids.add(path.name.removesuffix(".events.json"))
            continue
        if path.parent != task_root or path.suffix != ".json" or path.name.endswith(".events.json"):
            continue
        task_id, owner, session_ids, unconfirmed = _task_index(path)
        host_tasks[task_id] = (owner, session_ids)
        if owner == project_id and unconfirmed:
            unconfirmed_stops += 1
        for session_id in session_ids:
            owners[session_id].add(owner)
        if owner == project_id and not (task_root / f"{task_id}.events.json").is_file():
            missing_replays += 1

    session_paths: dict[str, set[Path]] = defaultdict(set)
    for path in _files(session_root):
        if SESSION_FILE.fullmatch(path.name):
            session_paths[_header_id(path)].add(path)
    present_sessions = set(session_paths)

    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database.startswith("file:"):
            raise AuditError("Audit requires a file-backed SQLite database")
        database_file = Path(url.database).resolve(strict=True)
        database_url = f"sqlite+pysqlite:///file:{database_file.as_posix()}?mode=ro&uri=true"
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if url.get_backend_name() == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            project_exists = connection.execute(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}).first() is not None
            business_ids = set(connection.execute(text(
                "SELECT id FROM harness_tasks WHERE project_id = :id"), {"id": project_id}).scalars())
    finally:
        engine.dispose()

    host_ids = {task_id for task_id, (owner, _) in host_tasks.items() if owner == project_id}
    incomplete_deletions = {task_id for task_id, owner in tombstones.items() if owner == project_id and (
        task_id in host_tasks or task_id in replay_ids or (session_root / task_id).exists()
    )}
    referenced = {session for task_id in host_ids for session in host_tasks[task_id][1]}
    shared = {session for session in referenced if len(owners[session]) > 1}
    session_tasks: dict[str, set[str]] = defaultdict(set)
    for task_id in host_ids:
        for session in host_tasks[task_id][1]:
            session_tasks[session].add(task_id)
    legacy_sessions = set()
    missing_owned_sessions = set()
    present_owned_sessions = set()
    for task_id in host_ids:
        for session in host_tasks[task_id][1]:
            paths = session_paths.get(session, ())
            owned = any(path.relative_to(session_root).parts[0] == task_id for path in paths)
            # Old Host versions had no task-owned root. A global replay is
            # usable only if the ID has one indexed task and no competing
            # task-owned directory; otherwise ownership cannot be proven.
            legacy = [path for path in paths if path.relative_to(session_root).parts[0] not in host_tasks]
            if legacy:
                legacy_sessions.add(session)
            if owned or (legacy and len(session_tasks[session]) == 1 and len(owners[session]) == 1
                         and not any(path.relative_to(session_root).parts[0] in host_tasks for path in paths)):
                present_owned_sessions.add(session)
            else:
                missing_owned_sessions.add(session)
    report = {
        "project_id": project_id,
        "project_exists": project_exists,
        "business_tasks": len(business_ids),
        "host_tasks": len(host_ids),
        "host_only_tasks": len(host_ids - business_ids),
        "deleted_task_markers": sum(owner == project_id for owner in tombstones.values()),
        "incomplete_deletions": len(incomplete_deletions),
        "business_only_tasks": len(business_ids - host_ids),
        "recorded_sessions": len(referenced),
        "present_sessions": len(present_owned_sessions),
        "legacy_layout_sessions": len(legacy_sessions),
        "missing_sessions": len(missing_owned_sessions),
        "shared_sessions": len(shared),
        "missing_event_replays": missing_replays,
        "unconfirmed_runner_stops": unconfirmed_stops,
        # The Host roots are dedicated to Test Atelier. Without a task index,
        # orphaned files cannot safely be assigned to one deleted project.
        "global_orphan_event_replays": len(replay_ids - host_tasks.keys()),
        "global_unreferenced_sessions": len(present_sessions - owners.keys()),
    }
    report["status"] = "inconsistent" if any(report[key] for key in (
        "host_only_tasks", "business_only_tasks", "incomplete_deletions", "missing_sessions", "shared_sessions", "missing_event_replays",
        "unconfirmed_runner_stops",
        "global_orphan_event_replays", "global_unreferenced_sessions",
    )) else "consistent"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or None)
    args = parser.parse_args()
    if not args.database_url and os.getenv("AI_STUDIO_DATABASE_PASSWORD_FILE"):
        try:
            args.database_url = configured_database_url()
        except ValueError:
            print(json.dumps({"status": "error", "error_type": "InvalidDatabaseConfiguration"}), file=sys.stderr)
            return 1
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required")
    try:
        report = audit(args.project_id, args.database_url, args.task_root, args.session_root)
    except Exception as exc:
        # Database errors can embed connection strings. Never print them.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "consistent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
