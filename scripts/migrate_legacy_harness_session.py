#!/usr/bin/env python3
"""Copy a uniquely owned legacy Session directory into a task-private root.

Run only with all API, worker, Projector and Host writers stopped. The source
directory remains untouched; never infer ownership from its name alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_harness_project_storage import (_files, _header_id, _task_index,
                                                   audit, SESSION_FILE, TASK_ID)
from scripts.backup_data import _fsync_directory, is_link_like, verify_archive
from scripts.deployment_secret_file import database_url as configured_database_url


class MigrationError(ValueError):
    pass


def _checked_files(root: Path):
    try:
        max_session_bytes = max(64 * 1024, min(32 * 1024 * 1024,
                                             int(os.getenv("HARNESS_SESSION_MAX_BYTES", 8 * 1024 * 1024))))
    except ValueError:
        max_session_bytes = 8 * 1024 * 1024
    for path in _files(root):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MigrationError("Host storage contains a special file or hard link")
        if SESSION_FILE.fullmatch(path.name) and info.st_size > max_session_bytes:
            raise MigrationError("Session exceeds the Host replay size limit")
        yield path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _db_task(database_url: str, task_id: str) -> tuple[int, int, str, set[str]]:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database.startswith("file:"):
            raise MigrationError("Migration requires a file-backed SQLite database")
        path = Path(url.database).resolve(strict=True)
        database_url = f"sqlite+pysqlite:///file:{path.as_posix()}?mode=ro&uri=true"
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if url.get_backend_name() == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            row = connection.execute(text(
                "SELECT project_id, user_id, status, session_id, session_ids "
                "FROM harness_tasks WHERE id = :task_id"), {"task_id": task_id}).first()
            if row is None:
                raise MigrationError("Task is missing from the business database")
            project_id, user_id, status, primary, ids = row
            if isinstance(ids, str):
                ids = json.loads(ids)
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                raise MigrationError("Business Session bindings are invalid")
            return project_id, user_id, status, set(ids) | ({primary} if primary else set())
    finally:
        engine.dispose()


def _files_match_backup(archive: Path, task_root: Path, session_root: Path,
                        *, staging: Path | None = None, private_copy: Path | None = None,
                        legacy_source: Path | None = None) -> dict:
    manifest = verify_archive(archive)
    if manifest.get("harness_storage") != "task-session-v1":
        raise MigrationError("A verified backup containing both Harness roots is required")
    actual = {}
    for label, root in (("tasks", task_root), ("sessions", session_root)):
        for path in _checked_files(root):
            if any(root_path is not None and root_path in path.parents for root_path in (staging, private_copy)):
                continue
            key = f"harness/{label}/{path.relative_to(root).as_posix()}"
            actual[key] = (path.stat().st_size, _digest(path))
    archived = {item["path"]: (item["size"], item["sha256"])
                for item in manifest["files"] if item["path"].startswith("harness/")}
    if private_copy is not None:
        if legacy_source is None:
            raise MigrationError("Missing legacy source for private copy verification")
        source_prefix = f"harness/sessions/{legacy_source.name}/"
        private_prefix = f"harness/sessions/{private_copy.name}/"
        original = {key: value for key, value in archived.items() if key.startswith(source_prefix)}
        if not original or any(key.startswith(private_prefix) for key in archived):
            raise MigrationError("Backup does not describe an unmigrated legacy source")
        copied = {f"{source_prefix}{path.relative_to(private_copy).as_posix()}":
                  (path.stat().st_size, _digest(path)) for path in _checked_files(private_copy)}
        if copied != original:
            raise MigrationError("Private Session copy does not match the verified backup")
        missing = set(archived) - set(actual)
        if not missing <= set(original):
            raise MigrationError("Backup is missing unrelated Host files")
        archived = {key: value for key, value in archived.items() if key not in missing}
    if actual != archived:
        raise MigrationError("Backup does not match the stopped Host roots")
    return manifest


def _backup_database_matches(archive: Path, manifest: dict, database_url: str, task_id: str,
                             expected: tuple[int, int, str, set[str]], restored_database_url: str | None) -> None:
    if manifest["database_kind"] != make_url(database_url).get_backend_name():
        raise MigrationError("Backup and live database types differ")
    if manifest["database_kind"] != "sqlite":
        if (not restored_database_url
                or make_url(restored_database_url).get_backend_name() != "postgresql"
                or make_url(restored_database_url) == make_url(database_url)
                or _db_task(restored_database_url, task_id) != expected):
            raise MigrationError("PostgreSQL migration requires matching task in a verified backup restore")
        return
    import tarfile
    with tempfile.TemporaryDirectory(prefix="ta-session-backup-db-") as directory:
        target = Path(directory) / "backup.db"
        with tarfile.open(archive, "r:gz") as stream:
            source = stream.extractfile("database.sqlite3")
            if source is None:
                raise MigrationError("Backup database is missing")
            with target.open("xb") as output:
                shutil.copyfileobj(source, output)
        if _db_task(f"sqlite:///{target}", task_id) != expected:
            raise MigrationError("Backup database does not contain the same task bindings")


def migrate(task_id: str, source_dir: Path, database_url: str, task_root: Path,
            session_root: Path, archive: Path, *, apply: bool = False, finalize: bool = False,
            replay_verified: bool = False, restored_database_url: str | None = None) -> dict:
    if apply and finalize:
        raise MigrationError("Copy and finalize must be separate stop-the-world steps")
    if finalize and not replay_verified:
        raise MigrationError("Verify the copied Session through the Host replay API before finalizing")
    if not TASK_ID.fullmatch(task_id):
        raise MigrationError("Invalid task ID")
    if any(is_link_like(path) for path in (task_root, session_root, source_dir)):
        raise MigrationError("Storage paths must not be symlinks or junctions")
    task_root = task_root.resolve(strict=True)
    session_root = session_root.resolve(strict=True)
    source_dir = source_dir.resolve(strict=True)
    if (not source_dir.is_dir() or source_dir.parent != session_root
            or task_root == session_root or task_root in session_root.parents
            or session_root in task_root.parents):
        raise MigrationError("Select one direct legacy Session directory within separate roots")
    # Reject special files before either the inventory or backup verifier opens
    # an artifact. In particular, a FIFO named session.v4.jsonl must not block.
    list(_checked_files(task_root))
    list(_checked_files(session_root))
    destination = session_root / task_id
    if is_link_like(destination) or (finalize and not destination.is_dir()) or (not finalize and destination.exists()):
        raise MigrationError("Task-private directory already exists; refuse to merge")
    indexes = {}
    references = {}
    for path in _checked_files(task_root):
        if path.parent == task_root and path.name.endswith(".deleted.json"):
            raise MigrationError("Pending Host deletion markers require manual reconciliation")
        if path.parent == task_root and path.suffix == ".json" and not path.name.endswith(".events.json"):
            indexed, owner, ids, _unconfirmed = _task_index(path)
            indexes[indexed] = (owner, ids)
            for session_id in ids:
                references.setdefault(session_id, set()).add(indexed)
    if source_dir.name in indexes:
        raise MigrationError("Source is a task-owned directory, not a legacy directory")
    index = indexes.get(task_id)
    if index is None or not (task_root / f"{task_id}.events.json").is_file():
        raise MigrationError("Host task index or replay is missing")
    raw_index = json.loads((task_root / f"{task_id}.json").read_text(encoding="utf-8"))
    business = _db_task(database_url, task_id)
    bound = index[1]
    if (type(raw_index.get("user_id")) is not int or raw_index["user_id"] < 1
            or business[0] != index[0] or business[1] != raw_index["user_id"]
            or business[2] not in {"completed", "failed", "cancelled"}
            or raw_index.get("status") != business[2]
            or business[3] - {f"ta-{task_id}"} != bound or not bound):
        raise MigrationError("Host and business ownership, terminal state or Session bindings differ")
    if any(references[session_id] != {task_id} for session_id in bound):
        raise MigrationError("Session ID is referenced by multiple tasks")
    report = audit(business[0], database_url, task_root, session_root)
    if report["status"] != "consistent" or report["shared_sessions"] or report["missing_sessions"]:
        raise MigrationError("Storage reconciliation must be consistent before migration")
    source_files = list(_checked_files(source_dir))
    if (not source_files and not finalize) or any(not SESSION_FILE.fullmatch(path.name) for path in source_files):
        raise MigrationError("Legacy directory contains unknown or no files")
    source_ids = {_header_id(path) for path in source_files}
    if (not source_ids <= bound) or (not finalize and source_ids != bound):
        raise MigrationError("Legacy directory contains unbound or missing Sessions")
    private_files = list(_checked_files(destination)) if finalize else []
    if finalize and {_header_id(path) for path in private_files} != bound:
        raise MigrationError("Private directory does not contain all bound Sessions")
    for path in _checked_files(session_root):
        if SESSION_FILE.fullmatch(path.name) and _header_id(path) in bound and path not in source_files + private_files:
            raise MigrationError("Bound Session ID exists outside the selected directory")
    manifest = _files_match_backup(archive, task_root, session_root,
                                   private_copy=destination if finalize else None,
                                   legacy_source=source_dir if finalize else None)
    _backup_database_matches(archive, manifest, database_url, task_id, business, restored_database_url)
    result = {"status": "ready_to_finalize" if finalize else "ready", "session_count": len(bound),
              "file_count": len(private_files) if finalize else len(source_files),
              "source_preserved": True}
    if finalize:
        if _db_task(database_url, task_id) != business:
            raise MigrationError("Task bindings changed before finalization")
        for path in source_files:
            path.unlink()
        for directory in sorted((path for path in source_dir.rglob("*") if path.is_dir()),
                                key=lambda path: len(path.parts), reverse=True):
            directory.rmdir()
        source_dir.rmdir()
        _fsync_directory(session_root)
        report = audit(business[0], database_url, task_root, session_root)
        if report["status"] != "consistent":
            raise MigrationError("Finalized Session storage is not consistent; restore backup before retrying")
        result.update(status="finalized", source_preserved=False,
                      remaining_legacy_layout_sessions=report["legacy_layout_sessions"])
        return result
    if not apply:
        return result

    stage = Path(tempfile.mkdtemp(prefix=".ta-migrate-", dir=session_root))
    try:
        stage.chmod(0o700)
        for path in source_files:
            target = stage / path.relative_to(source_dir)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("rb") as original, target.open("xb") as copy:
                shutil.copyfileobj(original, copy)
                copy.flush()
                os.fsync(copy.fileno())
            target.chmod(0o600)
            if _digest(path) != _digest(target) or _header_id(target) != _header_id(path):
                raise MigrationError("Copied Session failed hash or header validation")
        _files_match_backup(archive, task_root, session_root, staging=stage)  # Reject concurrent writes.
        if _db_task(database_url, task_id) != business:
            raise MigrationError("Task bindings changed during copying")
        if destination.exists() or is_link_like(destination):
            raise MigrationError("Task-private directory appeared during migration")
        for directory in sorted({stage, *(path.parent for path in stage.rglob("*") if path.is_file())},
                                key=lambda path: len(path.parts), reverse=True):
            _fsync_directory(directory)
        os.rename(stage, destination)
        _fsync_directory(session_root)
        result["status"] = "copied"
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    if sys.platform not in {"darwin", "win32", "linux"}:
        parser.error("Legacy Session migration is supported only on macOS, Windows and Linux")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or None)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--backup-archive", type=Path, required=True)
    parser.add_argument("--services-stopped", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Atomically publish a verified copy; keep original")
    parser.add_argument("--finalize", action="store_true", help="After Host replay verification, remove only the copied legacy source")
    parser.add_argument("--replay-verified", action="store_true", help="Confirm Host replay matched the copied Session")
    parser.add_argument("--verified-restore-database-url",
                        default=os.getenv("TEST_ATELIER_VERIFIED_RESTORE_DATABASE_URL"),
                        help="PostgreSQL backup restore URL (prefer the environment variable)")
    args = parser.parse_args()
    if not args.database_url and os.getenv("AI_STUDIO_DATABASE_PASSWORD_FILE"):
        try:
            args.database_url = configured_database_url()
        except ValueError:
            print(json.dumps({"status": "error", "error_type": "InvalidDatabaseConfiguration"}), file=sys.stderr)
            return 1
    if not args.database_url or not args.services_stopped:
        parser.error("DATABASE_URL and --services-stopped are required")
    try:
        result = migrate(args.task_id, args.source_dir, args.database_url, args.task_root,
                         args.session_root, args.backup_archive, apply=args.apply, finalize=args.finalize,
                         replay_verified=args.replay_verified,
                         restored_database_url=args.verified_restore_database_url)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
