"""Bounded, verifiable project evidence bundle (not a database backup)."""

import hashlib
import os
import re
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from . import ai_routes, conversion_routes
from .db import (AnalysisRun, Case, CaseComment, CaseDraft, CaseSuggestion, ConversionRecord,
                 ExecutionDefect, ExecutionDefectEvent, ExecutionEvent, ExecutionReport,
                 ExecutionRun, GovernanceChangeLog, GovernanceDirectory, GovernanceDuplicateGroup,
                 GovernanceDuplicateMember, GovernanceExportJob, GovernanceMetricSnapshot,
                 GovernanceValidationRun, HarnessTask, ImportBatch,
                 KnowledgeEntry, KnowledgeRevision,
                 Project, ProjectTemplateApplication, ReviewEvent, RunCase, SmokeItem, SmokeSuite,
                 SourceDocument, SuggestionJob, TaxonomyVersion, User, utcnow)
from .execution_defect_routes import _snapshot
from .execution_report_core import canonical_json
from .observability import env_int
from .read_snapshot import consistent_read
from .storage import open_private_regular_file


MAX_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_LINKS = 50000
SAFE_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
MEDIA_TYPE = "application/zip"
_active_exports = threading.BoundedSemaphore(2)


def _file_name(kind: str, value: int | str) -> str:
    identifier = str(value)
    if not SAFE_ID.fullmatch(identifier):
        raise HTTPException(503, "产物标识无法安全导出")
    return f"{kind}/{identifier}.json"


def _binary_name(kind: str, value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise HTTPException(503, "产物标识无法安全导出")
    return f"{kind}/{value}.bin"


def _directory_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _governance_file(path: str, project_id: int, *, export: bool) -> tuple[Path, Path]:
    root = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")) / "governance-snapshots"
    candidate = Path(path)
    filename = (r"export-[a-f0-9]{32}\.(?:xls|xlsx)" if export
                else r"[a-f0-9]{64}\.(?:xls|xlsx|csv)")
    if candidate.parent != root / str(project_id) or not re.fullmatch(filename, candidate.name):
        raise HTTPException(409, "治理文件路径或项目归属不安全，无法完整导出")
    return candidate, root


def _export(project_id: int, db: Session, archive, *, max_bytes: int = MAX_BYTES,
            deadline: float | None = None):
    files = []
    remaining = max_bytes
    index = {kind: set() for kind in ("import", "document", "analysis", "analysis_report", "harness_task", "draft",
                                      "case", "taxonomy", "suggestion_job", "suggestion", "governance_change",
                                      "duplicate_group", "duplicate_member", "governance_validation",
                                      "governance_metric", "governance_directory", "governance_export_job",
                                      "conversion",
                                      "conversion_input", "smoke_suite", "smoke_item", "execution_run",
                                      "run_case", "execution_report", "report_case", "defect", "knowledge")}
    references = []

    def link(source_kind, source_id, target_kind, target_id, relation):
        check_time()
        if len(references) >= MAX_LINKS:
            raise HTTPException(413, "项目来源关系达到导出上限，请联系管理员分批处理")
        references.append((source_kind, str(source_id), target_kind, str(target_id), relation))

    def check_time():
        if deadline is not None and time.monotonic() > deadline:
            raise HTTPException(503, "项目导出超时，请稍后重试")

    def check(size, *, manifest=False):
        nonlocal remaining
        check_time()
        if size > MAX_FILE_BYTES or size > remaining or len(files) + (1 if manifest else 2) > MAX_ENTRIES:
            raise HTTPException(413, "项目导出达到文件或容量上限，请联系管理员分批处理")
        remaining -= size

    def record(name, digest, size):
        files.append({"path": name, "size": size, "sha256": digest})

    def add_json(kind, identifier, payload, *, logical_id=None):
        name = _file_name(kind, identifier)
        data = canonical_json(payload)
        check(len(data))
        archive.writestr(name, data)
        record(name, hashlib.sha256(data).hexdigest(), len(data))
        if kind in index:
            index[kind].add(str(identifier if logical_id is None else logical_id))

    def add_analysis_report(task_id, report):
        if not SAFE_ID.fullmatch(task_id) or not isinstance(report, str) or not report.lstrip().startswith("# "):
            raise HTTPException(409, "Harness 原始分析报告损坏，无法完整导出")
        data = report.encode("utf-8")
        if len(data) > 1024 * 1024:
            raise HTTPException(409, "Harness 原始分析报告超出交付上限")
        name = f"analysis_report/{task_id}.md"
        check(len(data))
        archive.writestr(name, data)
        record(name, hashlib.sha256(data).hexdigest(), len(data))
        index["analysis_report"].add(str(task_id))

    def add_binary(name, path, root, *, expected_size=None, expected_sha256=None):
        try:
            stream, size = open_private_regular_file(path, root=root)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "项目文件已缺失或不安全，无法提供完整导出") from exc
        with stream:
            before = os.fstat(stream.fileno())
            if expected_size is not None and expected_size != size:
                raise HTTPException(409, "项目文件大小与记录不符，请先核对数据")
            check(size)
            digest = hashlib.sha256()
            copied = 0
            with archive.open(name, "w", force_zip64=True) as target:
                while chunk := stream.read(1024 * 1024):
                    check_time()
                    copied += len(chunk)
                    if copied > size:
                        raise HTTPException(409, "项目文件在导出期间发生变化，请重试")
                    digest.update(chunk)
                    target.write(chunk)
            if copied != size:
                raise HTTPException(409, "项目文件在导出期间发生变化，请重试")
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise HTTPException(409, "项目文件在导出期间发生变化，请重试")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise HTTPException(409, "项目来源文件与保存的摘要不一致")
            record(name, digest.hexdigest(), size)
            return size, digest.hexdigest()

    def rows(model):
        return db.scalars(select(model).where(model.project_id == project_id).order_by(model.id)).yield_per(100)

    for row in rows(ImportBatch):
        add_json("import", row.id, {"id": row.id, "project_id": project_id, "filename": row.filename,
                 "headers": row.headers, "rows": row.rows, "warnings": row.warnings,
                 "status": row.status, "kind": row.kind, "sha256": row.sha256,
                 "source_sheet": row.source_sheet, "row_count": row.row_count,
                 "created_at": row.created_at, "source_saved": bool(row.snapshot_path) if row.kind == "governance" else False})
        if row.kind == "governance" and row.snapshot_path:
            if row.sha256 is None or not re.fullmatch(r"[a-f0-9]{64}", row.sha256):
                raise HTTPException(409, "治理导入来源摘要损坏，无法完整导出")
            source_path, source_root = _governance_file(row.snapshot_path, project_id, export=False)
            if source_path.stem != row.sha256:
                raise HTTPException(409, "治理导入来源文件与摘要不匹配")
            add_binary(_binary_name("governance-import-files", str(row.id)), source_path, source_root,
                       expected_sha256=row.sha256)
    for row in rows(SourceDocument):
        verified = row.source_sha256 is not None and row.source_size is not None
        if ((row.source_sha256 is None) != (row.source_size is None)
                or verified and (not re.fullmatch(r"[a-f0-9]{64}", row.source_sha256) or row.source_size < 0)):
            raise HTTPException(409, "需求材料上传身份记录损坏，无法完整导出")
        add_json("document", row.id, {"id": row.id, "project_id": project_id, "filename": row.filename,
                 "extracted_text": row.extracted_text, "warnings": row.warnings, "created_at": row.created_at,
                 "source_sha256": row.source_sha256, "source_size": row.source_size,
                 "creation_integrity": "verified" if verified else "unverified"})
        add_binary(_binary_name("document-files", row.id), ai_routes.DOCUMENT_DIR / str(project_id) / f"{row.id}.bin",
                   ai_routes.DOCUMENT_DIR, expected_size=row.source_size,
                   expected_sha256=row.source_sha256)
    for row in rows(AnalysisRun):
        add_json("analysis", row.id, {"id": row.id, "project_id": project_id, "source_ids": row.source_ids,
                 "status": row.status, "provider": row.provider, "result": row.result,
                 "version": row.version, "created_at": row.created_at, "updated_at": row.updated_at})
        for value in row.source_ids or []:
            link("document", value, "analysis", row.id, "analyzed_from")
    for row in rows(HarnessTask):
        add_json("harness_task", row.id, {"id": row.id, "project_id": project_id,
                 "task_type": row.task_type, "task_version": row.task_version,
                 "result_schema": row.result_schema, "skill_id": row.skill_id,
                 "source_document_ids": row.source_document_ids, "analysis_run_id": row.analysis_run_id,
                 "retry_of": row.retry_of, "status": row.status, "session_id": row.session_id,
                 "session_ids": row.session_ids, "result": row.result if row.status == "completed" else None,
                 "created_at": row.created_at, "updated_at": row.updated_at})
        for value in row.source_document_ids or []:
            link("document", value, "harness_task", row.id, "submitted_to")
        if row.analysis_run_id:
            link("analysis", row.analysis_run_id, "harness_task", row.id, "generated_by")
        if (row.task_type == "requirement-analysis" and row.status == "completed"
                and isinstance(row.result, dict) and row.result.get("report_markdown") is not None):
            run = db.get(AnalysisRun, row.analysis_run_id) if row.analysis_run_id else None
            original = (run.result or {}).get("harness_result") if run is not None and isinstance(run.result, dict) else None
            if (run is None or run.project_id != project_id or run.provider != "harness"
                    or run.status != "ready" or row.analysis_run_id not in index["analysis"]
                    or not isinstance(original, dict) or original.get("task_id") != row.id
                    or original.get("report_markdown") != row.result["report_markdown"]):
                raise HTTPException(409, "Harness 报告与项目分析快照不一致，无法完整导出")
            add_analysis_report(row.id, row.result["report_markdown"])
            link("harness_task", row.id, "analysis_report", row.id, "contains_report")
            link("analysis_report", row.id, "analysis", row.analysis_run_id, "reviewed_as")
    for row in rows(CaseDraft):
        add_json("draft", row.id, {"id": row.id, "project_id": project_id, "analysis_id": row.analysis_id,
                 "payload": row.payload, "status": row.status, "version": row.version,
                 "case_id": row.case_id, "created_at": row.created_at})
        link("analysis", row.analysis_id, "draft", row.id, "drafted_from")
        if row.case_id is not None:
            link("draft", row.id, "case", row.case_id, "published_as")
    for batch in rows(Case).partitions(100):
        case_ids = [item.id for item in batch]
        reviews = defaultdict(list)
        comments = defaultdict(list)
        for item in db.scalars(select(ReviewEvent).where(ReviewEvent.case_id.in_(case_ids))
                               .order_by(ReviewEvent.id)):
            reviews[item.case_id].append(item)
        for item in db.scalars(select(CaseComment).where(CaseComment.case_id.in_(case_ids))
                               .order_by(CaseComment.id)):
            comments[item.case_id].append(item)
        for row in batch:
            add_json("case", row.id, {"id": row.id, "project_id": project_id, "import_id": row.import_id,
                 "original_id": row.original_id, "title": row.title, "original": row.original,
                 "directory": row.directory, "level": row.level, "smoke": row.smoke,
                 "review_status": row.review_status, "version": row.version,
                 "final_level": row.final_level, "final_smoke": row.final_smoke,
                 "final_categories": row.final_categories, "final_taxonomy_version_id": row.final_taxonomy_version_id,
                 "platform_id": row.platform_id, "name_key": row.name_key,
                 "governance_status": row.governance_status, "domain": row.domain,
                 "subdomain": row.subdomain, "endpoint": row.endpoint,
                 "tag_iteration": row.tag_iteration, "tag_requirement": row.tag_requirement,
                 "tag_change": row.tag_change, "fingerprint": row.fingerprint,
                 "content_hash": row.content_hash, "merged_into": row.merged_into,
                 "steps": row.steps, "precondition": row.precondition, "expected": row.expected,
                 "remark": row.remark, "created_at": row.created_at, "updated_at": row.updated_at,
                 "reviews": [{"id": item.id, "actor_id": item.user_id, "action": item.action,
                              "before": item.before, "after": item.after, "reason": item.reason,
                              "created_at": item.created_at} for item in reviews[row.id]],
                 "comments": [{"id": item.id, "actor_id": item.user_id, "text": item.text,
                               "created_at": item.created_at} for item in comments[row.id]]})
            link("import", row.import_id, "case", row.id, "imported_as")
    for row in rows(TaxonomyVersion):
        add_json("taxonomy", row.id, {"id": row.id, "project_id": project_id,
                 "number": row.number, "status": row.status, "revision": row.revision,
                 "definitions": row.definitions, "created_by": row.created_by,
                 "created_at": row.created_at, "published_at": row.published_at})
    for row in rows(SuggestionJob):
        if row.taxonomy_version_id is not None and str(row.taxonomy_version_id) not in index["taxonomy"]:
            raise HTTPException(409, "建议任务的分类版本不属于本项目，无法完整导出")
        add_json("suggestion_job", row.id, {"id": row.id, "project_id": project_id,
                 "created_by": row.created_by, "taxonomy_version_id": row.taxonomy_version_id,
                 "provider": row.provider, "status": row.status, "case_ids": row.case_ids,
                 "total": row.total, "completed": row.completed, "errors": row.errors,
                 "created_at": row.created_at})
        if row.taxonomy_version_id is not None:
            link("taxonomy", row.taxonomy_version_id, "suggestion_job", row.id, "used_for_suggestions")
    for row in rows(CaseSuggestion):
        if (str(row.case_id) not in index["case"]
                or row.taxonomy_version_id is not None and str(row.taxonomy_version_id) not in index["taxonomy"]
                or row.job_id is not None and str(row.job_id) not in index["suggestion_job"]):
            raise HTTPException(409, "用例建议来源不属于本项目，无法完整导出")
        add_json("suggestion", row.id, {"id": row.id, "project_id": project_id,
                 "case_id": row.case_id, "case_version": row.case_version,
                 "taxonomy_version_id": row.taxonomy_version_id, "job_id": row.job_id,
                 "source": row.source, "status": row.status, "categories": row.categories,
                 "level": row.level, "smoke": row.smoke, "rationale": row.rationale,
                 "questions": row.questions, "created_by": row.created_by,
                 "created_at": row.created_at, "decided_by": row.decided_by,
                 "decided_at": row.decided_at, "decision_reason": row.decision_reason})
        link("case", row.case_id, "suggestion", row.id, "suggested_for")
        if row.taxonomy_version_id is not None:
            link("taxonomy", row.taxonomy_version_id, "suggestion", row.id, "suggested_with")
        if row.job_id is not None:
            link("suggestion_job", row.job_id, "suggestion", row.id, "produced_suggestion")
    directory_changes = []
    for row in rows(GovernanceChangeLog):
        add_json("governance_change", row.id, {"id": row.id, "project_id": project_id,
                 "entity": row.entity, "entity_id": row.entity_id, "action": row.action,
                 "before": row.before_json, "after": row.after_json, "batch_key": row.batch_key,
                 "operator_id": row.operator_id, "ts": row.ts, "revert_of": row.revert_of})
        if row.entity in {"case", "duplicate-member"} and row.entity_id.isascii() and row.entity_id.isdecimal():
            case_id = int(row.entity_id)
            if str(case_id) not in index["case"]:
                case = db.get(Case, case_id)
                if case is not None and case.project_id != project_id:
                    raise HTTPException(409, "治理变更来源不属于本项目，无法完整导出")
            link("case", case_id, "governance_change", row.id, "governed_as")
        elif row.entity == "directory":
            directory_changes.append((row.entity_id, row.id))
    for batch in rows(GovernanceDuplicateGroup).partitions(100):
        for row in batch:
            if str(row.batch_id) not in index["import"]:
                raise HTTPException(409, "重复组来源批次不属于本项目，无法完整导出")
            add_json("duplicate_group", row.id, {"id": row.id, "project_id": project_id,
                     "batch_id": row.batch_id, "gid": row.gid, "name_key": row.name_key,
                     "name": row.name, "size": row.size, "cluster_count": row.cluster_count,
                     "category": row.category, "suggested_discard_count": row.suggested_discard_count,
                     "status": row.status, "revision": row.revision,
                     "decided_by": row.decided_by, "decided_at": row.decided_at})
            link("import", row.batch_id, "duplicate_group", row.id, "grouped_duplicates")
        for member in db.scalars(select(GovernanceDuplicateMember).where(
                GovernanceDuplicateMember.group_id.in_([row.id for row in batch]))
                .order_by(GovernanceDuplicateMember.id)):
            if str(member.case_id) not in index["case"]:
                raise HTTPException(409, "重复组成员用例不属于本项目，无法完整导出")
            add_json("duplicate_member", member.id, {"id": member.id, "project_id": project_id,
                     "group_id": member.group_id, "case_id": member.case_id,
                     "cluster_fp": member.cluster_fp, "score": member.score,
                     "decision": member.decision, "reason": member.reason})
            link("duplicate_group", member.group_id, "duplicate_member", member.id, "contains_duplicate")
            link("case", member.case_id, "duplicate_member", member.id, "reviewed_as_duplicate")
    for row in rows(GovernanceValidationRun):
        add_json("governance_validation", row.id, {"id": row.id, "project_id": project_id,
                 "filename": row.filename, "rows": row.rows, "result": row.result,
                 "status": row.status, "created_by": row.created_by, "created_at": row.created_at})
    for row in rows(GovernanceMetricSnapshot):
        if str(row.batch_id) not in index["import"]:
            raise HTTPException(409, "治理指标来源批次不属于本项目，无法完整导出")
        add_json("governance_metric", row.id, {"id": row.id, "project_id": project_id,
                 "batch_id": row.batch_id, "key": row.key, "value": row.value,
                 "created_at": row.created_at})
        link("import", row.batch_id, "governance_metric", row.id, "measured_as")
    directory_parents = []
    for row in db.scalars(select(GovernanceDirectory).where(GovernanceDirectory.project_id == project_id)
                          .order_by(GovernanceDirectory.path)).yield_per(100):
        identifier = _directory_id(row.path)
        add_json("governance_directory", identifier, {"id": identifier, "project_id": project_id,
                 "path": row.path, "parent": row.parent, "level": row.level,
                 "domain": row.domain, "subdomain": row.subdomain,
                 "whitelisted": row.whitelisted, "frozen": row.frozen, "status": row.status})
        if row.parent:
            directory_parents.append((row.parent, identifier))
    for parent, identifier in directory_parents:
        parent_id = _directory_id(parent)
        if parent_id in index["governance_directory"]:
            link("governance_directory", parent_id, "governance_directory", identifier, "contains_directory")
    for path, change_id in directory_changes:
        identifier = _directory_id(path)
        if identifier in index["governance_directory"]:
            link("governance_directory", identifier, "governance_change", change_id, "governed_as")
    for row in rows(GovernanceExportJob):
        if not row.path:
            raise HTTPException(409, "治理导出文件未保存，无法完整导出")
        export_path, export_root = _governance_file(row.path, project_id, export=True)
        selfcheck = row.selfcheck or {}
        created_sha256, created_size = selfcheck.get("created_sha256"), selfcheck.get("created_size")
        if "created_sha256" in selfcheck or "created_size" in selfcheck:
            if (not isinstance(created_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", created_sha256)
                    or type(created_size) is not int or created_size < 0):
                raise HTTPException(409, "治理导出创建时摘要记录损坏，无法完整导出")
        file_size, file_sha256 = add_binary(_binary_name("governance-export-files", str(row.id)),
                                           export_path, export_root,
                                           expected_size=created_size, expected_sha256=created_sha256)
        add_json("governance_export_job", row.id, {"id": row.id, "project_id": project_id,
                 "kind": row.kind, "format": row.format, "rows": row.rows,
                 "selfcheck": row.selfcheck, "created_by": row.created_by,
                 "created_at": row.created_at, "file_size": file_size, "file_sha256": file_sha256,
                 "creation_integrity": "verified" if created_sha256 is not None else "unverified"})
    for row in rows(ConversionRecord):
        source = conversion_routes.source_metadata(row)
        if (row.output_sha256 is not None
                and not conversion_routes.SHA256.fullmatch(row.output_sha256)):
            raise HTTPException(409, "转换结果创建时摘要损坏，无法完整导出")
        add_json("conversion", row.id, {"id": row.id, "project_id": project_id,
                 "source_filename": row.source_filename, "output_filename": row.output_filename,
                 "conversion_mode": row.conversion_mode, "output_mode": row.output_mode,
                 "app_version": row.app_version, "row_count": row.row_count,
                 "file_size": row.file_size, "preview": row.preview, "created_at": row.created_at,
                 "output_sha256": row.output_sha256,
                 "creation_integrity": "verified" if row.output_sha256 else "unverified"})
        add_binary(_binary_name("conversion-files", row.id), conversion_routes.DATA_DIR / str(project_id) / f"{row.id}.bin",
                   conversion_routes.DATA_DIR, expected_size=row.file_size,
                   expected_sha256=row.output_sha256)
        if source is not None:
            add_json("conversion_input", row.id, {"id": row.id, "project_id": project_id,
                     "conversion_id": row.id, "filename": row.source_filename,
                     "size": source["size"], "sha256": source["sha256"], "created_at": row.created_at})
            add_binary(_binary_name("conversion-input-files", row.id), conversion_routes.source_path(row),
                       conversion_routes.DATA_DIR, expected_size=source["size"],
                       expected_sha256=source["sha256"])
            link("conversion_input", row.id, "conversion", row.id, "converted_into")
    for row in rows(SmokeSuite):
        add_json("smoke_suite", row.id, {"id": row.id, "project_id": project_id,
                 "title": row.title, "target_version": row.target_version, "environment": row.environment,
                 "required_chains": row.required_chains, "revision": row.revision,
                 "created_by": row.created_by, "created_at": row.created_at, "updated_at": row.updated_at})
    for row in db.scalars(select(SmokeItem).join(SmokeSuite, SmokeItem.suite_id == SmokeSuite.id)
                          .where(SmokeSuite.project_id == project_id).order_by(SmokeItem.id)).yield_per(100):
        add_json("smoke_item", row.id, {"id": row.id, "project_id": project_id,
                 "suite_id": row.suite_id, "case_id": row.case_id, "case_version": row.case_version,
                 "chain_name": row.chain_name, "estimated_seconds": row.estimated_seconds,
                 "confirmed_by": row.confirmed_by, "confirmed_at": row.confirmed_at})
        link("smoke_suite", row.suite_id, "smoke_item", row.id, "contains_case")
        link("case", row.case_id, "smoke_item", row.id, "selected_for_smoke")
    for row in rows(ExecutionRun):
        add_json("execution_run", row.id, {"id": row.id, "project_id": project_id,
                 "suite_id": row.suite_id, "suite_revision": row.suite_revision,
                 "title": row.title, "target_version": row.target_version, "environment": row.environment,
                 "required_chains": row.required_chains, "frozen_case_count": row.frozen_case_count,
                 "status": row.status, "started_by": row.started_by, "started_at": row.started_at,
                 "completed_by": row.completed_by, "completed_at": row.completed_at})
        link("smoke_suite", row.suite_id, "execution_run", row.id, "executed_as")
    run_cases = db.scalars(select(RunCase).join(ExecutionRun, RunCase.run_id == ExecutionRun.id)
                           .where(ExecutionRun.project_id == project_id).order_by(RunCase.id)).yield_per(100)
    for batch in run_cases.partitions(100):
        events = defaultdict(list)
        for item in db.scalars(select(ExecutionEvent).where(
                ExecutionEvent.run_case_id.in_([row.id for row in batch])).order_by(ExecutionEvent.id)):
            events[item.run_case_id].append(item)
        for row in batch:
            add_json("run_case", row.id, {"id": row.id, "project_id": project_id,
                 "run_id": row.run_id, "case_id": row.case_id, "case_version": row.case_version,
                 "original_id": row.original_id, "title": row.title, "level": row.level,
                 "smoke": row.smoke, "chain_name": row.chain_name, "estimated_seconds": row.estimated_seconds,
                 "result": row.result, "actual_seconds": row.actual_seconds, "note": row.note,
                 "version": row.version, "executed_by": row.executed_by, "executed_at": row.executed_at,
                 "events": [{"id": item.id, "actor_id": item.user_id, "before": item.before,
                             "after": item.after, "created_at": item.created_at} for item in events[row.id]]})
            link("execution_run", row.run_id, "run_case", row.id, "contains_case")
            link("case", row.case_id, "run_case", row.id, "executed_as")
    for row in db.scalars(select(ExecutionReport).where(ExecutionReport.project_id == project_id)
                          .order_by(ExecutionReport.run_id)).yield_per(100):
        # Never export a corrupt report as independently verifiable evidence.
        _snapshot(project_id, row.run_id, db)
        add_json("execution_report", row.run_id, row.payload)
        link("execution_run", row.run_id, "execution_report", row.run_id, "completed_as")
        frozen_cases = row.payload.get("cases")
        if not isinstance(frozen_cases, list):
            raise HTTPException(409, "执行报告冻结用例结构损坏，无法导出")
        seen_cases = set()
        for item in frozen_cases:
            case_id = item.get("run_case_id") if isinstance(item, dict) else None
            if (type(case_id) is not int or case_id <= 0 or case_id in seen_cases
                    or type(item.get("case_id")) is not int or item["case_id"] <= 0):
                raise HTTPException(409, "执行报告冻结用例标识损坏，无法导出")
            seen_cases.add(case_id)
            logical_id = f"{row.run_id}:{case_id}"
            add_json("report_case", f"{row.run_id}-{case_id}",
                     {"id": logical_id, "project_id": project_id, "report_run_id": row.run_id,
                      "report_sha256": row.payload_sha256, "frozen": item}, logical_id=logical_id)
            link("execution_report", row.run_id, "report_case", logical_id, "contains_case")
            link("run_case", case_id, "report_case", logical_id, "frozen_as")
            link("case", item["case_id"], "report_case", logical_id, "snapshot_of")
    for batch in rows(ExecutionDefect).partitions(100):
        events = defaultdict(list)
        for item in db.scalars(select(ExecutionDefectEvent).where(
                ExecutionDefectEvent.defect_id.in_([row.id for row in batch])).order_by(ExecutionDefectEvent.id)):
            events[item.defect_id].append(item)
        for row in batch:
            if f"{row.run_id}:{row.run_case_id}" not in index["report_case"]:
                raise HTTPException(409, "缺陷关联的冻结用例不在项目报告中，无法完整导出")
            add_json("defect", row.id, {"id": row.id, "project_id": project_id, "run_id": row.run_id,
                 "run_case_id": row.run_case_id, "title": row.title, "description": row.description,
                 "severity": row.severity, "status": row.status, "resolution": row.resolution,
                 "version": row.version, "created_at": row.created_at, "updated_at": row.updated_at,
                 "events": [{"id": item.id, "actor_id": item.actor_id, "before": item.before,
                             "after": item.after, "created_at": item.created_at} for item in events[row.id]]})
            link("execution_report", row.run_id, "defect", row.id, "records_defect")
            link("report_case", f"{row.run_id}:{row.run_case_id}", "defect", row.id, "found_defect")
    for batch in rows(KnowledgeEntry).partitions(100):
        revisions = defaultdict(list)
        for item in db.scalars(select(KnowledgeRevision).where(
                KnowledgeRevision.entry_id.in_([row.id for row in batch])).order_by(KnowledgeRevision.version)):
            revisions[item.entry_id].append(item)
        for row in batch:
            add_json("knowledge", row.id, {"id": row.id, "project_id": project_id,
                 "source_type": row.source_type, "source_id": row.source_id,
                 "source_snapshot": row.source_snapshot, "title": row.title, "body": row.body,
                 "status": row.status, "version": row.version,
                 "created_at": row.created_at, "updated_at": row.updated_at,
                 "revisions": [{"version": item.version, "actor_id": item.actor_id,
                                "title": item.title, "body": item.body, "status": item.status,
                                "created_at": item.created_at} for item in revisions[row.id]]})
            link(row.source_type, row.source_id, "knowledge", row.id, "curated_as")

    # A missing source remains a historical reference, not a new live artifact.
    links = [{"from": {"type": a, "id": b, "included": b in index.get(a, set())},
              "to": {"type": c, "id": d, "included": d in index.get(c, set())}, "relation": e}
             for a, b, c, d, e in references]
    links.sort(key=lambda link: (link["from"]["type"], link["from"]["id"],
                                 link["to"]["type"], link["to"]["id"], link["relation"]))
    add_json("indexes", "references", {"schema_version": "project-references.v2", "links": links})
    counts = {kind: len(values) for kind, values in index.items()}
    project = db.get(Project, project_id)
    if project is None or project.deletion_pending_at:
        raise HTTPException(409, "项目状态已变化，请重试导出")
    applied = db.get(ProjectTemplateApplication, project_id)
    manifest = {"schema_version": "project-evidence.v2", "project_id": project_id,
                "project": {"name": project.name, "created_at": project.created_at,
                            "template_origin": ({"source_project_id": applied.source_project_id,
                                                 "template_id": applied.template_id,
                                                 "snapshot_sha256": applied.snapshot_sha256} if applied else None)},
                "generated_at": utcnow(), "counts": counts, "files": files,
                "scope": "Business evidence only; not a database or Harness Session backup. "
                         "Database rows share one read transaction; file bytes are verified as read. "
                         "No atomic database-and-filesystem snapshot is promised while writes continue."}
    manifest_bytes = canonical_json(manifest)
    check(len(manifest_bytes), manifest=True)
    archive.writestr("manifest.json", manifest_bytes)


def build_router(get_db, current_user, project_access) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["project-export"])

    @router.get("/evidence/export")
    def export_evidence(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        limit = env_int("TEST_ATELIER_EXPORT_MAX_BYTES", MAX_BYTES, 1)
        deadline = time.monotonic() + env_int("TEST_ATELIER_EXPORT_MAX_SECONDS", 300, 1)
        if not _active_exports.acquire(blocking=False):
            raise HTTPException(503, "项目导出服务忙，请稍后重试", headers={"Retry-After": "5"})
        output = None
        try:
            output = tempfile.TemporaryFile(mode="w+b")
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                 allowZip64=True) as archive:
                with consistent_read(db.get_bind()) as snapshot_db:
                    snapshot_user = snapshot_db.get(User, user.id)
                    if snapshot_user is None:
                        raise HTTPException(403, "无项目访问权限")
                    project_access(project_id, snapshot_user, snapshot_db)
                    _export(project_id, snapshot_db, archive, max_bytes=limit, deadline=deadline)
            output.seek(0)
        except Exception:
            if output is not None:
                output.close()
            _active_exports.release()
            raise

        def finish():
            try:
                output.close()
            finally:
                _active_exports.release()

        return StreamingResponse(iter(lambda: output.read(1024 * 1024), b""), media_type=MEDIA_TYPE,
                                 background=BackgroundTask(finish),
                                 headers={"Content-Disposition": f'attachment; filename="project-{project_id}-evidence.zip"',
                                          "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
