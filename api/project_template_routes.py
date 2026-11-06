"""Project-scoped configuration templates; never copy users, tasks or credentials."""

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import classification
from .conversion import service as conversion_service
from .db import (ConversionSettings, Membership, MembershipEvent, Project, ProjectSkill,
                 ProjectTemplate, ProjectTemplateApplication, TaxonomyVersion, User, utcnow)
from .skill_runner import SkillRunnerError, discover_skills


class TemplateName(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _summary(template: ProjectTemplate) -> dict:
    config = template.snapshot
    skills = config.get("skills", [])
    return {"id": template.id, "project_id": template.project_id, "name": template.name,
            "schema_version": config.get("schema_version"), "snapshot_sha256": template.snapshot_sha256,
            "skill_count": len(skills), "enabled_skill_count": sum(item.get("enabled") is True for item in skills),
            "has_taxonomy": config.get("taxonomy") is not None,
            "has_conversion_settings": config.get("conversion") is not None,
            "created_by_id": template.created_by, "created_at": template.created_at}


def _snapshot(project_id: int, db: Session) -> dict:
    skills = db.scalars(select(ProjectSkill).where(ProjectSkill.project_id == project_id)
                        .order_by(ProjectSkill.skill_id)).all()
    conversion = db.get(ConversionSettings, project_id)
    taxonomy = db.scalar(select(TaxonomyVersion).where(TaxonomyVersion.project_id == project_id,
                                                        TaxonomyVersion.status == "published")
                         .order_by(TaxonomyVersion.number.desc()).limit(1))
    snapshot = {"schema_version": "project-template.v1", "source_project_id": project_id,
                "skills": [{"skill_id": item.skill_id, "enabled": item.enabled} for item in skills],
                "conversion": None if conversion is None else {
                    "default_case_type": conversion.default_case_type,
                    "default_case_status": conversion.default_case_status,
                    "history_retention_days": conversion.history_retention_days,
                },
                "taxonomy": None if taxonomy is None else {"source_number": taxonomy.number,
                                                               "definitions": taxonomy.definitions}}
    if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise HTTPException(413, "项目模板配置超过 256 KiB 上限")
    return snapshot


def _validate(snapshot: dict, digest: str, source_id: int, specs) -> tuple[dict, dict | None, dict | None]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "project-template.v1":
        raise HTTPException(503, "项目模板版本不可用")
    if snapshot.get("source_project_id") != source_id or _digest(snapshot) != digest:
        raise HTTPException(503, "项目模板来源或摘要校验失败")
    skills = snapshot.get("skills")
    available = {spec.skill_id: spec for spec in specs}
    if not isinstance(skills, list) or len(skills) > 100 or any(
        not isinstance(item, dict) or set(item) != {"skill_id", "enabled"}
        or not isinstance(item["skill_id"], str) or type(item["enabled"]) is not bool for item in skills
    ):
        raise HTTPException(503, "项目模板技能配置无效")
    enabled = {item["skill_id"]: item["enabled"] for item in skills}
    if len(enabled) != len(skills):
        raise HTTPException(503, "项目模板技能配置重复")
    if set(enabled) - set(available):
        raise HTTPException(409, "模板包含已移除的项目技能，请先核对技能仓")
    conversion = snapshot.get("conversion")
    if conversion is not None and (not isinstance(conversion, dict)
                                   or set(conversion) != {"default_case_type", "default_case_status", "history_retention_days"}
                                   or not isinstance(conversion["default_case_type"], str)
                                   or conversion["default_case_type"] not in conversion_service.CASE_TYPES
                                   or not isinstance(conversion["default_case_status"], str)
                                   or conversion["default_case_status"] not in conversion_service.CASE_STATUSES
                                   or type(conversion["history_retention_days"]) is not int
                                   or conversion["history_retention_days"] not in (30, 60, 180)):
        raise HTTPException(503, "项目模板转换配置无效")
    taxonomy = snapshot.get("taxonomy")
    if taxonomy is not None:
        if not isinstance(taxonomy, dict) or set(taxonomy) != {"source_number", "definitions"}:
            raise HTTPException(503, "项目模板分类配置无效")
        definitions = taxonomy["definitions"]
        if not isinstance(definitions, dict):
            raise HTTPException(503, "项目模板分类配置无效")
        try:
            if classification.normalize_definitions(definitions, definitions) != definitions:
                raise ValueError("non-canonical taxonomy")
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, "项目模板分类配置无效") from exc
        if any(not definitions.get(dimension) for dimension in classification.DIMENSIONS):
            raise HTTPException(503, "项目模板分类配置不完整")
    return enabled, conversion, taxonomy


def build_router(get_db, current_user, csrf, project_access, writable, project_manager) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/templates", tags=["project-templates"])

    @router.get("")
    def list_templates(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        rows = db.scalars(select(ProjectTemplate).where(ProjectTemplate.project_id == project_id)
                          .order_by(ProjectTemplate.id.desc()).limit(100)).all()
        return [_summary(item) for item in rows]

    @router.post("", dependencies=[Depends(csrf)])
    def save_template(project_id: int, payload: TemplateName,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_manager(user, project_id, db)
        writable(user, project_id, db, fence=True)
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "模板名称不能为空")
        if (db.scalar(select(func.count(ProjectTemplate.id)).where(ProjectTemplate.project_id == project_id)) or 0) >= 100:
            raise HTTPException(409, "项目模板数量已达上限")
        snapshot = _snapshot(project_id, db)
        try:
            specs = discover_skills()
        except SkillRunnerError as exc:
            raise HTTPException(503, "项目技能仓不可用") from exc
        digest = _digest(snapshot)
        _validate(snapshot, digest, project_id, specs)
        row = ProjectTemplate(project_id=project_id, name=name, snapshot=snapshot,
                              snapshot_sha256=digest, created_by=user.id)
        db.add(row); db.commit(); db.refresh(row)
        return _summary(row)

    @router.get("/origin")
    def template_origin(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        row = db.get(ProjectTemplateApplication, project_id)
        if row is None:
            return None
        return {"project_id": row.project_id, "source_project_id": row.source_project_id,
                "template_id": row.template_id, "template_name": row.template_name,
                "snapshot_sha256": row.snapshot_sha256, "actor_id": row.actor_id,
                "created_at": row.created_at}

    @router.get("/{template_id}")
    def get_template(project_id: int, template_id: int,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        row = db.get(ProjectTemplate, template_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "项目模板不存在")
        return {**_summary(row), "snapshot": row.snapshot}

    @router.post("/{template_id}/instantiate", dependencies=[Depends(csrf)])
    def create_from_template(project_id: int, template_id: int, payload: TemplateName,
                             user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_manager(user, project_id, db)
        project_access(project_id, user, db, fence=True)
        template = db.get(ProjectTemplate, template_id)
        if template is None or template.project_id != project_id:
            raise HTTPException(404, "项目模板不存在")
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "项目名称不能为空")
        try:
            specs = discover_skills()
        except SkillRunnerError as exc:
            raise HTTPException(503, "项目技能仓不可用") from exc
        enabled, conversion, taxonomy = _validate(template.snapshot, template.snapshot_sha256,
                                                   project_id, specs)
        project = Project(name=name)
        db.add(project); db.flush()
        db.add(Membership(project_id=project.id, user_id=user.id, role="manager"))
        db.add(MembershipEvent(project_id=project.id, user_id=user.id, actor_id=user.id,
                               action="added", after_role="manager"))
        for spec in specs:
            db.add(ProjectSkill(project_id=project.id, skill_id=spec.skill_id, name=spec.name,
                                label=spec.label, source_path=spec.path, description=spec.description,
                                enabled=enabled.get(spec.skill_id, False)))
        if conversion is not None:
            db.add(ConversionSettings(project_id=project.id, default_prefix_path="", **conversion))
        if taxonomy is not None:
            db.add(TaxonomyVersion(project_id=project.id, number=1, status="published", revision=1,
                                   definitions=taxonomy["definitions"], created_by=user.id, published_at=utcnow()))
        db.add(ProjectTemplateApplication(project_id=project.id, source_project_id=project_id,
                                          template_id=template.id, template_name=template.name,
                                          snapshot_sha256=template.snapshot_sha256, actor_id=user.id))
        db.commit(); db.refresh(project)
        return {"id": project.id, "name": project.name,
                "role": "admin" if user.role == "admin" else "manager", "created_at": project.created_at,
                "archived": False, "archived_at": None, "template": _summary(template)}

    return router
