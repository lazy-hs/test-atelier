"""HTTP API for the project-scoped case governance workspace."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from openpyxl import Workbook, load_workbook

from . import ai_engine
from .db import (Case, GovernanceChangeLog, GovernanceDirectory, GovernanceDuplicateGroup,
                 GovernanceDuplicateMember, GovernanceExportJob, GovernanceMetricSnapshot,
                 GovernanceValidationRun, ImportBatch, Membership, User, utcnow)
from .governance_core import (SEP, analyze, content_hash, fingerprint, load_rules, load_whitelist,
                              name_key, predict_domain, source_case_timestamps, split_dir, text, validate_rows)
from .importers import ImportErrorDetail, parse


class Decision(BaseModel):
    case_id: int
    decision: Literal["keep", "merge", "discard", "retired", "rename"]
    merge_into: int | None = None
    rename: str | None = Field(default=None, max_length=255)
    reason: str = Field(default="", max_length=1000)


class GroupDecisions(BaseModel):
    expected_revision: int
    decisions: list[Decision] = Field(min_length=1, max_length=100)


class ApplyRequest(BaseModel):
    group_ids: list[int] = Field(min_length=1, max_length=100)
    expected_revisions: dict[str, int] = Field(default_factory=dict)
    preview: bool = False


class ValidationRequest(BaseModel):
    filename: str = Field(default="new-cases.csv", max_length=255)
    rows: list[dict] = Field(min_length=1, max_length=10000)
    mode: Literal["new", "legacy"] = "new"


class MoveRequest(BaseModel):
    case_ids: list[int] = Field(min_length=1, max_length=100)
    directory: str = Field(min_length=1, max_length=1024)


class AiGovernanceRequest(BaseModel):
    provider: Literal["remote", "codex_cli", "claude_code"] = "codex_cli"


class CaseEditRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=1024)
    endpoint_type: str | None = Field(default=None, max_length=40)
    endpoint: str | None = Field(default=None, max_length=40)


class WhitelistRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    state: Literal["requested", "approved", "active"] = "requested"


def _row_dicts(headers: list[str], rows: list[list]) -> list[dict]:
    return [{header: (row[index] if index < len(row) and row[index] is not None else "") for index, header in enumerate(headers)} for row in rows]


def _parse_file(filename: str, data: bytes) -> tuple[list[str], list[list], list[str]]:
    if filename.lower().endswith(".csv"):
        try:
            text_data = data.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text_data))
            grid = [[cell or None for cell in row] for row in reader]
            if not grid:
                raise ImportErrorDetail("CSV 文件为空")
            headers = [str(value or "") for value in grid[0]]
            rows = [row for row in grid[1:] if any(row)]
            if "测试用例ID" not in headers or "用例名称" not in headers:
                raise ImportErrorDetail("缺少测试用例ID或用例名称列")
            ids = [row[headers.index("测试用例ID")] if headers.index("测试用例ID") < len(row) else None for row in rows]
            if any(not value for value in ids) or len(set(ids)) != len(ids):
                raise ImportErrorDetail("用例 ID 为空或在文件内重复")
            return headers, rows, []
        except UnicodeDecodeError as exc:
            raise ImportErrorDetail("CSV 必须使用 UTF-8 编码") from exc
    return parse(filename, data)


def _source_root() -> Path:
    path = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")) / "governance-snapshots"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _manager(project_id: int, user: User, db: Session, project_role) -> None:
    role = project_role(project_id, user, db)
    if role not in ("admin", "manager"):
        raise HTTPException(403, "仅项目管理员可执行治理批量操作")


def _case_row(case: Case) -> dict:
    return {"id": case.id, "platform_id": case.platform_id or case.original_id, "name": case.title,
            "name_key": case.name_key, "directory": case.directory, "domain": case.domain,
            "subdomain": case.subdomain, "level": case.level, "status": case.governance_status,
            "source_status": next((str(item[1]) for item in zip([], []) if item), ""),
            "steps": case.steps or "", "precondition": case.precondition or "", "expected": case.expected or "",
            "endpoint_type": case.endpoint or "",
            "remark": case.remark or "", "fingerprint": case.fingerprint, "merged_into": case.merged_into,
            "created_at": case.created_at, "updated_at": case.updated_at}


def _rows_for_project(project_id: int, db: Session, include_retired: bool = True) -> list[dict]:
    query = select(Case).where(Case.project_id == project_id)
    if not include_retired:
        query = query.where(Case.governance_status == "active")
    return [{"测试用例ID": case.platform_id or case.original_id, "用例名称": case.title, "用例目录": case.directory,
             "用例类型": (case.original or ["", "", ""])[2] if len(case.original or []) > 2 else "",
             "用例状态": (case.original or ["", "", "", "正常"])[3] if len(case.original or []) > 3 else "正常",
             "用例等级": case.level, "创建人": (case.original or ["", "", "", "", "", ""])[5] if len(case.original or []) > 5 else "",
             "用例步骤": case.steps or "", "前置条件": case.precondition or "", "预期结果": case.expected or "",
             "备注": case.remark or "", "是否冒烟用例": case.smoke, "端类型": case.endpoint or "",
             "创建时间": case.created_at, "最后修改时间": case.updated_at}
            for case in db.scalars(query.order_by(Case.id)).all()]


def _sync_groups(project_id: int, batch_id: int, rows: list[dict], db: Session) -> dict:
    old_ids = db.scalars(select(GovernanceDuplicateGroup.id).where(GovernanceDuplicateGroup.project_id == project_id)).all()
    if old_ids:
        db.execute(delete(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id.in_(old_ids)))
        db.execute(delete(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.id.in_(old_ids)))
    by_case = {case.platform_id or case.original_id: case for case in db.scalars(select(Case).where(Case.project_id == project_id)).all()}
    # Retired/merged cases are historical governance results, not candidates
    # for another duplicate recommendation after a snapshot is re-imported.
    active_rows = [row for row in rows if by_case.get(text(row.get("测试用例ID")), None) is None or by_case[text(row.get("测试用例ID"))].governance_status == "active"]
    result = analyze(active_rows)
    by_platform = {text(row.get("测试用例ID")): row for row in active_rows}
    for group in result["groups"]:
        record = GovernanceDuplicateGroup(project_id=project_id, batch_id=batch_id, gid=group["gid"], name_key=group["name_key"], name=group["name"],
                                          size=group["size"], cluster_count=group["cluster_count"], category=group["category"], suggested_discard_count=group["suggested_discard_count"])
        db.add(record); db.flush()
        for member in group["members"]:
            case = by_case.get(member["case_id"])
            if case:
                db.add(GovernanceDuplicateMember(group_id=record.id, case_id=case.id, cluster_fp=member["fingerprint"], score=member["score"], decision=member["decision"], reason=member.get("reason", "")))
    return result


def _metrics(project_id: int, batch_id: int, result: dict, db: Session) -> None:
    for key, value in (("case_count", result["case_count"]), ("directory_count", result["directory_count"]),
                       ("duplicate_group_count", result["duplicate_group_count"]), ("duplicate_case_count", result["duplicate_case_count"]),
                       ("fingerprint_cluster_count", result["fingerprint_cluster_count"]), ("discard_count", result["discard_count"])):
        db.add(GovernanceMetricSnapshot(project_id=project_id, batch_id=batch_id, key=key, value=value))


def _serialize_group(group: GovernanceDuplicateGroup, db: Session) -> dict:
    members = db.scalars(select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id).order_by(GovernanceDuplicateMember.id)).all()
    cases = {case.id: case for case in db.scalars(select(Case).where(Case.id.in_([member.case_id for member in members]))).all()}
    return {"id": group.id, "gid": group.gid, "name": group.name, "name_key": group.name_key, "size": group.size,
            "cluster_count": group.cluster_count, "category": group.category, "status": group.status, "revision": group.revision,
            "suggested_discard_count": group.suggested_discard_count,
            "members": [{"case_id": member.case_id, "cluster_fp": member.cluster_fp, "score": member.score, "decision": member.decision, "reason": member.reason,
                         "case": _case_row(cases[member.case_id])} for member in members if member.case_id in cases]}


def build_router(get_db, current_user, csrf, project_access, writable, project_role):
    router = APIRouter(prefix="/api/v1/projects/{project_id}/governance", tags=["case-governance"])

    @router.post("/imports/preview", dependencies=[Depends(csrf)])
    async def preview(project_id: int, file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); writable(user, project_id, db)
        if not file.filename or file.filename.lower().rsplit(".", 1)[-1] not in {"xls", "xlsx", "csv"}:
            raise HTTPException(400, "治理导入仅支持 xls、xlsx 和 csv")
        data = await file.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024: raise HTTPException(400, "文件不能超过 20 MB")
        digest = hashlib.sha256(data).hexdigest()
        duplicate = db.scalar(select(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.kind == "governance", ImportBatch.sha256 == digest))
        if duplicate: raise HTTPException(409, "相同文件已经导入过")
        try: headers, rows, warnings = _parse_file(file.filename, data)
        except ImportErrorDetail as exc: raise HTTPException(400, str(exc)) from exc
        source_rows = _row_dicts(headers, rows)
        rules, rules_hash = load_rules(); whitelist, whitelist_hash = load_whitelist()
        # A source snapshot is a historical baseline.  Strict required-tag and
        # whitelist gates apply to M3 new-case validation, not baseline import.
        validation = validate_rows(source_rows[: min(100, len(source_rows))], _rows_for_project(project_id, db), mode="legacy", rules=rules, whitelist=whitelist)
        previous = db.scalar(select(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.kind == "governance", ImportBatch.status == "done").order_by(ImportBatch.id.desc()))
        batch = ImportBatch(project_id=project_id, filename=file.filename[:255], headers=headers, rows=rows, warnings=warnings,
                            status="preview", kind="governance", sha256=digest, source_sheet="Tcase_list", rules_hash=rules_hash, row_count=len(rows))
        db.add(batch); db.commit()
        snapshot = _source_root() / f"{digest}.{file.filename.rsplit('.', 1)[-1].lower()}"
        snapshot.write_bytes(data)
        try: os.chmod(snapshot, 0o600)
        except OSError: pass
        batch.snapshot_path = str(snapshot); db.commit()
        return {"id": batch.id, "filename": batch.filename, "sha256": digest, "headers": headers, "count": len(rows),
                "sample": rows[:5], "warnings": warnings, "rules_hash": rules_hash, "whitelist_hash": whitelist_hash,
                "validation": validation, "previous_batch_id": previous.id if previous else None}

    @router.post("/imports/{batch_id}/confirm", dependencies=[Depends(csrf)])
    def confirm(project_id: int, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); writable(user, project_id, db)
        batch = db.get(ImportBatch, batch_id)
        if not batch or batch.project_id != project_id or batch.kind != "governance": raise HTTPException(404, "治理导入批次不存在")
        if batch.status == "done": return {"count": batch.row_count or len(batch.rows), "status": "done", "batch_id": batch.id}
        headers, source_rows = batch.headers, _row_dicts(batch.headers, batch.rows)
        rules, _ = load_rules(); whitelist, _ = load_whitelist()
        checked = validate_rows(source_rows, _rows_for_project(project_id, db), mode="legacy", rules=rules, whitelist=whitelist)
        if checked["blocked"]: raise HTTPException(422, detail={"message": "存在阻断项", "validation": checked})
        known = {case.platform_id or case.original_id: case for case in db.scalars(select(Case).where(Case.project_id == project_id)).all()}
        for row in source_rows:
            cid = text(row.get("测试用例ID")); old = known.get(cid)
            source_created, source_updated = source_case_timestamps(row, None)
            values = dict(original_id=cid, title=text(row.get("用例名称")), directory=text(row.get("用例目录")), level=text(row.get("用例等级")), smoke=text(row.get("是否冒烟用例")), original=[row.get(header) for header in headers],
                          platform_id=cid, name_key=name_key(row.get("用例名称")), governance_status="active", domain=predict_domain(row.get("用例目录")),
                          **({"created_at": source_created, "updated_at": source_updated} if source_created or source_updated else {}),
                          subdomain=split_dir(row.get("用例目录"))[-1] if split_dir(row.get("用例目录")) else "", endpoint=text(row.get("端类型")), steps=text(row.get("用例步骤")),
                          precondition=text(row.get("前置条件")), expected=text(row.get("预期结果")), remark=text(row.get("备注")), fingerprint=fingerprint(row), content_hash=content_hash(row), import_id=batch.id)
            if old:
                for key, value in values.items(): setattr(old, key, value)
                # A source re-import is not an edit when it has no timestamp
                # columns; preserve the persisted case times in that case.
            else:
                db.add(Case(project_id=project_id, **values))
        batch.status = "done"; db.flush()
        # Include persisted metadata in the scoring input so duplicate
        # recommendations account for timestamps as well as source fields.
        persisted = {case.platform_id or case.original_id: case for case in db.scalars(select(Case).where(Case.project_id == project_id)).all()}
        for row in source_rows:
            case = persisted.get(text(row.get("测试用例ID")))
            if case:
                row["创建时间"], row["最后修改时间"] = case.created_at, case.updated_at
        result = _sync_groups(project_id, batch.id, source_rows, db); _metrics(project_id, batch.id, result, db)
        db.commit()
        return {"count": len(source_rows), "status": "done", "batch_id": batch.id, "metrics": {key: value for key, value in result.items() if key != "groups"}}

    @router.get("/imports")
    def imports(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        return [{"id": row.id, "filename": row.filename, "count": row.row_count or len(row.rows), "status": row.status, "sha256": row.sha256, "created_at": row.created_at}
                for row in db.scalars(select(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.kind == "governance").order_by(ImportBatch.id.desc())).all()]

    @router.get("/imports/{batch_id}/diff")
    def diff(project_id: int, batch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); batch = db.get(ImportBatch, batch_id)
        if not batch or batch.project_id != project_id or batch.kind != "governance": raise HTTPException(404, "治理导入批次不存在")
        current = {text(row.get("测试用例ID")): row for row in _row_dicts(batch.headers, batch.rows)}
        previous = db.scalar(select(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.kind == "governance", ImportBatch.id < batch_id, ImportBatch.status == "done").order_by(ImportBatch.id.desc()))
        old = {text(row.get("测试用例ID")): row for row in _row_dicts(previous.headers, previous.rows)} if previous else {}
        return {"added": sorted(set(current) - set(old)), "removed": sorted(set(old) - set(current)), "changed": sorted(cid for cid in set(current) & set(old) if current[cid] != old[cid]), "previous_batch_id": previous.id if previous else None}

    @router.get("/dashboard")
    def dashboard(project_id: int, batch: int | None = Query(default=None), user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        case_count = db.scalar(select(func.count(Case.id)).where(Case.project_id == project_id)) or 0
        # A full case deletion invalidates the previous governance snapshot. Do
        # not surface historical metrics as if they still described the project.
        if case_count == 0:
            return {"batch": None, "metrics": {key: 0 for key in (
                "case_count", "directory_count", "duplicate_group_count", "duplicate_case_count",
                "fingerprint_cluster_count", "multi_cluster_group_count", "discard_count")}, "status_counts": {}}
        batch_row = db.get(ImportBatch, batch) if batch else db.scalar(select(ImportBatch).where(ImportBatch.project_id == project_id, ImportBatch.kind == "governance", ImportBatch.status == "done").order_by(ImportBatch.id.desc()))
        if not batch_row: return {"batch": None, "metrics": {}, "status_counts": {}}
        values = {row.key: row.value for row in db.scalars(select(GovernanceMetricSnapshot).where(GovernanceMetricSnapshot.project_id == project_id, GovernanceMetricSnapshot.batch_id == batch_row.id)).all()}
        statuses = dict(db.execute(select(Case.governance_status, func.count(Case.id)).where(Case.project_id == project_id).group_by(Case.governance_status)).all())
        return {"batch": {"id": batch_row.id, "filename": batch_row.filename, "sha256": batch_row.sha256, "created_at": batch_row.created_at}, "metrics": values, "status_counts": statuses}

    @router.get("/rules")
    def rules(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        # The rules are intentionally read-only through this API.
        project_access(project_id, user, db)
        rules_value, rules_hash = load_rules(); whitelist, whitelist_hash = load_whitelist()
        return {"rules": rules_value, "rules_hash": rules_hash, "whitelist_hash": whitelist_hash, "whitelist": sorted(whitelist)}

    @router.post("/retry", dependencies=[Depends(csrf)])
    def retry_governance(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Reopen applied governance groups for another review pass."""
        _manager(project_id, user, db, project_role)
        groups = db.scalars(select(GovernanceDuplicateGroup).where(
            GovernanceDuplicateGroup.project_id == project_id,
            GovernanceDuplicateGroup.status == "applied")).all()
        case_ids = [member.case_id for group in groups for member in db.scalars(
            select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id)).all()]
        if case_ids:
            for case in db.scalars(select(Case).where(Case.id.in_(set(case_ids)))).all():
                case.governance_status, case.merged_into = "active", None
                case.updated_at = utcnow()
        for group in groups:
            group.status, group.revision = "pending", group.revision + 1
            group.decided_by, group.decided_at = None, None
        db.commit()
        return {"groups": len(groups), "cases": len(set(case_ids))}

    @router.get("/duplicates")
    def duplicates(project_id: int, category: str = "all", status: str = "pending", q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); query = select(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.project_id == project_id)
        if category in {"A", "B", "C"}: query = query.where(GovernanceDuplicateGroup.category == category)
        if status != "all": query = query.where(GovernanceDuplicateGroup.status == status)
        if q: query = query.where(GovernanceDuplicateGroup.name.contains(q))
        groups = db.scalars(query.order_by(GovernanceDuplicateGroup.category, GovernanceDuplicateGroup.size.desc(), GovernanceDuplicateGroup.id)).all()
        return {"total": len(groups), "items": [{key: value for key, value in _serialize_group(group, db).items() if key != "members"} for group in groups]}

    @router.get("/duplicates/{group_id}")
    def duplicate_detail(project_id: int, group_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); group = db.get(GovernanceDuplicateGroup, group_id)
        if not group or group.project_id != project_id: raise HTTPException(404, "重复组不存在")
        return _serialize_group(group, db)

    @router.put("/cases/{case_id}", dependencies=[Depends(csrf)])
    def edit_case(project_id: int, case_id: int, payload: CaseEditRequest,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        """Edit governance-owned dimensions while retaining the source row and audit trail."""
        _manager(project_id, user, db, project_role)
        case = db.get(Case, case_id)
        if not case or case.project_id != project_id:
            raise HTTPException(404, "治理用例不存在")
        rules, _ = load_rules()
        endpoint_type = (payload.endpoint_type or payload.endpoint or "").strip()
        allowed = set(rules.get("endpoint_values", []))
        if not endpoint_type:
            raise HTTPException(422, "端类型不能为空")
        if allowed and endpoint_type not in allowed:
            raise HTTPException(422, "端类型不在允许集合")
        directory = payload.directory.strip()
        if not directory.startswith(text(rules.get("root"))):
            raise HTTPException(422, "目录不在监控中心模块下")
        before = {"directory": case.directory, "endpoint_type": case.endpoint or ""}
        after = {"directory": directory, "endpoint_type": endpoint_type}
        changed = before != after
        if changed:
            case.directory = directory
            case.endpoint = endpoint_type
            case.domain = predict_domain(directory)
            parts = split_dir(directory)
            case.subdomain = parts[-1] if parts else ""
            case.updated_at = utcnow()
            batch = db.get(ImportBatch, case.import_id)
            if batch and case.original:
                source = list(case.original)
                for field, value in (("用例目录", directory), ("端类型", endpoint_type)):
                    if field in batch.headers:
                        index = batch.headers.index(field)
                        while len(source) <= index:
                            source.append("")
                        source[index] = value
                case.original = source
        batch_key = uuid.uuid4().hex
        if changed:
            db.add(GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case.id), action="edit",
                                       before_json=before, after_json=after, batch_key=batch_key, operator_id=user.id))
        db.commit(); db.refresh(case)
        return {"case": _case_row(case), "batch_key": batch_key, "changed": changed}

    @router.post("/duplicates/{group_id}/decide", dependencies=[Depends(csrf)])
    def decide(project_id: int, group_id: int, payload: GroupDecisions, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); group = db.get(GovernanceDuplicateGroup, group_id)
        if not group or group.project_id != project_id: raise HTTPException(404, "重复组不存在")
        if group.revision != payload.expected_revision: raise HTTPException(409, "重复组已被他人修改，请刷新")
        members = {row.case_id: row for row in db.scalars(select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id)).all()}
        results = []
        batch_key = uuid.uuid4().hex
        for item in payload.decisions:
            member = members.get(item.case_id)
            if not member: results.append({"case_id": item.case_id, "status": "not_found"}); continue
            before = {"decision": member.decision, "reason": member.reason}
            member.decision, member.reason = item.decision, item.reason; results.append({"case_id": item.case_id, "status": "saved"})
            db.add(GovernanceChangeLog(project_id=project_id, entity="duplicate-member", entity_id=str(member.case_id),
                                       action="decide", before_json=before,
                                       after_json={"decision": member.decision, "reason": member.reason},
                                       batch_key=batch_key, operator_id=user.id))
        group.revision += 1; group.decided_by = user.id; group.decided_at = utcnow(); db.commit()
        return {"group": _serialize_group(group, db), "results": results, "batch_key": batch_key}

    @router.post("/duplicates/{group_id}/ai", dependencies=[Depends(csrf)])
    def ai_group(project_id: int, group_id: int, payload: AiGovernanceRequest,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
        _manager(project_id, user, db, project_role)
        group = db.get(GovernanceDuplicateGroup, group_id)
        if not group or group.project_id != project_id:
            raise HTTPException(404, "重复组不存在")
        if payload.provider == "remote" and (user.role != "admin" or not ai_engine.REMOTE_ENABLED):
            raise HTTPException(403, "远程模型未由管理员启用")
        if payload.provider == "codex_cli" and not ai_engine.CODEX_ENABLED:
            raise HTTPException(403, "Codex CLI 未由管理员启用")
        if payload.provider == "claude_code" and not ai_engine.CLAUDE_ENABLED:
            raise HTTPException(403, "Claude Code 未由管理员启用")
        members = db.scalars(select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id)).all()
        cases = {case.id: case for case in db.scalars(select(Case).where(Case.id.in_([item.case_id for item in members]))).all()}
        prompt = "治理评分规则\n请根据目录完整性、创建时间、最后修改时间、状态、等级、前置条件、预期结果及内容相似度，对以下重复用例给出保留、合并或废弃建议。时间越新的用例优先级越高。仅返回 JSON 数组，每项包含 case_id、decision、reason。\n" + json.dumps([{"case_id": item.case_id, "name": cases[item.case_id].title, "directory": cases[item.case_id].directory, "created_at": cases[item.case_id].created_at, "updated_at": cases[item.case_id].updated_at, "level": cases[item.case_id].level, "steps": cases[item.case_id].steps, "expected": cases[item.case_id].expected} for item in members if item.case_id in cases], ensure_ascii=False)
        schema = {"type": "array", "items": {"type": "object", "required": ["case_id", "decision", "reason"], "properties": {"case_id": {"type": "integer"}, "decision": {"type": "string", "enum": ["keep", "merge", "discard", "retired"]}, "reason": {"type": "string"}}}}
        try:
            output = ai_engine.request_structured(prompt, schema, payload.provider)
        except ai_engine.StructuredOutputError as exc:
            # Preserve the model's original response so operators can inspect
            # or manually reuse it when strict parsing is not compatible.
            return {"changed": 0, "fallback": True, "raw_output": exc.raw_output,
                    "message": "模型返回无法兼容，已保留原始输出", "group": _serialize_group(group, db)}
        except Exception as exc:
            raise HTTPException(502, f"AI 治理失败：{exc}") from exc
        if not isinstance(output, list):
            return {"changed": 0, "fallback": True, "raw_output": json.dumps(output, ensure_ascii=False),
                    "message": "模型返回格式无法兼容，已保留原始输出", "group": _serialize_group(group, db)}
        allowed = {item.case_id for item in members}
        valid = [item for item in output if isinstance(item, dict) and item.get("case_id") in allowed and item.get("decision") in {"keep", "merge", "discard", "retired"} and text(item.get("reason"))]
        if not valid or len(valid) != len(output):
            return {"changed": 0, "fallback": True, "raw_output": json.dumps(output, ensure_ascii=False),
                    "message": "模型返回内容无法兼容，已保留原始输出", "group": _serialize_group(group, db)}
        changed = 0
        for item in valid:
            member = next(member for member in members if member.case_id == int(item["case_id"]))
            member.decision, member.reason = item["decision"], text(item["reason"])
            changed += 1
        group.status, group.revision = "pending", group.revision + 1
        db.commit()
        return {"changed": changed, "group": _serialize_group(group, db)}

    @router.post("/duplicates/apply", dependencies=[Depends(csrf)])
    def apply(project_id: int, payload: ApplyRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
        _manager(project_id, user, db, project_role)
        groups = [db.get(GovernanceDuplicateGroup, gid) for gid in payload.group_ids]
        if any(not group or group.project_id != project_id for group in groups): raise HTTPException(404, "重复组不存在")
        conflicts = [group.id for group in groups if str(group.id) in payload.expected_revisions and group.revision != payload.expected_revisions[str(group.id)]]
        if conflicts: raise HTTPException(409, detail={"message": "重复组已被他人修改，请刷新", "groups": conflicts})
        members = [member for group in groups for member in db.scalars(select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id)).all()]
        counts = {"keep": sum(member.decision == "keep" for member in members), "retire": sum(member.decision in {"merge", "discard", "retired"} for member in members)}
        if payload.preview: return {"preview": True, "groups": len(groups), "counts": counts}
        batch_key = uuid.uuid4().hex; changed = 0
        for member in members:
            case = db.get(Case, member.case_id)
            if not case: continue
            before = {"governance_status": case.governance_status, "merged_into": case.merged_into, "title": case.title}
            if member.decision == "keep": case.governance_status, case.merged_into = "active", None
            elif member.decision == "merge": case.governance_status, case.merged_into = "retired", next((item.case_id for item in members if item.group_id == member.group_id and item.decision == "keep"), None)
            else: case.governance_status, case.merged_into = "retired", None
            case.updated_at = utcnow()
            after = {"governance_status": case.governance_status, "merged_into": case.merged_into, "title": case.title}
            if before != after:
                db.add(GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case.id), action=member.decision, before_json=before, after_json=after, batch_key=batch_key, operator_id=user.id)); changed += 1
        for group in groups: group.status = "applied"; group.revision += 1; group.decided_by = user.id; group.decided_at = utcnow()
        db.commit(); return {"batch_key": batch_key, "groups": len(groups), "changed": changed, "counts": counts}

    @router.post("/duplicates/{group_id}/undo", dependencies=[Depends(csrf)])
    def undo(project_id: int, group_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        _manager(project_id, user, db, project_role); group = db.get(GovernanceDuplicateGroup, group_id)
        if not group or group.project_id != project_id: raise HTTPException(404, "重复组不存在")
        case_ids = [str(member.case_id) for member in db.scalars(select(GovernanceDuplicateMember).where(GovernanceDuplicateMember.group_id == group.id)).all()]
        logs = db.scalars(select(GovernanceChangeLog).where(GovernanceChangeLog.project_id == project_id, GovernanceChangeLog.entity == "case", GovernanceChangeLog.entity_id.in_(case_ids)).order_by(GovernanceChangeLog.id.desc())).all()
        restored = 0
        for log in logs:
            case = db.get(Case, int(log.entity_id))
            if case and case.governance_status == log.after_json.get("governance_status"):
                case.governance_status = log.before_json.get("governance_status", "active"); case.merged_into = log.before_json.get("merged_into"); case.title = log.before_json.get("title", case.title); case.updated_at = utcnow(); restored += 1
        group.status = "pending"; group.revision += 1; db.commit(); return {"restored": restored, "group_id": group_id}

    @router.get("/change-log")
    def change_log(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        return [{"id": item.id, "entity": item.entity, "entity_id": item.entity_id, "action": item.action, "batch_key": item.batch_key, "before": item.before_json, "after": item.after_json, "ts": item.ts} for item in db.scalars(select(GovernanceChangeLog).where(GovernanceChangeLog.project_id == project_id).order_by(GovernanceChangeLog.id.desc()).limit(200)).all()]

    @router.post("/validations", dependencies=[Depends(csrf)])
    def validation(project_id: int, payload: ValidationRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); writable(user, project_id, db); rules, _ = load_rules(); whitelist, _ = load_whitelist()
        result = validate_rows(payload.rows, _rows_for_project(project_id, db), mode=payload.mode, rules=rules, whitelist=whitelist)
        run = GovernanceValidationRun(project_id=project_id, filename=payload.filename, rows=payload.rows, result=result, status="ready" if result["can_promote"] else "blocked", created_by=user.id)
        db.add(run); db.commit(); return {"id": run.id, "status": run.status, **result}

    @router.get("/validations/{run_id}")
    def validation_detail(project_id: int, run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); run = db.get(GovernanceValidationRun, run_id)
        if not run or run.project_id != project_id: raise HTTPException(404, "校验运行不存在")
        return {"id": run.id, "filename": run.filename, "status": run.status, "rows": run.rows, **run.result}

    @router.post("/validations/{run_id}/promote", dependencies=[Depends(csrf)])
    def promote_validation(project_id: int, run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); writable(user, project_id, db)
        run = db.get(GovernanceValidationRun, run_id)
        if not run or run.project_id != project_id: raise HTTPException(404, "校验运行不存在")
        if run.status != "ready": raise HTTPException(422, "校验仍有阻断项")
        rules, _ = load_rules(); whitelist, _ = load_whitelist()
        checked = validate_rows(run.rows, _rows_for_project(project_id, db), rules=rules, whitelist=whitelist)
        if checked["blocked"]: run.result, run.status = checked, "blocked"; db.commit(); raise HTTPException(422, detail=checked)
        batch = ImportBatch(project_id=project_id, filename=run.filename, headers=list(run.rows[0].keys()), rows=[[row.get(key, "") for key in run.rows[0].keys()] for row in run.rows], warnings=[], status="done", kind="governance-new", source_sheet="Tcase_list", row_count=len(run.rows), rules_hash=load_rules()[1])
        db.add(batch); db.flush()
        for row in run.rows:
            generated_id = text(row.get("测试用例ID")) or f"PENDING-{uuid.uuid4().hex[:12]}"
            source_created, source_updated = source_case_timestamps(row, batch.created_at)
            values = dict(original_id=generated_id, title=text(row.get("用例名称")), directory=text(row.get("用例目录")), level=text(row.get("用例等级")), smoke=text(row.get("是否冒烟用例")), original=list(row.values()), created_at=source_created, updated_at=source_updated, platform_id=text(row.get("测试用例ID")) or None, name_key=name_key(row.get("用例名称")), governance_status="pending_id" if not text(row.get("测试用例ID")) else "active", domain=predict_domain(row.get("用例目录")), subdomain=split_dir(row.get("用例目录"))[-1] if split_dir(row.get("用例目录")) else "", endpoint=text(row.get("端类型")), steps=text(row.get("用例步骤")), precondition=text(row.get("前置条件")), expected=text(row.get("预期结果")), remark=text(row.get("备注")), fingerprint=fingerprint(row), content_hash=content_hash(row), import_id=batch.id)
            db.add(Case(project_id=project_id, **values))
        run.status = "promoted"; db.commit(); return {"batch_id": batch.id, "count": len(run.rows), "status": run.status}

    @router.get("/directories/tree")
    def directories(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); whitelist, _ = load_whitelist(); paths = set(whitelist) | {text(row[0]) for row in db.execute(select(Case.directory).where(Case.project_id == project_id)).all() if text(row[0])}
        return [{"path": path, "level": len(split_dir(path)), "whitelisted": path in whitelist,
                 "frozen": any(token in path for token in ("迭代", "阶段", "废弃")),
                 "case_count": db.scalar(select(func.count(Case.id)).where(Case.project_id == project_id, Case.directory == path)) or 0,
                 "case_ids": db.scalars(select(Case.id).where(Case.project_id == project_id, Case.directory == path).order_by(Case.id)).all()} for path in sorted(paths)]

    @router.post("/directories/move", dependencies=[Depends(csrf)])
    def move(project_id: int, payload: MoveRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
        _manager(project_id, user, db, project_role); whitelist, _ = load_whitelist()
        if whitelist and payload.directory not in whitelist: raise HTTPException(422, "目标目录不在白名单")
        changed = 0; batch_key = uuid.uuid4().hex
        for case in db.scalars(select(Case).where(Case.project_id == project_id, Case.id.in_(payload.case_ids))).all():
            before = {"directory": case.directory}; case.directory = payload.directory; case.domain = predict_domain(payload.directory); case.subdomain = split_dir(payload.directory)[-1] if split_dir(payload.directory) else ""; case.updated_at = utcnow()
            db.add(GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case.id), action="move", before_json=before, after_json={"directory": payload.directory}, batch_key=batch_key, operator_id=user.id)); changed += 1
        db.commit(); return {"changed": changed, "batch_key": batch_key}

    @router.get("/directories/migrations")
    def migrations(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        rows = db.scalars(select(Case).where(Case.project_id == project_id, Case.governance_status != "retired")).all()
        return [{"case_id": case.id, "platform_id": case.platform_id or case.original_id, "source": case.directory,
                 "target": predict_domain(case.directory), "status": "suggested" if predict_domain(case.directory) != case.domain else "mapped"}
                for case in rows if predict_domain(case.directory) != case.domain]

    @router.post("/directories/whitelist/apply", dependencies=[Depends(csrf)])
    def whitelist_apply(project_id: int, payload: WhitelistRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
        _manager(project_id, user, db, project_role)
        batch_key = uuid.uuid4().hex
        changed = 0
        for path in {text(value) for value in payload.paths if text(value)}:
            item = db.get(GovernanceDirectory, {"project_id": project_id, "path": path})
            before = {"status": item.status, "whitelisted": item.whitelisted} if item else {}
            if not item:
                parts = split_dir(path)
                item = GovernanceDirectory(project_id=project_id, path=path, parent=SEP.join(parts[:-1]), level=len(parts),
                                            domain=predict_domain(path), subdomain=parts[-1] if parts else "")
                db.add(item)
            item.status = payload.state
            item.whitelisted = payload.state == "active"
            db.add(GovernanceChangeLog(project_id=project_id, entity="directory", entity_id=path, action="whitelist",
                                       before_json=before, after_json={"status": item.status, "whitelisted": item.whitelisted},
                                       batch_key=batch_key, operator_id=user.id))
            changed += 1
        db.commit(); return {"changed": changed, "state": payload.state, "batch_key": batch_key}

    @router.get("/regression")
    def regression(project_id: int, iteration: str = "", level: str = "", domain: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); rows = _rows_for_project(project_id, db, include_retired=False)
        selected = [row for row in rows if (not level or text(row.get("用例等级")) in set(level.split(","))) and (not domain or domain in text(row.get("用例目录"))) and (not iteration or f"[迭代:{iteration}]" in text(row.get("备注")))]
        return {"total": len(selected), "items": selected}

    @router.post("/exports")
    def export_cases(project_id: int, format: Literal["xlsx", "xls"] = "xlsx", iteration: str = "", level: str = "", domain: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        rows = regression(project_id, iteration, level, domain, user, db)["items"]
        headers = ["测试用例ID", "用例名称", "用例类型", "用例状态", "用例等级", "创建人", "用例目录", "用例步骤", "前置条件", "预期结果", "备注", "是否冒烟用例", "端类型", "创建时间", "最后修改时间"]
        output = io.BytesIO()
        if format == "xls":
            try:
                import xlwt
            except ImportError as exc:
                raise HTTPException(503, "BIFF8 导出依赖 xlwt 尚未安装") from exc
            book_xls = xlwt.Workbook(); sheet_xls = book_xls.add_sheet("Tcase_list")
            for column, header in enumerate(headers): sheet_xls.write(0, column, header)
            for row_number, row in enumerate(rows, 1):
                for column, header in enumerate(headers): sheet_xls.write(row_number, column, row.get(header, ""))
            book_xls.save(output); payload = output.getvalue(); media_type = "application/vnd.ms-excel"; suffix = "xls"
        else:
            book = Workbook(); sheet = book.active; sheet.title = "Tcase_list"; sheet.append(headers)
            for row in rows: sheet.append([row.get(header, "") for header in headers])
            book.save(output); payload = output.getvalue(); media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"; suffix = "xlsx"
        digest = uuid.uuid4().hex; destination = _source_root() / f"export-{digest}.{suffix}"; destination.write_bytes(payload)
        selfcheck = {"sheet": "Tcase_list", "headers": headers, "rows": len(rows), "format": format, "readback": True}
        try:
            if format == "xls":
                import xlrd
                check_sheet = xlrd.open_workbook(file_contents=payload).sheet_by_name("Tcase_list")
                selfcheck["readback_rows"] = max(0, check_sheet.nrows - 1); selfcheck["readback_headers"] = check_sheet.row_values(0)
            else:
                check_book = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
                check_sheet = check_book["Tcase_list"]; selfcheck["readback_rows"] = max(0, check_sheet.max_row - 1); selfcheck["readback_headers"] = [cell.value for cell in next(check_sheet.iter_rows(max_row=1))]; check_book.close()
        except Exception as exc:
            selfcheck = {**selfcheck, "readback": False, "error": type(exc).__name__}
        job = GovernanceExportJob(project_id=project_id, kind="cases", format=format, path=str(destination), rows=len(rows), selfcheck=selfcheck, created_by=user.id); db.add(job); db.commit()
        return {"id": job.id, "filename": destination.name, "rows": len(rows), "selfcheck": selfcheck, "download": f"/api/v1/projects/{project_id}/governance/exports/{job.id}/download"}

    @router.get("/exports/{export_id}/selfcheck")
    def export_selfcheck(project_id: int, export_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); job = db.get(GovernanceExportJob, export_id)
        if not job or job.project_id != project_id: raise HTTPException(404, "导出任务不存在")
        return {"id": job.id, **job.selfcheck}

    @router.get("/exports/{export_id}/download")
    def export_download(project_id: int, export_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db); job = db.get(GovernanceExportJob, export_id)
        if not job or job.project_id != project_id or not Path(job.path).is_file(): raise HTTPException(404, "导出文件不存在")
        media_type = "application/vnd.ms-excel" if job.format == "xls" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return StreamingResponse(Path(job.path).open("rb"), media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{Path(job.path).name}"'})

    return router

