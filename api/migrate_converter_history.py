"""Copy legacy ecxel_check history into a dedicated project, without changing the source."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from sqlalchemy import select

from .conversion_routes import DATA_DIR
from .db import Base, ConversionRecord, Membership, Project, SessionLocal, User, engine


def migrate(source: Path, dry_run: bool = False) -> int:
    records = json.loads((source / "records.json").read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("历史索引格式错误")
    Base.metadata.create_all(engine)
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
            file_path = (source / "files" / filename).resolve()
            file_path.relative_to((source / "files").resolve())
            preview_path = (source / "previews" / item["preview_filename"]).resolve()
            preview_path.relative_to((source / "previews").resolve())
            data = file_path.read_bytes()
            if len(data) != item["file_size"]:
                raise ValueError(f"历史文件大小不匹配: {item['id']}")
            preview = json.loads(preview_path.read_text(encoding="utf-8"))
            pending.append((item, file_path, preview, hashlib.sha256(data).hexdigest()))
        if dry_run:
            print(f"待迁入 {len(pending)} 条, 目标项目: {project_id or '新建历史转换记录'}")
            return len(pending)
        if not project:
            project = Project(name="历史转换记录")
            db.add(project)
            db.flush()
            db.add(Membership(project_id=project.id, user_id=admin.id))
        copied = []
        try:
            for item, path, preview, digest in pending:
                target = DATA_DIR / str(project.id) / f"{item['id']}.bin"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                copied.append(target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
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
        print(f"迁入 {len(pending)} 条, 项目 ID: {project.id}")
        return len(pending)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="原工程 history 目录")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    migrate(args.source, args.dry_run)
