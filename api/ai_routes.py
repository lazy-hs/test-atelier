"""Project-scoped document, analysis and case-authoring endpoints."""

import io
import json
import os
import subprocess
import uuid
import httpx
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai_engine, ai_jobs
from .skill_runner import SkillRunnerError, discover_skills, generate_xmind as generate_skill_xmind
from .db import AiJob, AiSettings, AnalysisRun, Case, CaseDraft, ImportBatch, ProjectSkill, SourceDocument, User, utcnow
from .document_extract import extract

DOCUMENT_DIR = Path(os.getenv("AI_STUDIO_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))) / "documents"
MAX_FILE_BYTES = 20 * 1024 * 1024


class AnalysisCreate(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=10)
    provider: str = "local"


class ReportEdit(BaseModel):
    version: int
    report_markdown: str = Field(min_length=1, max_length=200_000)


class Decision(BaseModel):
    version: int
    decision: str = Field(min_length=1, max_length=3000)


class DraftEdit(BaseModel):
    version: int
    payload: ai_engine.DraftOutput


class AiSettingsUpdate(BaseModel):
    local_url: str = Field(min_length=1, max_length=255)
    local_model: str = Field(min_length=1, max_length=120)
    remote_enabled: bool = False
    remote_url: str = Field(default="", max_length=255)
    remote_model: str = Field(default="", max_length=120)
    remote_api_key: str = Field(default="", max_length=1000)
    codex_enabled: bool = False
    codex_command: str = Field(default="", max_length=255)
    codex_binary: str = Field(default="", max_length=255)
    codex_model: str = Field(default="", max_length=120)
    claude_enabled: bool = False
    claude_command: str = Field(default="claude", min_length=1, max_length=255)
    claude_model: str = Field(default="", max_length=120)
    provider: str = "local"


def valid_url(value: str, required: bool = False) -> bool:
    if not value:
        return not required
    return urlparse(value).scheme in {"http", "https"} and bool(urlparse(value).netloc)


def run_dict(run: AnalysisRun) -> dict:
    return {"id": run.id, "project_id": run.project_id, "source_ids": run.source_ids,
            "status": run.status, "provider": run.provider, "result": run.result,
            "error": run.error, "version": run.version, "created_at": run.created_at,
            "updated_at": run.updated_at}


def draft_dict(draft: CaseDraft) -> dict:
    return {"id": draft.id, "analysis_id": draft.analysis_id, "status": draft.status,
            "payload": draft.payload, "version": draft.version, "case_id": draft.case_id}


def build_router(get_db, current_user, csrf, project_access, writable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["ai-workflows"])

    def scope(project_id: int, user: User, db: Session):
        project_access(project_id, user, db)

    def require_skill(project_id: int, skill_id: str, db: Session):
        skill = db.get(ProjectSkill, (project_id, skill_id))
        if skill is None:
            try:
                spec = next(item for item in discover_skills() if item.skill_id == skill_id)
            except (SkillRunnerError, StopIteration) as exc:
                raise HTTPException(503, "项目技能仓不可用") from exc
            skill = ProjectSkill(project_id=project_id, skill_id=spec.skill_id, name=spec.name,
                                 label=spec.label, source_path=spec.path, description=spec.description,
                                 enabled=spec.enabled_by_default)
            db.add(skill); db.commit()
        if skill is not None and not skill.enabled:
            raise HTTPException(409, f"项目技能已停用：{skill.label}")

    def find_run(project_id: int, run_id: str, db: Session) -> AnalysisRun:
        run = db.get(AnalysisRun, run_id)
        if not run or run.project_id != project_id:
            raise HTTPException(404, "分析任务不存在")
        return run

    def find_draft(project_id: int, draft_id: str, db: Session) -> CaseDraft:
        draft = db.get(CaseDraft, draft_id)
        if not draft or draft.project_id != project_id:
            raise HTTPException(404, "用例草稿不存在")
        return draft

    def settings_for(db: Session) -> AiSettings:
        settings = db.get(AiSettings, 1)
        if settings:
            return settings
        settings = AiSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
        return settings

    def config_dict(settings: AiSettings, user: User) -> dict:
        admin = user.role == "admin"
        return {"local_url": settings.local_url, "local_model": settings.local_model,
                "remote_enabled": bool(settings.remote_enabled) and admin,
                "remote_url": settings.remote_url if admin else "",
                "remote_model": settings.remote_model if admin else "",
                "remote_api_key_set": bool(settings.remote_api_key) if admin else False,
                "remote_api_key_mask": "已配置" if settings.remote_api_key and admin else "",
                "codex_enabled": bool(settings.codex_enabled),
                "codex_command": settings.codex_command if admin else "",
                "codex_binary": settings.codex_command if admin else "",
                "codex_model": settings.codex_model if admin else "",
                "claude_enabled": bool(settings.claude_enabled),
                "claude_command": settings.claude_command if admin else "",
                "claude_model": settings.claude_model if admin else ""}

    @router.get("/ai/config")
    def model_config(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        return config_dict(settings_for(db), user)

    @router.get("/ai/admin-config")
    def admin_model_config(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可配置模型")
        return config_dict(settings_for(db), user)

    @router.put("/ai/admin-config", dependencies=[Depends(csrf)])
    def update_admin_model_config(project_id: int, payload: AiSettingsUpdate,
                                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可配置模型")
        local_url, remote_url = payload.local_url.strip().rstrip("/"), payload.remote_url.strip().rstrip("/")
        settings = settings_for(db)
        if not valid_url(local_url) or (payload.remote_enabled and not valid_url(remote_url, required=True)):
            raise HTTPException(422, "模型地址必须是 http 或 https 地址")
        if payload.remote_enabled and (not payload.remote_model.strip() or (not payload.remote_api_key.strip() and not settings.remote_api_key)):
            raise HTTPException(422, "启用远程模型时必须填写模型名称和 API Key")
        settings.local_url = local_url
        settings.local_model = payload.local_model.strip()
        settings.remote_enabled = payload.remote_enabled
        settings.remote_url = remote_url
        settings.remote_model = payload.remote_model.strip()
        codex_command = (payload.codex_command or payload.codex_binary or "codex").strip()
        claude_command = (payload.claude_command or "claude").strip()
        for command, label in ((codex_command, "Codex"), (claude_command, "Claude Code")):
            if not command.strip() or any(char.isspace() for char in command) or "\x00" in command:
                raise HTTPException(422, f"{label} 命令必须是单个可执行文件路径")
        settings.codex_enabled = payload.codex_enabled
        settings.codex_command = codex_command
        settings.codex_model = payload.codex_model.strip()
        settings.claude_enabled = payload.claude_enabled
        settings.claude_command = claude_command
        settings.claude_model = payload.claude_model.strip()
        if payload.remote_api_key.strip():
            settings.remote_api_key = ai_engine.encrypt_secret(payload.remote_api_key.strip())
        elif not payload.remote_enabled:
            settings.remote_api_key = ""
        settings.updated_at = utcnow()
        db.commit(); db.refresh(settings)
        ai_engine.apply_config({"local_url": settings.local_url, "local_model": settings.local_model,
                                "remote_enabled": settings.remote_enabled, "remote_url": settings.remote_url,
                                "remote_model": settings.remote_model,
                                "remote_api_key": ai_engine.decrypt_secret(settings.remote_api_key),
                                "codex_enabled": settings.codex_enabled, "codex_command": settings.codex_command,
                                "codex_model": settings.codex_model, "claude_enabled": settings.claude_enabled,
                                "claude_command": settings.claude_command, "claude_model": settings.claude_model})
        return config_dict(settings, user)

    @router.post("/ai/admin-config/test", dependencies=[Depends(csrf)])
    def test_admin_model_config(project_id: int, payload: AiSettingsUpdate,
                                user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可测试模型")
        local_url, remote_url = payload.local_url.strip().rstrip("/"), payload.remote_url.strip().rstrip("/")
        remote_required = payload.provider == "remote"
        if not valid_url(local_url) or (remote_required and not valid_url(remote_url, required=True)):
            raise HTTPException(422, "模型地址必须是 http 或 https 地址")
        try:
            if payload.provider in {"codex", "codex_cli", "claude", "claude_code"}:
                if payload.provider in {"codex", "codex_cli"}:
                    command, enabled = (payload.codex_command or payload.codex_binary or "codex").strip(), payload.codex_enabled
                    provider = "codex_cli"
                else:
                    command, enabled = payload.claude_command.strip(), payload.claude_enabled
                    provider = "claude_code"
                if not enabled:
                    raise ValueError(f"{provider} 未启用")
                if not command or any(char.isspace() for char in command) or "\x00" in command:
                    raise ValueError("CLI 命令必须是单个可执行文件路径")
                subprocess.run([command, "--version"], capture_output=True, text=True, timeout=8, check=True, shell=False)
                return {"ok": True, "provider": provider, "message": "CLI 可连接"}
            if payload.provider == "remote":
                if not payload.remote_model.strip() or not payload.remote_api_key.strip():
                    raise ValueError("启用远程模型时必须填写模型名称和 API Key")
                response = httpx.get(remote_url + "/models", headers={"Authorization": f"Bearer {payload.remote_api_key.strip()}"}, timeout=8)
                response.raise_for_status()
                return {"ok": True, "provider": "remote", "message": "远程模型服务可连接"}
            response = httpx.get(local_url + "/api/tags", timeout=8)
            response.raise_for_status()
            models = [item.get("name") for item in response.json().get("models", []) if item.get("name")]
            return {"ok": True, "provider": "local", "models": models, "message": "Ollama 可连接"}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "模型服务无法连接或返回了无效响应") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(502, "CLI 无法启动或返回了失败状态") from exc

    @router.post("/documents", dependencies=[Depends(csrf)])
    async def upload_document(project_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        filename = Path(file.filename or "").name
        data = await file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "文件超过 20 MB 限制")
        try:
            text, warnings = extract(filename, data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        document = SourceDocument(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id,
                                  filename=filename[:255], extracted_text=text, warnings=warnings)
        path = DOCUMENT_DIR / str(project_id) / f"{document.id}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
            db.add(document)
            db.commit()
        except Exception:
            db.rollback()
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        return {"id": document.id, "filename": document.filename, "text": text, "warnings": warnings}

    @router.get("/documents")
    def documents(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        rows = db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id).order_by(SourceDocument.created_at.desc())).all()
        return [{"id": row.id, "filename": row.filename, "warnings": row.warnings, "created_at": row.created_at,
                 "text_length": len(row.extracted_text)} for row in rows]

    @router.get("/documents/{document_id}")
    def document_detail(project_id: int, document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        row = db.get(SourceDocument, document_id)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "文档不存在")
        return {"id": row.id, "filename": row.filename, "text": row.extracted_text, "warnings": row.warnings}

    @router.get("/documents/{document_id}/download")
    def document_download(project_id: int, document_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        row = db.get(SourceDocument, document_id)
        if not row or row.project_id != project_id:
            raise HTTPException(404, "文档不存在")
        path = DOCUMENT_DIR / str(project_id) / f"{row.id}.bin"
        if not path.is_file():
            raise HTTPException(404, "原始文件不存在")
        return FileResponse(path, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}"})

    @router.post("/analyses", dependencies=[Depends(csrf)])
    def create_analysis(project_id: int, payload: AnalysisCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        require_skill(project_id, "requirement-analysis", db)
        if payload.provider == "codex":
            payload.provider = "codex_cli"
        if payload.provider == "claude":
            payload.provider = "claude_code"
        if payload.provider not in {"local", "remote", "codex_cli", "claude_code"}:
            raise HTTPException(422, "模型类型无效")
        if payload.provider == "remote" and (user.role != "admin" or not ai_engine.REMOTE_ENABLED):
            raise HTTPException(403, "远程模型未由管理员启用")
        if payload.provider == "codex_cli" and not ai_engine.CODEX_ENABLED:
            raise HTTPException(403, "Codex CLI 未由管理员启用")
        if payload.provider == "claude_code" and not ai_engine.CLAUDE_ENABLED:
            raise HTTPException(403, "Claude Code 未由管理员启用")
        if len(set(payload.source_ids)) != len(payload.source_ids):
            raise HTTPException(422, "文档不可重复选择")
        for document_id in payload.source_ids:
            row = db.get(SourceDocument, document_id)
            if not row or row.project_id != project_id or not row.extracted_text.strip():
                raise HTTPException(422, "文档不存在或未提取到可分析内容")
        run = AnalysisRun(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id,
                          source_ids=payload.source_ids, provider=payload.provider, result={})
        job = AiJob(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id,
                    analysis_id=run.id, kind="analysis")
        db.add_all([run, job]); db.commit()
        ai_jobs.enqueue(job.id)
        return {"analysis_id": run.id, "job_id": job.id, "status": "queued"}

    @router.get("/analyses")
    def analyses(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        rows = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project_id).order_by(AnalysisRun.created_at.desc())).all()
        return [run_dict(row) for row in rows]

    @router.get("/analyses/{run_id}")
    def analysis_detail(project_id: int, run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        return run_dict(find_run(project_id, run_id, db))

    @router.get("/analyses/{run_id}/quality")
    def quality(project_id: int, run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        run = find_run(project_id, run_id, db)
        rows = [item.payload for item in db.scalars(select(CaseDraft).where(CaseDraft.analysis_id == run.id)).all()]
        return ai_engine.assess_cases(rows, run.result)

    @router.patch("/analyses/{run_id}/report", dependencies=[Depends(csrf)])
    def edit_report(project_id: int, run_id: str, payload: ReportEdit, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        run = find_run(project_id, run_id, db)
        if run.status != "ready" or run.version != payload.version:
            raise HTTPException(409, "报告已更新，请刷新")
        run.result = {**run.result, "report_markdown": payload.report_markdown, "report_edited": True}
        run.version += 1; run.updated_at = utcnow(); db.commit()
        return run_dict(run)

    @router.patch("/analyses/{run_id}/ambiguities/{ambiguity_id}", dependencies=[Depends(csrf)])
    def decide(project_id: int, run_id: str, ambiguity_id: str, payload: Decision,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        run = find_run(project_id, run_id, db)
        if run.status != "ready" or run.version != payload.version:
            raise HTTPException(409, "分析结果已更新，请刷新")
        ambiguities = [dict(item) for item in run.result.get("ambiguities", [])]
        target = next((item for item in ambiguities if item["id"] == ambiguity_id), None)
        if target is None:
            raise HTTPException(404, "待澄清项不存在")
        target["decision"] = payload.decision.strip()
        docs = [db.get(SourceDocument, doc_id) for doc_id in run.source_ids]
        result = {**run.result, "ambiguities": ambiguities}
        if not result.get("report_edited"):
            result["report_markdown"] = ai_engine.render_report(result, [{"id": doc.id, "filename": doc.filename} for doc in docs])
        run.result = result; run.version += 1; run.updated_at = utcnow(); db.commit()
        return run_dict(run)

    @router.post("/analyses/{run_id}/generate", dependencies=[Depends(csrf)])
    def generate(project_id: int, run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        require_skill(project_id, "test-cases", db)
        run = find_run(project_id, run_id, db)
        if run.status != "ready":
            raise HTTPException(409, "分析报告尚未就绪")
        if any(item["critical"] and not item.get("decision") for item in run.result.get("ambiguities", [])):
            raise HTTPException(409, "请先确认关键歧义")
        existing = db.scalar(select(CaseDraft.id).where(CaseDraft.analysis_id == run_id).limit(1))
        active = db.scalar(select(AiJob.id).where(AiJob.analysis_id == run_id, AiJob.kind == "cases", AiJob.status.in_(["queued", "running"])) .limit(1))
        if existing or active:
            raise HTTPException(409, "已有草稿或生成任务，请勿重复提交")
        job = AiJob(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id, analysis_id=run_id, kind="cases")
        db.add(job); db.commit(); ai_jobs.enqueue(job.id)
        return {"job_id": job.id, "status": "queued"}

    @router.get("/ai/jobs/{job_id}")
    def job_detail(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        job = db.get(AiJob, job_id)
        if not job or job.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        return {"id": job.id, "kind": job.kind, "status": job.status, "error": job.error, "attempts": job.attempts, "analysis_id": job.analysis_id}

    @router.get("/ai/jobs")
    def jobs(project_id: int, analysis_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        query = select(AiJob).where(AiJob.project_id == project_id)
        if analysis_id:
            find_run(project_id, analysis_id, db)
            query = query.where(AiJob.analysis_id == analysis_id)
        rows = db.scalars(query.order_by(AiJob.created_at.desc())).all()
        return [{"id": job.id, "kind": job.kind, "status": job.status, "error": job.error,
                 "attempts": job.attempts, "analysis_id": job.analysis_id} for job in rows]

    @router.post("/ai/jobs/{job_id}/retry", dependencies=[Depends(csrf)])
    def retry(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        job = db.get(AiJob, job_id)
        if not job or job.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        if job.status != "failed":
            raise HTTPException(409, "只能重试失败任务")
        job.status = "queued"; job.error = ""; db.commit(); ai_jobs.enqueue(job.id)
        return {"job_id": job.id, "status": "queued"}

    @router.get("/drafts")
    def drafts(project_id: int, analysis_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        query = select(CaseDraft).where(CaseDraft.project_id == project_id)
        if analysis_id:
            find_run(project_id, analysis_id, db)
            query = query.where(CaseDraft.analysis_id == analysis_id)
        return [draft_dict(item) for item in db.scalars(query.order_by(CaseDraft.created_at.desc())).all()]

    @router.patch("/drafts/{draft_id}", dependencies=[Depends(csrf)])
    def edit_draft(project_id: int, draft_id: str, payload: DraftEdit, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        draft = find_draft(project_id, draft_id, db)
        if draft.status != "draft" or draft.version != payload.version:
            raise HTTPException(409, "草稿已修改或发布，请刷新")
        known = {item["id"] for item in find_run(project_id, draft.analysis_id, db).result["requirements"]}
        data = payload.payload.model_dump()
        if not set(data["requirement_ids"]).issubset(known) or data["priority"] not in {"P0", "P1", "P2", "P3"} or data["smoke"] not in {"", "是", "否"}:
            raise HTTPException(422, "需求追溯、等级或冒烟值无效")
        if not data["title"].strip() or any(not step["action"].strip() or not step["expected"].strip() for step in data["steps"]):
            raise HTTPException(422, "标题、步骤与预期不可为空")
        draft.payload = data; draft.version += 1; db.commit()
        return draft_dict(draft)

    @router.post("/drafts/{draft_id}/publish", dependencies=[Depends(csrf)])
    def publish(project_id: int, draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        draft = find_draft(project_id, draft_id, db)
        if draft.status == "published":
            return {"case_id": draft.case_id, "status": "published"}
        value = draft.payload
        all_drafts = [item.payload for item in db.scalars(select(CaseDraft).where(CaseDraft.analysis_id == draft.analysis_id)).all()]
        quality = ai_engine.assess_cases(all_drafts, find_run(project_id, draft.analysis_id, db).result)
        if quality["errors"]:
            raise HTTPException(422, "用例草稿质量检查未通过：" + "；".join(quality["errors"]))
        original_id = f"AI-{draft.id[:16]}"
        headers = ["测试用例ID", "用例名称", "用例目录", "用例等级", "前置条件", "用例步骤", "预期结果", "是否冒烟用例", "需求ID", "测试方法", "端类型", "备注"]
        fields = [original_id, value["title"], " / ".join(value["module"]),
                  str({"P0": 1, "P1": 2, "P2": 3, "P3": 4}[value["priority"]]), value["precondition"],
                  "\n".join(step["action"] for step in value["steps"]), "\n".join(step["expected"] for step in value["steps"]),
                  value["smoke"], ", ".join(value["requirement_ids"]), value["method"], value["label"], value["remark"]]
        batch = ImportBatch(project_id=project_id, filename="AI 用例编写", headers=headers, rows=[fields], warnings=[], status="done")
        db.add(batch); db.flush()
        case = Case(project_id=project_id, import_id=batch.id, original_id=original_id,
                    title=value["title"], directory=fields[2], level=fields[3], smoke=value["smoke"], original=fields)
        db.add(case); db.flush()
        draft.status = "published"; draft.case_id = case.id; draft.version += 1; db.commit()
        return {"case_id": case.id, "status": "published"}

    @router.get("/drafts/export")
    def export_drafts(project_id: int, analysis_id: str, format: str = "xmind",
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        find_run(project_id, analysis_id, db)
        rows = [item.payload for item in db.scalars(select(CaseDraft).where(CaseDraft.project_id == project_id, CaseDraft.analysis_id == analysis_id)).all()]
        if not rows:
            raise HTTPException(404, "尚无用例草稿")
        if format == "xmind":
            cases = [{"模块": row["module"], "用例标题": row["title"], "优先级": row["priority"],
                      "前置条件": row["precondition"], "步骤": [{"操作": step["action"], "预期": step["expected"]} for step in row["steps"]],
                      "备注": row["remark"], "标签": row["label"]} for row in rows]
            content = generate_skill_xmind(cases, "测试用例")
            media_type, filename = "application/vnd.xmind.workbook", "用例草稿.xmind"
        elif format == "xlsx":
            book = Workbook(); sheet = book.active; sheet.title = "用例草稿"
            sheet.append(["需求ID", "模块", "用例名称", "用例等级", "前置条件", "用例步骤", "预期结果", "是否冒烟用例", "设计方法", "标签", "备注"])
            def safe(text):
                value = str(text or "")
                return "'" + value if value.startswith(("=", "+", "-", "@")) else value
            for row in rows:
                sheet.append([safe(", ".join(row["requirement_ids"])), safe(" / ".join(row["module"])), safe(row["title"]), row["priority"],
                              safe(row["precondition"]), safe("\n".join(item["action"] for item in row["steps"])),
                              safe("\n".join(item["expected"] for item in row["steps"])), row["smoke"], row["method"], row["label"], safe(row["remark"])])
            output = io.BytesIO(); book.save(output); content = output.getvalue()
            media_type, filename = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "用例草稿.xlsx"
        else:
            raise HTTPException(422, "仅支持 XMind 和 Excel 导出")
        return StreamingResponse(io.BytesIO(content), media_type=media_type,
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    return router
