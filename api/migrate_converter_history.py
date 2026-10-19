"""Copy legacy ecxel_check history into a dedicated project, without changing the source."""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .conversion_routes import DATA_DIR
from .db import ConversionRecord, Membership, Project, SessionLocal, User, engine
from .observability import deployment_release, sanitize_log_fields
from .storage import is_private_regular_file, open_private_regular_file, write_private_atomic


def _event(event: str, level: str = "INFO", **fields: object) -> dict[str, object]:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": 1,
        "level": level,
        "service": "test-atelier",
        "environment": os.getenv("AI_STUDIO_ENV", "development")[:32],
        "event": event,
        "component": "legacy-migration",
        "request_id": "-",
        **sanitize_log_fields(fields, allowed={"error_type", "dry_run", "duration_ms", "count"}),
    }
    release = deployment_release()
    if release:
        payload["release"] = release
    return payload


def _source_file(root: Path, relative_name: str) -> Path:
    """Resolve a legacy artifact while rejecting symlinked source files."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("历史目录无效")
    candidate = root / relative_name
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("历史文件路径无效") from exc
    if not is_private_regular_file(candidate):
        raise ValueError("历史文件必须是普通文件")
    return candidate


def migrate(source: Path, dry_run: bool = False, quiet: bool = False) -> int:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("历史目录无效")
    source = source.resolve()
    records_path = source / "records.json"
    if not is_private_regular_file(records_path):
        raise ValueError("历史索引必须是普通文件")
    records = json.loads(records_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("历史索引格式错误")
    # Schema ownership stays with Alembic. This utility only migrates legacy
    # rows and must never silently create or mutate a production schema.
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.role == "admin").order_by(User.id))
        if admin is None:
            raise ValueError("Test Atelier 尚无管理员，请先启动 API 初始化")
        project = db.scalar(select(Project).where(Project.name == "历史转换记录"))
        project_id = project.id if project else None
        pending = []
        for item in records:
            if db.get(ConversionRecord, item["id"]):
                continue
            filename = item["stored_filename"]
            file_path = _source_file(source / "files", filename)
            preview_path = _source_file(source / "previews", item["preview_filename"])
            data = file_path.read_bytes()
            if len(data) != item["file_size"]:
                raise ValueError(f"历史文件大小不匹配: {item['id']}")
            preview = json.loads(preview_path.read_text(encoding="utf-8"))
            pending.append((item, file_path, preview, hashlib.sha256(data).hexdigest()))
        if dry_run:
            if not quiet:
                print(f"待迁入 {len(pending)} 条, 目标项目: {project_id or '新建历史转换记录'}")
            return len(pending)
        if not pending:
            if not quiet:
                print("迁入 0 条, 无需创建历史项目")
            return 0
        if not project:
            project = Project(name="历史转换记录")
            db.add(project)
            db.flush()
            db.add(Membership(project_id=project.id, user_id=admin.id))
        copied = []
        try:
            for item, path, preview, digest in pending:
                target = DATA_DIR / str(project.id) / f"{item['id']}.bin"
                write_private_atomic(target, path.read_bytes(), root=DATA_DIR)
                copied.append(target)
                stream, _ = open_private_regular_file(target, root=DATA_DIR)
                with stream:
                    if hashlib.sha256(stream.read()).hexdigest() != digest:
                        raise ValueError(f"迁入文件校验失败: {item['id']}")
                db.add(ConversionRecord(
                    id=item["id"], project_id=project.id, user_id=admin.id,
                    created_at=item["created_at"], source_filename=item["source_filename"],
                    output_filename=item["output_filename"], conversion_mode=item["conversion_mode"],
                    output_mode=item["output_mode"], media_type=item["media_type"],
                    app_version=item.get("app_version", ""), row_count=item["row_count"],
                    file_size=item["file_size"], preview=preview,
                ))
            db.commit()
        except Exception:
            db.rollback()
            for target in copied:
                target.unlink(missing_ok=True)
            raise
        if not quiet:
            print(f"迁入 {len(pending)} 条, 项目 ID: {project.id}")
        return len(pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="原工程 history 目录")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出单行结构化事件")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        count = migrate(args.source, args.dry_run, quiet=args.as_json)
    except KeyboardInterrupt:
        if args.as_json:
            print(json.dumps(_event(
                "legacy_migration.cancelled", "WARNING", error_type="KeyboardInterrupt",
                dry_run=args.dry_run, duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ), ensure_ascii=False, separators=(",", ":")), flush=True)
            return 130
        raise
    except Exception as exc:
        if args.as_json:
            print(json.dumps(_event(
                "legacy_migration.failed", "ERROR", error_type=type(exc).__name__,
                dry_run=args.dry_run, duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ), ensure_ascii=False, separators=(",", ":")), flush=True)
            return 1
        raise
    if args.as_json:
        print(json.dumps(_event(
            "legacy_migration.completed", count=count, dry_run=args.dry_run,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        ), ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
