"""Project-scoped document, analysis and case-authoring endpoints."""

import io
import json
import logging
import os
import subprocess
import uuid
import httpx
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from . import ai_engine, ai_jobs, config_file
from .skill_runner import SkillRunnerError, discover_skills, generate_xmind as generate_skill_xmind
from .db import AiJob, AiSettings, AnalysisRun, Case, CaseDraft, ImportBatch, ProjectSkill, SourceDocument, User, utcnow
from .document_extract import extract
from .observability import monotonic, redact_sensitive_text, request_id as current_request_id
from .storage import open_private_regular_file, write_private_atomic

logger = logging.getLogger(__name__)


def _prompt_texts(job: AiJob) -> list[str]:
    """Normalize persisted prompt history before calculating or returning it.

    The current schema stores plain strings, but early development databases
    could contain structured ``{"text": ...}`` entries. Prompt inspection is
    a diagnostic path and must not turn one malformed legacy row into a 500.
    Invalid entries are ignored rather than exposing arbitrary object data as
    a React child or attempting to measure a non-string value.
    """
    normalized: list[str] = []
    for value in list(job.prompts or []):
        if isinstance(value, str):
            normalized.append(value)
        elif isinstance(value, dict) and isinstance(value.get("text"), str):
            normalized.append(value["text"])
    return normalized

DOCUMENT_DIR = Path(os.getenv("AI_STUDIO_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))) / "documents"
MAX_FILE_BYTES = 20 * 1024 * 1024


class AnalysisCreate(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=10)
    provider: str = Field(default="local", max_length=32)


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
    claude_command: str = Field(default="claude", max_length=255)
    claude_model: str = Field(default="", max_length=120)
    providers: list[dict] | None = Field(default=None, max_length=50)
    provider: str = "local"


class ProviderProbeRequest(BaseModel):
    provider_id: str = Field(default="", max_length=80)
    endpoint: str = Field(min_length=1, max_length=255)
    protocol: Literal["openai", "responses", "anthropic", "gemini", "system-one"] = "openai"
    model: str = Field(default="", max_length=160)
    models: list[str] = Field(default_factory=list, max_length=30)
    api_key: str = Field(default="", max_length=1000)


def valid_url(value: str, required: bool = False) -> bool:
    if not value:
        return not required
    return urlparse(value).scheme in {"http", "https"} and bool(urlparse(value).netloc)


def _provider_key(settings: AiSettings, provider_id: str, typed_key: str) -> str:
    if typed_key.strip():
        return typed_key.strip()
    try:
        catalog = json.loads(settings.providers_json or "[]")
    except (TypeError, ValueError):
        catalog = []
    for item in catalog if isinstance(catalog, list) else []:
        if isinstance(item, dict) and str(item.get("id")) == provider_id:
            encrypted = str(item.get("api_key_encrypted") or "")
            if encrypted:
                return ai_engine.decrypt_secret(encrypted)
    return ""


def _probe_provider(endpoint: str, protocol: str, model: str, api_key: str) -> dict:
    """Send a minimal request used by protocol/model detection."""
    base = endpoint.rstrip("/")
    headers = {"Accept": "application/json"}
    params: dict[str, str] = {}
    if protocol in {"openai", "responses", "system-one"}:
        url = f"{base}/responses" if protocol == "responses" else f"{base}/chat/completions"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        if protocol == "responses":
            body = {"model": model, "input": "ping", "max_output_tokens": 1}
    elif protocol == "anthropic":
        url = f"{base}/messages"
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    else:
        url = f"{base}/models/{quote(model, safe='')}:generateContent"
        if api_key:
            params["key"] = api_key
        body = {"contents": [{"parts": [{"text": "ping"}]}]}
    response = httpx.post(url, headers=headers, params=params, json=body, timeout=10)
    ok = 200 <= response.status_code < 300
    result = {"ok": ok, "status": response.status_code, "protocol": protocol, "model": model}
    if not ok:
        result["error"] = redact_sensitive_text(response.text[:240] or "请求失败")
    return result


def _listed_models(endpoint: str, protocol: str, api_key: str) -> list[dict[str, str]]:
    base = endpoint.rstrip("/")
    headers = {"Accept": "application/json"}
    params: dict[str, str] = {}
    if protocol == "gemini":
        url = f"{base}/models"
        if api_key:
            params["key"] = api_key
    else:
        url = f"{base}/models"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()
    raw = payload.get("models", payload.get("data", [])) if isinstance(payload, dict) else []
    output: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            model_id = item
            name = item
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").removeprefix("models/")
            name = str(item.get("name") or item.get("id") or model_id).removeprefix("models/")
        else:
            continue
        if model_id:
            output.append({"id": model_id[:160], "name": name[:160]})
    return output[:200]


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
        try:
            providers = json.loads(settings.providers_json or "[]")
            if not isinstance(providers, list):
                providers = []
        except (TypeError, ValueError):
            providers = []
        # Never return encrypted provider secrets to the browser.
        providers = [
            {key: item[key] for key in ("id", "kind", "name", "endpoint", "model", "enabled", "detail", "protocol") if key in item}
            | {"api_key_set": bool(item.get("api_key_encrypted"))}
            for item in providers if isinstance(item, dict)
        ]
        local_enabled = ai_engine.catalog_provider_enabled(settings.providers_json, "local")
        return {"local_url": settings.local_url, "local_model": settings.local_model,
                "local_enabled": local_enabled,
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
                "claude_model": settings.claude_model if admin else "",
                "providers": providers if admin else []}

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
        catalog_api_key = next((str(item.get("api_key", "")).strip() for item in (payload.providers or []) if isinstance(item, dict) and str(item.get("api_key", "")).strip()), "")
        if payload.remote_enabled and (not payload.remote_model.strip() or (not payload.remote_api_key.strip() and not settings.remote_api_key and not catalog_api_key)):
            raise HTTPException(422, "启用远程模型时必须填写模型名称和 API Key")
        settings.local_url = local_url
        settings.local_model = payload.local_model.strip()
        settings.remote_enabled = payload.remote_enabled
        settings.remote_url = remote_url
        settings.remote_model = payload.remote_model.strip()
        codex_command = (payload.codex_command or payload.codex_binary or "codex").strip()
        claude_command = (payload.claude_command or "").strip() or "claude"
        for command, label in ((codex_command, "Codex"), (claude_command, "Claude Code")):
            try:
                ai_engine._validate_cli_command(command)
            except RuntimeError as exc:
                raise HTTPException(422, f"{label} {exc}") from exc
        settings.codex_enabled = payload.codex_enabled
        settings.codex_command = codex_command
        settings.codex_model = payload.codex_model.strip()
        settings.claude_enabled = payload.claude_enabled
        settings.claude_command = claude_command
        settings.claude_model = payload.claude_model.strip()
        if payload.providers is not None:
            # Provider catalog metadata is intentionally non-secret. API keys
            # remain in the encrypted legacy field and are never serialized.
            try:
                previous_catalog = json.loads(settings.providers_json or "[]")
            except (TypeError, ValueError):
                previous_catalog = []
            previous_by_id = {str(item.get("id")): item for item in previous_catalog if isinstance(item, dict)}
            catalog = []
            for item in payload.providers:
                if not isinstance(item, dict):
                    continue
                provider_id = str(item.get("id", "")).strip()[:80]
                name = str(item.get("name", "")).strip()[:120]
                kind = str(item.get("kind", "remote")).strip()
                protocol = str(item.get("protocol", "openai")).strip()
                if not provider_id or not name or kind not in {"local", "remote", "cli"} or protocol not in {"openai", "responses", "anthropic", "gemini", "system-one"}:
                    continue
                prior = previous_by_id.get(provider_id, {})
                encrypted_key = str(prior.get("api_key_encrypted", ""))
                if str(item.get("api_key", "")).strip():
                    encrypted_key = ai_engine.encrypt_secret(str(item["api_key"]).strip())
                catalog.append({
                    "id": provider_id,
                    "kind": kind,
                    "name": name,
                    "endpoint": str(item.get("endpoint", "")).strip()[:255],
                    "model": str(item.get("model", "")).strip()[:120],
                    "enabled": bool(item.get("enabled", False)),
                    "detail": str(item.get("detail", "")).strip()[:160],
                    "protocol": protocol,
                    "api_key_encrypted": encrypted_key,
                })
            settings.providers_json = json.dumps(catalog, ensure_ascii=False)
        if payload.remote_api_key.strip():
            settings.remote_api_key = ai_engine.encrypt_secret(payload.remote_api_key.strip())
        elif catalog_api_key:
            settings.remote_api_key = ai_engine.encrypt_secret(catalog_api_key)
        elif not payload.remote_enabled:
            settings.remote_api_key = ""
        settings.updated_at = utcnow()
        db.commit(); db.refresh(settings)
        ai_engine.apply_config({"local_url": settings.local_url, "local_model": settings.local_model,
                                "local_enabled": ai_engine.catalog_provider_enabled(settings.providers_json, "local"),
                                "remote_enabled": settings.remote_enabled, "remote_url": settings.remote_url,
                                "remote_model": settings.remote_model,
                                "remote_api_key": ai_engine.decrypt_secret(settings.remote_api_key),
                                "codex_enabled": settings.codex_enabled, "codex_command": settings.codex_command,
                                "codex_model": settings.codex_model, "claude_enabled": settings.claude_enabled,
                                "claude_command": settings.claude_command, "claude_model": settings.claude_model})
        try:
            config_file.persist_model_settings(settings)
        except OSError as exc:
            logger.error("config.file.write_failed", extra={
                "event": "config.file.write_failed", "component": "config-file",
                "error_type": type(exc).__name__, "actor_id": user.id,
            })
            raise HTTPException(503, "核心配置文件不可写") from exc
        logger.info("ai.settings.updated", extra={
            "event": "ai.settings.updated", "actor_id": user.id, "role": user.role,
            "component": "ai-settings",
        })
        return config_dict(settings, user)

    @router.post("/ai/admin-config/detect", dependencies=[Depends(csrf)])
    def detect_provider_protocol(project_id: int, payload: ProviderProbeRequest,
                                 user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可检测模型供应商")
        if not valid_url(payload.endpoint, required=True):
            raise HTTPException(422, "供应商地址必须是 http 或 https 地址")
        settings = settings_for(db)
        key = _provider_key(settings, payload.provider_id, payload.api_key)
        model = payload.model.strip()
        if not model:
            try:
                model = (_listed_models(payload.endpoint, payload.protocol, key) or [{"id": "test-model"}])[0]["id"]
            except (httpx.HTTPError, OSError):
                model = "test-model"
        results = []
        for protocol in ("openai", "responses", "anthropic", "gemini", "system-one"):
            try:
                results.append(_probe_provider(payload.endpoint, protocol, model, key))
            except (httpx.HTTPError, OSError) as exc:
                results.append({"ok": False, "protocol": protocol, "model": model,
                                "error": redact_sensitive_text(str(exc))})
        return {"results": results}

    @router.post("/ai/admin-config/test-models", dependencies=[Depends(csrf)])
    def test_provider_models(project_id: int, payload: ProviderProbeRequest,
                             user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可检测模型供应商")
        if not valid_url(payload.endpoint, required=True):
            raise HTTPException(422, "供应商地址必须是 http 或 https 地址")
        settings = settings_for(db)
        key = _provider_key(settings, payload.provider_id, payload.api_key)
        models = [item.strip() for item in payload.models if item.strip()][:30]
        if not models:
            raise HTTPException(422, "请先填写或选择至少一个模型")
        results = []
        for model in models:
            try:
                results.append(_probe_provider(payload.endpoint, payload.protocol, model, key))
            except (httpx.HTTPError, OSError) as exc:
                results.append({"ok": False, "protocol": payload.protocol, "model": model,
                                "error": redact_sensitive_text(str(exc))})
        return {"results": results}

    @router.post("/ai/admin-config/models", dependencies=[Depends(csrf)])
    def fetch_provider_models(project_id: int, payload: ProviderProbeRequest,
                              user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        if user.role != "admin":
            raise HTTPException(403, "仅全局管理员可获取模型列表")
        if not valid_url(payload.endpoint, required=True):
            raise HTTPException(422, "供应商地址必须是 http 或 https 地址")
        try:
            models = _listed_models(payload.endpoint, payload.protocol, _provider_key(settings_for(db), payload.provider_id, payload.api_key))
        except httpx.HTTPError as exc:
            raise HTTPException(502, "无法获取供应商模型列表") from exc
        return {"models": models}

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
        test_started = monotonic()
        tested_provider = payload.provider
        test_error_type = None
        try:
            if payload.provider == "local" and not ai_engine.LOCAL_ENABLED:
                raise ValueError("本地模型未启用")
            if payload.provider in {"codex", "codex_cli", "claude", "claude_code"}:
                if payload.provider in {"codex", "codex_cli"}:
                    command, enabled = (payload.codex_command or payload.codex_binary or "codex").strip(), payload.codex_enabled
                    provider = "codex_cli"
                else:
                    command, enabled = (payload.claude_command or "").strip() or "claude", payload.claude_enabled
                    provider = "claude_code"
                if not enabled:
                    raise ValueError(f"{provider} 未启用")
                try:
                    ai_engine._validate_cli_command(command)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
                # A version check only proves that the executable exists. Run
                # a small structured turn so auth and session initialization
                # are validated before the user starts a long task.
                tested_provider = provider
                ai_engine.test_cli_connection(provider, command, payload.codex_model if provider in {"codex", "codex_cli"} else payload.claude_model)
                return {"ok": True, "provider": provider, "message": "CLI 可连接"}
            if payload.provider == "remote":
                stored_key = ai_engine.decrypt_secret(settings_for(db).remote_api_key)
                remote_api_key = payload.remote_api_key.strip() or stored_key
                if not payload.remote_model.strip() or not remote_api_key:
                    raise ValueError("启用远程模型时必须填写模型名称和 API Key")
                response = httpx.get(remote_url + "/models", headers={"Authorization": f"Bearer {remote_api_key}"}, timeout=8)
                response.raise_for_status()
                return {"ok": True, "provider": "remote", "message": "远程模型服务可连接"}
            response = httpx.get(local_url + "/api/tags", timeout=8)
            response.raise_for_status()
            models = [item.get("name") for item in response.json().get("models", []) if item.get("name")]
            return {"ok": True, "provider": "local", "models": models, "message": "Ollama 可连接"}
        except ValueError as exc:
            test_error_type = type(exc).__name__
            raise HTTPException(422, str(exc)) from exc
        except httpx.HTTPError as exc:
            test_error_type = type(exc).__name__
            raise HTTPException(502, "模型服务无法连接或返回了无效响应") from exc
        except ai_engine.CliTimeout as exc:
            test_error_type = type(exc).__name__
            raise HTTPException(504, redact_sensitive_text(str(exc))) from exc
        except RuntimeError as exc:
            test_error_type = type(exc).__name__
            # Provider CLIs may include connection URLs or credential-shaped
            # diagnostics in stderr. Keep the useful bounded summary while
            # applying the same redaction used by structured logs.
            raise HTTPException(502, redact_sensitive_text(str(exc))) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            test_error_type = type(exc).__name__
            raise HTTPException(502, "CLI 无法启动或返回了失败状态") from exc
        finally:
            logger.log(logging.WARNING if test_error_type else logging.INFO,
                       "ai.provider.connection_test.failed" if test_error_type else "ai.provider.connection_test.succeeded",
                       extra={
                           "event": "ai.provider.connection_test.failed" if test_error_type else "ai.provider.connection_test.succeeded",
                           "provider": tested_provider, "actor_id": user.id,
                           "component": "ai-settings", "error_type": test_error_type,
                           "duration_ms": round((monotonic() - test_started) * 1000, 2),
                       })

    @router.post("/documents", dependencies=[Depends(csrf)])
    async def upload_document(project_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        writable(user, project_id, db)
        filename = Path(file.filename or "").name
        data = await file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "文件超过 20 MB 限制")
        extract_started = monotonic()
        try:
            text, warnings = extract(filename, data)
        except ValueError as exc:
            logger.warning("document.extract.failed", extra={
                "event": "document.extract.failed", "project_id": project_id,
                "actor_id": user.id, "error_type": type(exc).__name__,
                "source": Path(filename).suffix.lower().lstrip("."),
                "duration_ms": round((monotonic() - extract_started) * 1000, 2),
            })
            raise HTTPException(422, str(exc)) from exc
        document = SourceDocument(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id,
                                  filename=filename[:255], extracted_text=text, warnings=warnings)
        path = DOCUMENT_DIR / str(project_id) / f"{document.id}.bin"
        try:
            write_private_atomic(path, data, root=DOCUMENT_DIR)
            db.add(document)
            db.commit()
        except Exception as exc:
            db.rollback()
            path.unlink(missing_ok=True)
            logger.exception("document.persist.failed", extra={
                "event": "document.persist.failed", "project_id": project_id,
                "actor_id": user.id, "error_type": type(exc).__name__,
            })
            raise
        logger.info("document.uploaded", extra={
            "event": "document.uploaded", "project_id": project_id,
            "actor_id": user.id, "source": Path(filename).suffix.lower().lstrip("."),
            "file_size": len(data), "text_length": len(text),
            "warning_count": len(warnings),
            "duration_ms": round((monotonic() - extract_started) * 1000, 2),
        })
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
        try:
            stream, size = open_private_regular_file(path, root=DOCUMENT_DIR)
        except OSError as exc:
            logger.warning("document.download.rejected", extra={
                "event": "document.download.rejected", "project_id": project_id,
                "actor_id": user.id, "error_type": type(exc).__name__,
                "component": "document-storage",
            })
            raise HTTPException(404, "原始文件不存在")
        return StreamingResponse(
            iter(lambda: stream.read(1024 * 1024), b""),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
                "Content-Length": str(size),
            },
            background=BackgroundTask(stream.close),
        )

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
        if payload.provider == "local" and not ai_engine.LOCAL_ENABLED:
            raise HTTPException(403, "本地模型未由管理员启用")
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
                    analysis_id=run.id, kind="analysis", request_id=current_request_id())
        db.add_all([run, job]); db.commit()
        if ai_jobs.enqueue(job.id, current_request_id()) is False:
            raise HTTPException(503, "AI 任务队列已满，请稍后重试", headers={"Retry-After": "5"})
        logger.info("ai_job.enqueued", extra={
            "event": "ai_job.enqueued", "job_id": job.id, "project_id": project_id,
            "kind": job.kind, "provider": run.provider, "actor_id": user.id,
        })
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
        logger.info("analysis.report.updated", extra={
            "event": "analysis.report.updated", "project_id": project_id,
            "actor_id": user.id, "component": "analysis-report",
        })
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
        logger.info("analysis.ambiguity.decided", extra={
            "event": "analysis.ambiguity.decided", "project_id": project_id,
            "actor_id": user.id, "component": "analysis-report",
        })
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
        job = AiJob(id=uuid.uuid4().hex, project_id=project_id, user_id=user.id, analysis_id=run_id,
                    kind="cases", request_id=current_request_id())
        db.add(job); db.commit()
        if ai_jobs.enqueue(job.id, current_request_id()) is False:
            raise HTTPException(503, "AI 任务队列已满，请稍后重试", headers={"Retry-After": "5"})
        logger.info("ai_job.enqueued", extra={
            "event": "ai_job.enqueued", "job_id": job.id, "project_id": project_id,
            "kind": job.kind, "provider": run.provider, "actor_id": user.id,
        })
        return {"job_id": job.id, "status": "queued"}

    @router.get("/ai/jobs/{job_id}")
    def job_detail(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        job = db.get(AiJob, job_id)
        if not job or job.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        return {"id": job.id, "kind": job.kind, "status": job.status, "error": job.error,
                "attempts": job.attempts, "analysis_id": job.analysis_id,
                "request_id": job.request_id or "",
                "prompt_calls": job.prompt_calls or 0, "prompt_length": job.prompt_length or 0,
                "prompt_available": bool(job.prompts)}

    def _load_prompt_job(project_id: int, job_id: str, user: User, db: Session) -> AiJob:
        """Load exact prompts without exposing provider configuration secrets.

        Project access is required just like the job status endpoint.  Prompts
        are never written to application logs; the hash lets clients verify a
        cached copy without exposing any additional configuration values.
        """
        scope(project_id, user, db)
        job = db.get(AiJob, job_id)
        if not job or job.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        return job

    @router.get("/ai/jobs/{job_id}/prompt")
    def job_prompt(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Legacy singular endpoint returning the raw prompt strings."""
        job = _load_prompt_job(project_id, job_id, user, db)
        prompts = _prompt_texts(job)
        logger.info("ai_prompt.viewed", extra={
            "event": "ai_prompt.viewed", "job_id": job.id, "project_id": project_id,
            "analysis_id": job.analysis_id, "kind": job.kind, "actor_id": user.id, "prompt_calls": len(prompts),
            "prompt_length": sum(len(item) for item in prompts), "component": "prompt-viewer",
        })
        return {"job_id": job.id, "analysis_id": job.analysis_id, "kind": job.kind,
                "status": job.status, "prompts": prompts,
                "prompt_calls": job.prompt_calls or len(prompts),
                "prompt_length": job.prompt_length or sum(len(item) for item in prompts),
                "prompt_sha256": job.prompt_sha256 or ""}

    @router.get("/ai/jobs/{job_id}/prompts")
    def job_prompts(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Structured prompt history used by the prompt viewer."""
        job = _load_prompt_job(project_id, job_id, user, db)
        prompts = _prompt_texts(job)
        run = db.get(AnalysisRun, job.analysis_id)
        provider = run.provider if run else ""
        logger.info("ai_prompt.viewed", extra={
            "event": "ai_prompt.viewed", "job_id": job.id, "project_id": project_id,
            "analysis_id": job.analysis_id, "kind": job.kind, "provider": provider, "actor_id": user.id,
            "prompt_calls": len(prompts), "prompt_length": sum(len(item) for item in prompts),
            "component": "prompt-viewer",
        })
        entries = [{"index": index, "provider": provider, "text": text, "length": len(text)}
                   for index, text in enumerate(prompts, start=1)]
        return {"job_id": job.id, "analysis_id": job.analysis_id, "kind": job.kind,
                "status": job.status, "prompts": entries,
                "prompt_calls": job.prompt_calls or len(prompts),
                "prompt_length": job.prompt_length or sum(len(item) for item in prompts),
                "prompt_sha256": job.prompt_sha256 or ""}

    @router.get("/ai/jobs")
    def jobs(project_id: int, analysis_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db)
        query = select(AiJob).where(AiJob.project_id == project_id)
        if analysis_id:
            find_run(project_id, analysis_id, db)
            query = query.where(AiJob.analysis_id == analysis_id)
        rows = db.scalars(query.order_by(AiJob.created_at.desc())).all()
        return [{"id": job.id, "kind": job.kind, "status": job.status, "error": job.error,
                 "attempts": job.attempts, "analysis_id": job.analysis_id,
                 "request_id": job.request_id or "",
                 "prompt_calls": job.prompt_calls or 0, "prompt_length": job.prompt_length or 0,
                 "prompt_available": bool(job.prompts)} for job in rows]

    @router.post("/ai/jobs/{job_id}/retry", dependencies=[Depends(csrf)])
    def retry(project_id: int, job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
        scope(project_id, user, db); writable(user, project_id, db)
        job = db.get(AiJob, job_id)
        if not job or job.project_id != project_id:
            raise HTTPException(404, "任务不存在")
        if job.status != "failed":
            raise HTTPException(409, "只能重试失败任务")
        job.status = "queued"; job.error = ""
        job.lease_owner = ""; job.lease_until = 0.0; job.lease_heartbeat = 0.0
        db.commit()
        if ai_jobs.enqueue(job.id, current_request_id()) is False:
            raise HTTPException(503, "AI 任务队列已满，请稍后重试", headers={"Retry-After": "5"})
        logger.info("ai_job.requeued", extra={
            "event": "ai_job.requeued", "job_id": job.id, "project_id": project_id,
            "kind": job.kind, "actor_id": user.id,
        })
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
