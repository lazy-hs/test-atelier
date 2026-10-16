"""Project-scoped HTTP adapter for the ecxel_check conversion engine."""

import io
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conversion import service
from .conversion.converter import ConversionError
from .db import ConversionRecord, ConversionSettings, User

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DATA_DIR = Path(os.getenv("AI_STUDIO_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))) / "conversions"


class ConfigUpdate(BaseModel):
    default_prefix_path: str | None = Field(default=None, max_length=2000)
    default_case_type: str | None = None
    default_case_status: str | None = None
    history_retention_days: int | None = None


def settings_for(db: Session, project_id: int) -> ConversionSettings:
    settings = db.get(ConversionSettings, project_id)
    if settings is None:
        settings = ConversionSettings(project_id=project_id, default_prefix_path="",
                                      default_case_type="业务功能用例", default_case_status="正常",
                                      history_retention_days=180)
    return settings


def public_config(settings: ConversionSettings) -> dict:
    return {
        "app_name": "用例格式转换台", "app_version": service.APP_VERSION,
        "template_file": service.TEMPLATE.name, "path_separator": service.PATH_SEPARATOR,
        "case_types": service.CASE_TYPES, "case_statuses": service.CASE_STATUSES,
        "default_prefix_path": settings.default_prefix_path,
        "default_case_type": settings.default_case_type,
        "default_case_status": settings.default_case_status,
        "history_retention_days": settings.history_retention_days,
        "history_retention_day_options": [180, 60, 30],
    }


def record_dict(record: ConversionRecord) -> dict:
    return {key: getattr(record, key) for key in (
        "id", "created_at", "source_filename", "output_filename", "conversion_mode",
        "output_mode", "media_type", "app_version", "row_count", "file_size",
    )}


def output_path(record: ConversionRecord) -> Path:
    return DATA_DIR / str(record.project_id) / f"{record.id}.bin"


def prune(db: Session, project_id: int, days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for record in db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id)).all():
        try:
            created = datetime.fromisoformat(record.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff:
            output_path(record).unlink(missing_ok=True)
            db.delete(record)
    db.commit()


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/conversion", tags=["conversion"])

    def scope(project_id: int, user: User, db: Session):
        project_access(project_id, user, db)

    def find_record(project_id: int, record_id: str, db: Session) -> ConversionRecord:
        record = db.get(ConversionRecord, record_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(404, "历史记录不存在或已过期。")
        return record

    async def read_file(file: UploadFile) -> tuple[bytes, str]:
        name = Path(file.filename or "").name
        if not name or Path(name).suffix.lower() not in {".xlsx", ".csv", ".xmind"}:
            raise HTTPException(400, "文件类型不支持")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "文件超过 20 MB 限制")
        return data, name

    @router.get("/config")
    def get_config(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        config = public_config(settings_for(db, project_id))
        db.commit()
        return config

    @router.patch("/config", dependencies=[Depends(csrf)])
    def patch_config(project_id: int, payload: ConfigUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        settings = settings_for(db, project_id)
        if payload.default_prefix_path is not None:
            settings.default_prefix_path = payload.default_prefix_path
        if payload.default_case_type is not None:
            if payload.default_case_type not in service.CASE_TYPES:
                raise HTTPException(422, "用例类型不在当前配置允许范围内。")
            settings.default_case_type = payload.default_case_type
        if payload.default_case_status is not None:
            if payload.default_case_status not in service.CASE_STATUSES:
                raise HTTPException(422, "用例状态不在当前配置允许范围内。")
            settings.default_case_status = payload.default_case_status
        if payload.history_retention_days is not None:
            if payload.history_retention_days not in (30, 60, 180):
                raise HTTPException(422, "历史记录保留天数仅支持 30、60、180。")
            settings.history_retention_days = payload.history_retention_days
        db.add(settings)
        db.commit()
        return public_config(settings)

    @router.post("/preview", dependencies=[Depends(csrf)])
    async def preview(project_id: int, file: UploadFile = File(...), conversion_mode: str = Form("excel_to_excel"),
                      output_mode: str = Form("after"), prefix_path: str = Form(""), case_type: str = Form(""),
                      case_status: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        data, name = await read_file(file)
        try:
            return service.preview(data, name, conversion_mode, output_mode, prefix_path, case_type, case_status)
        except ConversionError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/export", dependencies=[Depends(csrf)])
    async def export(project_id: int, file: UploadFile = File(...), conversion_mode: str = Form("excel_to_excel"),
                     output_mode: str = Form("after"), prefix_path: str = Form(""), case_type: str = Form(""),
                     case_status: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        data, name = await read_file(file)
        try:
            result, output, output_name, media_type = service.export(data, name, conversion_mode, output_mode, prefix_path, case_type, case_status)
        except ConversionError as exc:
            raise HTTPException(422, str(exc)) from exc
        record = ConversionRecord(
            id=uuid.uuid4().hex, project_id=project_id, user_id=user.id,
            source_filename=name, output_filename=output_name, conversion_mode=conversion_mode,
            output_mode=result["output_mode"], media_type=media_type, app_version=service.APP_VERSION,
            row_count=result["row_count"], file_size=len(output), preview=result,
        )
        path = output_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(output)
            os.replace(temporary, path)
            db.add(record)
            db.commit()
        except Exception:
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            db.rollback()
            raise
        prune(db, project_id, settings_for(db, project_id).history_retention_days)
        return StreamingResponse(io.BytesIO(output), media_type=media_type,
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(output_name)}"})

    @router.get("/history")
    def history(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        settings = settings_for(db, project_id)
        prune(db, project_id, settings.history_retention_days)
        records = db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id).order_by(ConversionRecord.created_at.desc())).all()
        return {"retention_days": settings.history_retention_days, "records": [record_dict(item) for item in records]}

    @router.get("/history/{record_id}")
    def history_preview(project_id: int, record_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        record = find_record(project_id, record_id, db)
        return {"record": record_dict(record), "preview": record.preview}

    @router.get("/history/{record_id}/download")
    def history_download(project_id: int, record_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        record = find_record(project_id, record_id, db)
        path = output_path(record)
        if not path.is_file():
            raise HTTPException(404, "历史结果文件不存在。")
        return FileResponse(path, media_type=record.media_type,
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(record.output_filename)}"})

    @router.delete("/history/{record_id}", dependencies=[Depends(csrf)])
    def history_delete(project_id: int, record_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        record = find_record(project_id, record_id, db)
        output_path(record).unlink(missing_ok=True)
        db.delete(record)
        db.commit()
        return history(project_id, user, db)

    @router.delete("/history", dependencies=[Depends(csrf)])
    def history_clear(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        records = db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id)).all()
        for record in records:
            output_path(record).unlink(missing_ok=True)
            db.delete(record)
        db.commit()
        return {**history(project_id, user, db), "deleted_count": len(records)}

    return router
