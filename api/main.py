import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from argon2 import PasswordHasher
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from openpyxl import Workbook

from .db import (Base, AiSettings, AnalysisRun, Case, CaseComment, CaseDraft, CaseSuggestion, ConversionRecord, ExecutionRun, ImportBatch,
                 Membership, MembershipEvent, Project, ReviewEvent, RunCase, SessionLocal,
                 ProjectSkill, SmokeItem, SmokeSuite, SourceDocument, SuggestionJob, TaxonomyVersion, User,
                 ExecutionEvent, engine, utcnow)
from .skill_runner import SkillRunnerError, discover_skills
from .importers import ImportErrorDetail, parse


secret = os.getenv("AI_STUDIO_SESSION_SECRET")
if not secret or len(secret) < 32:
    raise RuntimeError("AI_STUDIO_SESSION_SECRET must contain at least 32 characters")

hasher = PasswordHasher()


def initialize():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(User.id).limit(1)):
            password = os.getenv("AI_STUDIO_ADMIN_PASSWORD")
            if not password or len(password) < 6:
                raise RuntimeError("Set AI_STUDIO_ADMIN_PASSWORD (at least 6 characters) before first startup")
            db.add(User(username=os.getenv("AI_STUDIO_ADMIN_USER", "admin"), password_hash=hasher.hash(password), role="admin"))
            db.commit()
        settings = db.get(AiSettings, 1)
        if not settings:
            settings = AiSettings(id=1)
            db.add(settings)
            db.commit()
        # Existing databases created before CLI provider support have empty
        # command columns; normalize them before loading the runtime config.
        changed = False
        if not settings.codex_command:
            settings.codex_command = "codex"
            changed = True
        if not settings.claude_command:
            settings.claude_command = "claude"
            changed = True
        if changed:
            db.commit()
        from . import ai_engine
        ai_engine.apply_config({"local_url": settings.local_url, "local_model": settings.local_model,
                                "remote_enabled": settings.remote_enabled, "remote_url": settings.remote_url,
                                "remote_model": settings.remote_model,
                                "remote_api_key": ai_engine.decrypt_secret(settings.remote_api_key),
                                "codex_enabled": settings.codex_enabled, "codex_command": settings.codex_command,
                                "codex_model": settings.codex_model, "claude_enabled": settings.claude_enabled,
                                "claude_command": settings.claude_command, "claude_model": settings.claude_model})


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    from .ai_jobs import recover_jobs
    from .suggestion_jobs import recover_jobs as recover_suggestion_jobs
    recover_jobs()
    recover_suggestion_jobs()
    yield


app = FastAPI(title="Test Atelier", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict", https_only=os.getenv("COOKIE_SECURE") == "1")


def get_db():
    with SessionLocal() as db:
        yield db


def current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("uid")
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def csrf(request: Request):
    expected = request.session.get("csrf")
    submitted = request.headers.get("x-csrf-token", "")
    if not expected or not secrets.compare_digest(expected, submitted):
        raise HTTPException(403, "CSRF 校验失败")


def project_access(project_id: int, user: User, db: Session):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if user.role != "admin" and not db.get(Membership, (project_id, user.id)):
        raise HTTPException(403, "无项目访问权限")
    return project


def project_role(project_id: int, user: User, db: Session) -> str:
    project_access(project_id, user, db)
    if user.role == "admin":
        return "admin"
    return db.get(Membership, (project_id, user.id)).role


def writable(user: User, project_id: int, db: Session):
    if project_role(project_id, user, db) == "reader":
        raise HTTPException(403, "只读账号不能修改")


def project_manager(user: User, project_id: int, db: Session):
    if project_role(project_id, user, db) not in ("admin", "manager"):
        raise HTTPException(403, "仅项目管理员可管理成员")


def ensure_project_skills(project_id: int, db: Session) -> list[ProjectSkill]:
    """Materialize the source skill registry for a project on first access."""
    try:
        specs = discover_skills()
    except SkillRunnerError as exc:
        raise HTTPException(503, str(exc)) from exc
    existing = {row.skill_id: row for row in db.scalars(select(ProjectSkill).where(ProjectSkill.project_id == project_id)).all()}
    changed = False
    for spec in specs:
        if spec.skill_id not in existing:
            row = ProjectSkill(project_id=project_id, skill_id=spec.skill_id, name=spec.name,
                               label=spec.label, source_path=spec.path, description=spec.description,
                               enabled=spec.enabled_by_default)
            db.add(row); existing[spec.skill_id] = row; changed = True
        else:
            row = existing[spec.skill_id]
            row.name, row.label, row.source_path, row.description = spec.name, spec.label, spec.path, spec.description
    if changed:
        db.commit()
    return list(existing.values())


def release_assignments(project_id: int, member_id: int, actor_id: int, db: Session):
    case_ids = db.scalars(update(Case).where(Case.project_id == project_id, Case.assignee_id == member_id)
                          .values(assignee_id=None, version=Case.version + 1).returning(Case.id)).all()
    for case_id in case_ids:
        db.add(ReviewEvent(case_id=case_id, user_id=actor_id, action="assignment",
                           before={"assignee_id": member_id}, after={"assignee_id": None},
                           reason="成员权限变更，负责人已清除"))


class Login(BaseModel):
    username: str
    password: str


class Named(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=12)
    role: str


class MembershipCreate(BaseModel):
    user_id: int | None = None
    username: str | None = None
    role: Literal["manager", "reviewer", "reader"] | None = None


class MembershipRoleUpdate(BaseModel):
    role: Literal["manager", "reviewer", "reader"]


class ProjectMemberCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=12)
    role: Literal["manager", "reviewer", "reader"] = "reviewer"


class SkillUpdate(BaseModel):
    enabled: bool


class Review(BaseModel):
    version: int
    final_level: str
    final_smoke: str
    reason: str = Field(min_length=1, max_length=1000)


class Assignment(BaseModel):
    version: int
    assignee_id: int | None = None


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class BatchReviewItem(BaseModel):
    id: int
    version: int
    final_level: str
    final_smoke: str


class BatchReview(BaseModel):
    items: list[BatchReviewItem] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class BatchDelete(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=100)


@app.get("/api/v1/auth/csrf")
def csrf_token(request: Request):
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    return {"token": request.session["csrf"]}


@app.post("/api/v1/auth/login", dependencies=[Depends(csrf)])
def login(payload: Login, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    try:
        valid = bool(user and hasher.verify(user.password_hash, payload.password))
    except Exception:
        valid = False
    if not valid:
        raise HTTPException(401, "用户名或密码错误")
    request.session.clear()
    request.session["uid"] = user.id
    request.session["csrf"] = secrets.token_urlsafe(32)
    return {"id": user.id, "username": user.username, "role": user.role, "csrf": request.session["csrf"]}


@app.post("/api/v1/auth/logout", dependencies=[Depends(csrf)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/api/v1/projects")
def projects(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "admin":
        return [{"id": item.id, "name": item.name, "role": "admin"} for item in db.scalars(select(Project).order_by(Project.id.desc()))]
    rows = db.execute(select(Project, Membership.role).join(Membership).where(Membership.user_id == user.id).order_by(Project.id.desc()))
    return [{"id": project.id, "name": project.name, "role": role} for project, role in rows]


@app.post("/api/v1/projects", dependencies=[Depends(csrf)])
def create_project(payload: Named, user: User = Depends(current_user), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "项目名称不能为空")
    project = Project(name=name)
    db.add(project)
    db.flush()
    db.add(Membership(project_id=project.id, user_id=user.id, role="manager"))
    db.add(MembershipEvent(project_id=project.id, user_id=user.id, actor_id=user.id, action="added", after_role="manager"))
    try:
        for spec in discover_skills():
            db.add(ProjectSkill(project_id=project.id, skill_id=spec.skill_id, name=spec.name,
                                label=spec.label, source_path=spec.path, description=spec.description,
                                enabled=spec.enabled_by_default))
    except SkillRunnerError as exc:
        raise HTTPException(503, str(exc)) from exc
    db.commit()
    return {"id": project.id, "name": project.name, "role": "admin" if user.role == "admin" else "manager"}


@app.get("/api/v1/projects/{project_id}/skills")
def list_project_skills(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    rows = ensure_project_skills(project_id, db)
    return [{"skill_id": row.skill_id, "name": row.name, "label": row.label,
             "source_path": row.source_path, "description": row.description,
             "enabled": row.enabled, "updated_at": row.updated_at} for row in rows]


@app.patch("/api/v1/projects/{project_id}/skills/{skill_id}", dependencies=[Depends(csrf)])
def update_project_skill(project_id: int, skill_id: str, payload: SkillUpdate,
                         user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    rows = ensure_project_skills(project_id, db)
    row = next((item for item in rows if item.skill_id == skill_id), None)
    if row is None:
        raise HTTPException(404, "技能不存在")
    row.enabled = payload.enabled
    row.updated_at = utcnow()
    db.commit()
    return {"skill_id": row.skill_id, "enabled": row.enabled, "updated_at": row.updated_at}


@app.get("/api/v1/projects/{project_id}/overview")
def project_overview(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_access(project_id, user, db)
    total = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id)) or 0
    pending = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id, Case.review_status == "pending")) or 0
    my_pending = db.scalar(select(func.count()).select_from(Case).where(Case.project_id == project_id,
                           Case.assignee_id == user.id, Case.review_status == "pending")) or 0
    pending_suggestions = db.scalar(select(func.count(func.distinct(CaseSuggestion.case_id)))
        .join(Case, CaseSuggestion.case_id == Case.id)
        .where(CaseSuggestion.project_id == project_id, CaseSuggestion.status == "pending",
               CaseSuggestion.case_version == Case.version)) or 0
    analysis_count = db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.project_id == project_id)) or 0
    draft_count = db.scalar(select(func.count()).select_from(CaseDraft).where(CaseDraft.project_id == project_id, CaseDraft.status == "draft")) or 0
    conversion_count = db.scalar(select(func.count()).select_from(ConversionRecord).where(ConversionRecord.project_id == project_id)) or 0
    document_count = db.scalar(select(func.count()).select_from(SourceDocument).where(SourceDocument.project_id == project_id)) or 0
    import_count = db.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.status == "done")) or 0
    smoke_suite_count = db.scalar(select(func.count()).select_from(SmokeSuite).where(SmokeSuite.project_id == project_id)) or 0
    active_run_count = db.scalar(select(func.count()).select_from(ExecutionRun).where(ExecutionRun.project_id == project_id,
                                  ExecutionRun.status == "active")) or 0
    imports = db.scalars(select(ImportBatch).where(ImportBatch.project_id == project_id).order_by(ImportBatch.id.desc()).limit(5)).all()
    analyses = db.scalars(select(AnalysisRun).where(AnalysisRun.project_id == project_id).order_by(AnalysisRun.created_at.desc()).limit(5)).all()
    drafts = db.scalars(select(CaseDraft).where(CaseDraft.project_id == project_id).order_by(CaseDraft.created_at.desc()).limit(5)).all()
    conversions = db.scalars(select(ConversionRecord).where(ConversionRecord.project_id == project_id).order_by(ConversionRecord.created_at.desc()).limit(5)).all()
    documents = db.scalars(select(SourceDocument).where(SourceDocument.project_id == project_id).order_by(SourceDocument.created_at.desc()).limit(5)).all()
    runs = db.scalars(select(ExecutionRun).where(ExecutionRun.project_id == project_id).order_by(ExecutionRun.id.desc()).limit(5)).all()
    activity = (
        [{"id": f"import-{item.id}", "kind": "import", "title": item.filename, "status": "已导入" if item.status == "done" else "待确认", "created_at": item.created_at, "view": "imports"} for item in imports]
        + [{"id": f"analysis-{item.id}", "kind": "analysis", "title": f"需求分析 · {len(item.source_ids)} 份材料", "status": {"ready": "已完成", "failed": "失败", "queued": "排队中", "running": "处理中"}.get(item.status, item.status), "created_at": item.created_at, "view": "requirements"} for item in analyses]
        + [{"id": f"draft-{item.id}", "kind": "draft", "title": item.payload.get("title", "用例草稿"), "status": "已发布" if item.status == "published" else "草稿", "created_at": item.created_at, "view": "authoring"} for item in drafts]
        + [{"id": f"conversion-{item.id}", "kind": "conversion", "title": item.output_filename, "status": "已导出", "created_at": item.created_at, "view": "conversion"} for item in conversions]
        + [{"id": f"document-{item.id}", "kind": "document", "title": item.filename, "status": "已上传", "created_at": item.created_at, "view": "requirements"} for item in documents]
        + [{"id": f"run-{item.id}", "kind": "run", "title": f"{item.title} · {item.target_version}", "status": "执行中" if item.status == "active" else "已完成", "created_at": item.started_at, "view": "smoke"} for item in runs]
    )

    def activity_time(item: dict) -> datetime:
        try:
            value = datetime.fromisoformat(item["created_at"])
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    return {"project": {"id": project.id, "name": project.name}, "case_count": total,
            "pending_count": pending, "my_pending_count": my_pending, "pending_suggestion_count": pending_suggestions,
            "analysis_count": analysis_count, "draft_count": draft_count,
            "conversion_count": conversion_count, "document_count": document_count,
            "smoke_suite_count": smoke_suite_count, "active_run_count": active_run_count,
            "import_count": import_count, "recent_activity": sorted(activity, key=activity_time, reverse=True)[:6],
            "recent_imports": [
                {"id": item.id, "filename": item.filename, "count": len(item.rows), "status": item.status, "created_at": item.created_at}
                for item in imports]}


@app.post("/api/v1/users", dependencies=[Depends(csrf)])
def create_user(payload: UserCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin" or payload.role not in ("admin", "reviewer", "reader"):
        raise HTTPException(403, "无权创建该账号")
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise HTTPException(409, "用户名已存在")
    created = User(username=payload.username, password_hash=hasher.hash(payload.password), role=payload.role)
    db.add(created)
    db.commit()
    return {"id": created.id, "username": created.username, "role": created.role}


@app.post("/api/v1/projects/{project_id}/members", dependencies=[Depends(csrf)])
def add_member(project_id: int, payload: MembershipCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    if (payload.user_id is None) == (payload.username is None):
        raise HTTPException(422, "请提供账号 ID 或用户名")
    target = db.get(User, payload.user_id) if payload.user_id is not None else db.scalar(select(User).where(User.username == payload.username.strip()))
    if not target:
        raise HTTPException(404, "账号不存在")
    if target.role == "admin" and user.role != "admin":
        raise HTTPException(403, "无法管理全局管理员")
    role = payload.role or ("manager" if target.role == "admin" else target.role)
    if role == "manager" and user.role != "admin":
        raise HTTPException(403, "仅全局管理员可授予项目管理员权限")
    existing = db.get(Membership, (project_id, target.id))
    if not existing:
        db.add(Membership(project_id=project_id, user_id=target.id, role=role))
        db.add(MembershipEvent(project_id=project_id, user_id=target.id, actor_id=user.id, action="added", after_role=role))
        db.commit()
    return {"id": target.id, "username": target.username, "role": "admin" if target.role == "admin" else (existing.role if existing else role)}


@app.post("/api/v1/projects/{project_id}/members/new", dependencies=[Depends(csrf)])
def create_project_member(project_id: int, payload: ProjectMemberCreate,
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    if user.role != "admin":
        raise HTTPException(403, "仅全局管理员可创建账号")
    username = payload.username.strip()
    if len(username) < 2:
        raise HTTPException(422, "用户名至少 2 个字符")
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "用户名已存在")
    member = User(username=username, password_hash=hasher.hash(payload.password),
                  role="reader" if payload.role == "reader" else "reviewer")
    db.add(member)
    db.flush()
    db.add(Membership(project_id=project_id, user_id=member.id, role=payload.role))
    db.add(MembershipEvent(project_id=project_id, user_id=member.id, actor_id=user.id,
                           action="added", after_role=payload.role))
    db.commit()
    return {"id": member.id, "username": member.username, "role": payload.role}


@app.get("/api/v1/projects/{project_id}/members")
def list_members(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    rows = db.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id).where(Membership.project_id == project_id).order_by(User.username))
    return [{"id": member.id, "username": member.username, "role": "admin" if member.role == "admin" else role} for member, role in rows]


@app.patch("/api/v1/projects/{project_id}/members/{member_id}", dependencies=[Depends(csrf)])
def change_member_role(project_id: int, member_id: int, payload: MembershipRoleUpdate,
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    member = db.get(Membership, (project_id, member_id))
    target = db.get(User, member_id)
    if not member or not target:
        raise HTTPException(404, "项目成员不存在")
    if target.role == "admin" or (user.role != "admin" and (member.role == "manager" or payload.role == "manager")):
        raise HTTPException(403, "无法调整该成员的权限")
    if member.role != payload.role:
        before = member.role
        member.role = payload.role
        if payload.role == "reader":
            release_assignments(project_id, member_id, user.id, db)
        db.add(MembershipEvent(project_id=project_id, user_id=member_id, actor_id=user.id,
                               action="role_changed", before_role=before, after_role=payload.role))
        db.commit()
    return {"id": target.id, "username": target.username, "role": member.role}


@app.delete("/api/v1/projects/{project_id}/members/{member_id}", dependencies=[Depends(csrf)])
def remove_member(project_id: int, member_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    member = db.get(Membership, (project_id, member_id))
    target = db.get(User, member_id)
    if not member or not target:
        raise HTTPException(404, "项目成员不存在")
    if target.role == "admin" or (user.role != "admin" and member.role == "manager"):
        raise HTTPException(403, "无法移除该成员")
    db.add(MembershipEvent(project_id=project_id, user_id=member_id, actor_id=user.id,
                           action="removed", before_role=member.role))
    release_assignments(project_id, member_id, user.id, db)
    db.delete(member)
    db.commit()
    return {"ok": True}


@app.get("/api/v1/projects/{project_id}/member-events")
def member_events(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_manager(user, project_id, db)
    events = db.scalars(select(MembershipEvent).where(MembershipEvent.project_id == project_id).order_by(MembershipEvent.id.desc()).limit(30)).all()
    user_ids = {uid for event in events for uid in (event.user_id, event.actor_id)}
    users = {item.id: item.username for item in db.scalars(select(User).where(User.id.in_(user_ids)))}
    return [{"id": event.id, "member": users.get(event.user_id, "未知账号"), "actor": users.get(event.actor_id, "未知账号"),
             "action": event.action, "before_role": event.before_role, "after_role": event.after_role,
             "created_at": event.created_at} for event in events]


@app.post("/api/v1/projects/{project_id}/imports/preview", dependencies=[Depends(csrf)])
async def preview(project_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    writable(user, project_id, db)
    if not file.filename or file.filename.lower().rsplit(".", 1)[-1] not in ("xls", "xlsx", "xmind"):
        raise HTTPException(400, "文件类型不支持")
    data = await file.read(10 * 1024 * 1024 + 1)
    try:
        headers, rows, warnings = parse(file.filename, data)
    except ImportErrorDetail as exc:
        raise HTTPException(400, str(exc)) from exc
    if not rows:
        raise HTTPException(400, "没有可导入的用例")
    batch = ImportBatch(project_id=project_id, filename=file.filename[:255], headers=headers, rows=rows, warnings=warnings)
    db.add(batch)
    db.commit()
    return {"id": batch.id, "filename": batch.filename, "headers": headers, "count": len(rows), "sample": rows[:5], "warnings": warnings}


@app.post("/api/v1/projects/{project_id}/imports/{batch_id}/confirm", dependencies=[Depends(csrf)])
def confirm(project_id: int, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    writable(user, project_id, db)
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.project_id != project_id:
        raise HTTPException(404, "导入记录不存在")
    if batch.status == "done":
        return {"count": len(batch.rows), "status": "done"}
    headers = batch.headers
    known = set(db.scalars(select(Case.original_id).where(Case.project_id == project_id)))
    incoming = {row[headers.index("测试用例ID")] for row in batch.rows}
    if known & incoming:
        raise HTTPException(409, "项目中已有相同用例 ID，未导入任何行")
    def field(row, name):
        return row[headers.index(name)] or "" if name in headers else ""
    for row in batch.rows:
        db.add(Case(project_id=project_id, import_id=batch.id, original_id=field(row, "测试用例ID"),
                    title=field(row, "用例名称"), directory=field(row, "用例目录"),
                    level=field(row, "用例等级"), smoke=field(row, "是否冒烟用例"), original=row))
    batch.status = "done"
    db.commit()
    return {"count": len(batch.rows), "status": "done"}


@app.get("/api/v1/projects/{project_id}/imports")
def imports(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    return [{"id": item.id, "filename": item.filename, "count": len(item.rows), "status": item.status, "created_at": item.created_at}
            for item in db.scalars(select(ImportBatch).where(ImportBatch.project_id == project_id).order_by(ImportBatch.id.desc()))]


def case_dict(item: Case, assignee: str | None = None, pending_suggestion_count: int = 0):
    return {"id": item.id, "original_id": item.original_id, "title": item.title, "directory": item.directory,
            "level": item.level, "smoke": item.smoke, "original": item.original, "final_level": item.final_level,
            "final_smoke": item.final_smoke, "review_status": item.review_status, "version": item.version,
            "reviewer": item.reviewer, "reviewed_at": item.reviewed_at, "import_id": item.import_id,
            "assignee_id": item.assignee_id, "assignee": assignee,
            "final_categories": item.final_categories or {},
            "final_taxonomy_version_id": item.final_taxonomy_version_id,
            "pending_suggestion_count": pending_suggestion_count}


def case_assignee(item: Case, db: Session) -> str | None:
    if item.assignee_id is None:
        return None
    member = db.get(User, item.assignee_id)
    return member.username if member else None


def apply_review(project_id: int, item_id: int, version: int, level: str, smoke: str,
                 reason: str, user: User, role: str, db: Session) -> tuple[str, dict | None]:
    if level not in ("1", "2", "3", "4") or smoke not in ("", "是", "否"):
        return "invalid", None
    item = db.get(Case, item_id)
    if not item or item.project_id != project_id:
        return "not_found", None
    if role == "reviewer" and item.assignee_id not in (None, user.id):
        return "forbidden", None
    if item.version != version:
        return "conflict", None
    before = {"level": item.final_level, "smoke": item.final_smoke, "status": item.review_status}
    after = {"level": level, "smoke": smoke, "status": "reviewed"}
    if before == after:
        return "unchanged", case_dict(item, case_assignee(item, db))
    changed_at = utcnow()
    result = db.execute(update(Case).where(Case.id == item_id, Case.project_id == project_id, Case.version == version)
                        .values(final_level=level, final_smoke=smoke, review_status="reviewed",
                                version=Case.version + 1, reviewer=user.username, reviewed_at=changed_at)
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        return "conflict", None
    db.add(ReviewEvent(case_id=item_id, user_id=user.id, action="review", before=before, after=after, reason=reason.strip()))
    db.commit()
    db.refresh(item)
    return "saved", case_dict(item, case_assignee(item, db))


@app.get("/api/v1/projects/{project_id}/cases")
def cases(project_id: int, q: str = "", page: int = 1, size: int = 30, assignee: str = "all", status: str = "all", suggestion: str = "all",
          user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    if page < 1 or size < 1 or size > 100 or assignee not in ("all", "me", "unassigned") or status not in ("all", "pending", "reviewed") or suggestion not in ("all", "pending"):
        raise HTTPException(400, "筛选或分页参数无效")
    query = select(Case).where(Case.project_id == project_id)
    if q:
        query = query.where(Case.title.contains(q) | Case.original_id.contains(q) | Case.directory.contains(q))
    if assignee == "me":
        query = query.where(Case.assignee_id == user.id)
    elif assignee == "unassigned":
        query = query.where(Case.assignee_id.is_(None))
    if status != "all":
        query = query.where(Case.review_status == status)
    if suggestion == "pending":
        query = query.where(exists(select(CaseSuggestion.id).where(CaseSuggestion.case_id == Case.id,
                  CaseSuggestion.status == "pending", CaseSuggestion.case_version == Case.version)))
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Case.id).offset((page - 1) * size).limit(size)).all()
    assignee_ids = {item.assignee_id for item in items if item.assignee_id is not None}
    names = {member.id: member.username for member in db.scalars(select(User).where(User.id.in_(assignee_ids)))}
    ids = [item.id for item in items]
    active = dict(db.execute(select(CaseSuggestion.case_id, func.count(CaseSuggestion.id)).join(Case, CaseSuggestion.case_id == Case.id)
        .where(CaseSuggestion.case_id.in_(ids), CaseSuggestion.status == "pending",
               CaseSuggestion.case_version == Case.version).group_by(CaseSuggestion.case_id)).all()) if ids else {}
    return {"total": count, "items": [case_dict(item, names.get(item.assignee_id), active.get(item.id, 0)) for item in items]}


def delete_case_rows(case_ids: list[int], db: Session) -> None:
    """Remove a case and all review/smoke snapshots that reference it."""
    if not case_ids:
        return
    run_case_ids = db.scalars(select(RunCase.id).where(RunCase.case_id.in_(case_ids))).all()
    if run_case_ids:
        db.execute(delete(ExecutionEvent).where(ExecutionEvent.run_case_id.in_(run_case_ids)))
    db.execute(delete(RunCase).where(RunCase.case_id.in_(case_ids)))
    db.execute(delete(SmokeItem).where(SmokeItem.case_id.in_(case_ids)))
    db.execute(delete(CaseSuggestion).where(CaseSuggestion.case_id.in_(case_ids)))
    db.execute(delete(CaseComment).where(CaseComment.case_id.in_(case_ids)))
    db.execute(delete(ReviewEvent).where(ReviewEvent.case_id.in_(case_ids)))
    db.execute(delete(CaseDraft).where(CaseDraft.case_id.in_(case_ids)))
    db.execute(delete(Case).where(Case.id.in_(case_ids)))


@app.delete("/api/v1/projects/{project_id}/cases/batch-delete", dependencies=[Depends(csrf)])
def batch_delete_cases(project_id: int, payload: BatchDelete, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    seen: set[int] = set()
    results: list[dict] = []
    deletable: list[int] = []
    for case_id in payload.case_ids:
        if case_id in seen:
            results.append({"id": case_id, "status": "invalid", "detail": "重复的用例 ID"})
            continue
        seen.add(case_id)
        item = db.get(Case, case_id)
        if not item or item.project_id != project_id:
            results.append({"id": case_id, "status": "not_found"})
            continue
        deletable.append(case_id)
        results.append({"id": case_id, "status": "deleted"})
    delete_case_rows(deletable, db)
    db.commit()
    return {"results": results, "deleted": len(deletable)}


@app.delete("/api/v1/projects/{project_id}/cases/{case_id}", dependencies=[Depends(csrf)])
def delete_case(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    delete_case_rows([case_id], db)
    db.commit()
    return {"id": case_id, "deleted": 1}


@app.put("/api/v1/projects/{project_id}/cases/batch-review", dependencies=[Depends(csrf)])
def batch_review(project_id: int, payload: BatchReview, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    if not payload.reason.strip():
        raise HTTPException(422, "请填写审核理由")
    role = project_role(project_id, user, db)
    seen = set()
    results = []
    for entry in payload.items:
        if entry.id in seen:
            results.append({"id": entry.id, "status": "invalid", "detail": "重复的用例 ID"})
            continue
        seen.add(entry.id)
        outcome, value = apply_review(project_id, entry.id, entry.version, entry.final_level,
                                      entry.final_smoke, payload.reason, user, role, db)
        results.append({"id": entry.id, "status": outcome, "case": value})
    return {"results": results, "saved": sum(item["status"] == "saved" for item in results),
            "conflicts": sum(item["status"] == "conflict" for item in results)}


@app.get("/api/v1/projects/{project_id}/cases/{case_id}")
def case_detail(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    batch = db.get(ImportBatch, item.import_id)
    return {**case_dict(item, case_assignee(item, db)), "headers": batch.headers}


@app.put("/api/v1/projects/{project_id}/cases/{case_id}/assignment", dependencies=[Depends(csrf)])
def assign_case(project_id: int, case_id: int, payload: Assignment,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    role = project_role(project_id, user, db)
    if role == "reviewer" and (item.assignee_id not in (None, user.id) or payload.assignee_id not in (None, user.id)):
        raise HTTPException(403, "只能认领或释放自己的用例")
    target = None
    if payload.assignee_id is not None:
        target = db.get(User, payload.assignee_id)
        membership = db.get(Membership, (project_id, payload.assignee_id))
        if not target or not membership or membership.role not in ("manager", "reviewer"):
            raise HTTPException(422, "负责人必须是当前项目的可写成员")
    if item.version != payload.version:
        raise HTTPException(409, "用例已被他人修改，请刷新")
    if item.assignee_id == payload.assignee_id:
        return case_dict(item, case_assignee(item, db))
    before_id = item.assignee_id
    changed = db.execute(update(Case).where(Case.id == case_id, Case.project_id == project_id, Case.version == payload.version)
                         .values(assignee_id=payload.assignee_id, version=Case.version + 1)
                         .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "用例已被他人修改，请刷新")
    db.add(ReviewEvent(case_id=case_id, user_id=user.id, action="assignment",
                       before={"assignee_id": before_id}, after={"assignee_id": payload.assignee_id},
                       reason=f"负责人调整为 {target.username if target else '未分派'}"))
    db.commit()
    db.refresh(item)
    return case_dict(item, target.username if target else None)


@app.get("/api/v1/projects/{project_id}/cases/{case_id}/activity")
def case_activity(project_id: int, case_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    events = db.scalars(select(ReviewEvent).where(ReviewEvent.case_id == case_id).order_by(ReviewEvent.id.desc()).limit(50)).all()
    comments = db.scalars(select(CaseComment).where(CaseComment.case_id == case_id).order_by(CaseComment.id.desc()).limit(100)).all()
    ids = {author_id for entry in [*events, *comments] for author_id in (entry.user_id,)}
    names = {member.id: member.username for member in db.scalars(select(User).where(User.id.in_(ids)))}
    return {"events": [{"id": entry.id, "action": entry.action, "actor": names.get(entry.user_id, "未知账号"),
                        "before": entry.before, "after": entry.after, "reason": entry.reason, "created_at": entry.created_at}
                       for entry in events],
            "comments": [{"id": entry.id, "actor": names.get(entry.user_id, "未知账号"), "text": entry.text,
                          "created_at": entry.created_at} for entry in comments]}


@app.post("/api/v1/projects/{project_id}/cases/{case_id}/comments", dependencies=[Depends(csrf)])
def add_case_comment(project_id: int, case_id: int, payload: CommentCreate,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    item = db.get(Case, case_id)
    if not item or item.project_id != project_id:
        raise HTTPException(404, "用例不存在")
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "讨论内容不能为空")
    entry = CaseComment(case_id=case_id, user_id=user.id, text=text)
    db.add(entry)
    db.commit()
    return {"id": entry.id, "actor": user.username, "text": entry.text, "created_at": entry.created_at}


@app.put("/api/v1/projects/{project_id}/cases/{case_id}/review", dependencies=[Depends(csrf)])
def review(project_id: int, case_id: int, payload: Review, user: User = Depends(current_user), db: Session = Depends(get_db)):
    writable(user, project_id, db)
    if not payload.reason.strip():
        raise HTTPException(422, "请填写审核理由")
    status, value = apply_review(project_id, case_id, payload.version, payload.final_level,
                                 payload.final_smoke, payload.reason, user, project_role(project_id, user, db), db)
    if status == "not_found":
        raise HTTPException(404, "用例不存在")
    if status == "forbidden":
        raise HTTPException(403, "用例已分派给其他成员")
    if status == "conflict":
        raise HTTPException(409, "用例已被他人修改，请刷新")
    if status == "invalid":
        raise HTTPException(400, "等级或冒烟值无效")
    return value


@app.get("/api/v1/projects/{project_id}/export")
def export(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_access(project_id, user, db)
    items = db.scalars(select(Case).where(Case.project_id == project_id).order_by(Case.id)).all()
    book = Workbook()
    sheet = book.active
    sheet.title = "用例"
    headers = []
    for item in items:
        for name in db.get(ImportBatch, item.import_id).headers:
            if name not in headers:
                headers.append(name)
    appended = ["最终等级", "最终冒烟", "审核状态", "审核人", "审核时间", "负责人",
                "建议来源", "建议等级", "建议冒烟", "建议依据", "最终业务模块", "最终业务场景", "最终测试维度", "最终端类型"]
    sheet.append(headers + appended)
    def safe(value):
        if value is None:
            return None
        text = str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text
    suggestion_rows = db.scalars(select(CaseSuggestion).where(CaseSuggestion.project_id == project_id)
                                 .order_by(CaseSuggestion.id.desc())).all()
    latest = {}
    for proposal in suggestion_rows:
        if proposal.case_id not in latest:
            latest[proposal.case_id] = proposal
    versions = {item.final_taxonomy_version_id for item in items if item.final_taxonomy_version_id is not None}
    definitions = {version.id: version.definitions for version in db.scalars(select(TaxonomyVersion).where(TaxonomyVersion.id.in_(versions)))}
    for item in items:
        source = dict(zip(db.get(ImportBatch, item.import_id).headers, item.original))
        proposal = latest.get(item.id)
        options = definitions.get(item.final_taxonomy_version_id) or {}
        def category_label(key):
            identifier = (item.final_categories or {}).get(key)
            return next((entry["name"] for entry in options.get(key, []) if entry["id"] == identifier), None)
        sheet.append([safe(source.get(name)) for name in headers] + [
            item.final_level, item.final_smoke if item.review_status == "reviewed" else None,
            "已审核" if item.review_status == "reviewed" else "待审核", item.reviewer, item.reviewed_at, case_assignee(item, db),
            proposal.source if proposal else None, proposal.level if proposal else None, proposal.smoke if proposal else None,
            safe(proposal.rationale) if proposal else None,
            *(safe(category_label(key)) for key in ("business_module", "business_scenario", "test_dimension", "endpoint_type"))])
    stats = book.create_sheet("统计")
    stats.append(["指标", "数量"])
    stats.append(["用例总数", len(items)])
    stats.append(["待审核", sum(item.review_status == "pending" for item in items)])
    stats.append(["原始冒烟空值", sum(not item.smoke for item in items)])
    for level in ("1", "2", "3", "4"):
        stats.append([f"原始等级 {level}", sum(item.level == level for item in items)])
    out = io.BytesIO()
    book.save(out)
    out.seek(0)
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="project-{project_id}-cases.xlsx"'})


from .conversion_routes import build_router  # noqa: E402
from .ai_routes import build_router as build_ai_router  # noqa: E402
from .suggestion_routes import build_router as build_suggestion_router  # noqa: E402
from .smoke_routes import build_router as build_smoke_router  # noqa: E402

app.include_router(build_router(get_db, current_user, csrf, project_access, writable))
app.include_router(build_ai_router(get_db, current_user, csrf, project_access, writable))
app.include_router(build_suggestion_router(get_db, current_user, csrf, project_access, writable,
                                           project_manager, project_role, case_dict, case_assignee))
app.include_router(build_smoke_router(get_db, current_user, csrf, project_access, writable))
