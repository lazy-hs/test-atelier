"""Read-only, project-scoped references between existing testing artifacts."""

import base64
import hashlib
import hmac
import json
import re
import threading
import time

from typing import Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import column, func, inspect, select, table
from sqlalchemy.orm import Session

from .db import (AnalysisRun, Case, CaseDraft, CaseSuggestion, ConversionRecord,
                 ExecutionDefect, ExecutionReport, ExecutionRun, GovernanceChangeLog, GovernanceDirectory,
                 GovernanceDuplicateGroup, GovernanceDuplicateMember, GovernanceExportJob,
                 GovernanceMetricSnapshot, GovernanceValidationRun, HarnessTask, ImportBatch,
                 KnowledgeEntry, RunCase, SmokeItem, SmokeSuite, SourceDocument,
                 SuggestionJob, TaxonomyVersion, User)
from .conversion_routes import source_metadata
from .execution_defect_routes import _snapshot
from .read_snapshot import consistent_read


ArtifactType = Literal["import", "document", "analysis", "harness_task", "draft", "case",
                       "conversion", "conversion_input", "smoke_suite", "smoke_item", "execution_run", "run_case",
                       "execution_report", "report_case", "defect", "knowledge", "taxonomy",
                       "suggestion_job", "suggestion", "governance_change", "duplicate_group",
                       "duplicate_member", "governance_validation", "governance_metric", "governance_export_job",
                       "governance_directory"]
CatalogType = Literal["import", "document", "analysis", "harness_task", "draft", "case",
                      "conversion", "smoke_suite", "smoke_item", "execution_run", "run_case",
                      "execution_report", "defect", "knowledge", "taxonomy", "suggestion_job",
                      "suggestion", "governance_change", "duplicate_group", "duplicate_member",
                      "governance_validation", "governance_metric", "governance_export_job",
                      "governance_directory"]
MODELS = {"import": ImportBatch, "document": SourceDocument, "analysis": AnalysisRun,
          "harness_task": HarnessTask, "draft": CaseDraft, "case": Case,
          "conversion": ConversionRecord, "smoke_suite": SmokeSuite, "smoke_item": SmokeItem,
          "execution_run": ExecutionRun, "run_case": RunCase,
          "defect": ExecutionDefect, "knowledge": KnowledgeEntry, "taxonomy": TaxonomyVersion,
          "suggestion_job": SuggestionJob, "suggestion": CaseSuggestion,
          "governance_change": GovernanceChangeLog, "duplicate_group": GovernanceDuplicateGroup,
          "duplicate_member": GovernanceDuplicateMember, "governance_validation": GovernanceValidationRun,
          "governance_metric": GovernanceMetricSnapshot, "governance_export_job": GovernanceExportJob,
          "governance_directory": GovernanceDirectory}
NUMERIC = {"import", "case", "smoke_suite", "smoke_item", "execution_run", "run_case",
           "execution_report", "defect", "knowledge", "taxonomy", "suggestion",
           "governance_change", "duplicate_group", "duplicate_member",
           "governance_validation", "governance_metric", "governance_export_job"}
MAX_EDGES = 100
MAX_VERIFIED_EDGES = 5000
MAX_VERIFIED_BYTES = 8 * 1024 * 1024
MAX_VERIFIED_CATALOG_ITEMS = 5000
MAX_VERIFIED_CATALOG_BYTES = 8 * 1024 * 1024
MAX_PROJECT_SNAPSHOT_NODES = 5000
MAX_PROJECT_SNAPSHOT_EDGES = 10000
MAX_PROJECT_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_PROJECT_SNAPSHOT_SECONDS = 60
_active_project_snapshots = threading.BoundedSemaphore(2)
PAGE_SIZE = 40
DOCUMENT_REFS = table("document_references", column("document_id"), column("source_kind"), column("source_id"))


def _directory_id(path: str) -> str:
    return "dir_" + base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _directory_path(value: str) -> str:
    if not value.startswith("dir_") or len(value) > 5500:
        raise HTTPException(422, "目录 ID 无效")
    encoded = value[4:]
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        path = raw.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "目录 ID 无效") from exc
    if not path or len(path) > 1024 or _directory_id(path) != value:
        raise HTTPException(422, "目录 ID 无效")
    return path


def _normalized(kind: ArtifactType, value: str) -> str:
    if kind == "governance_directory":
        _directory_path(value)
        return value
    if kind == "report_case":
        parts = value.split(":")
        if len(parts) == 2 and all(part.isascii() and part.isdecimal() and
                                   0 < len(part) <= 18 and int(part) > 0 for part in parts):
            return f"{int(parts[0])}:{int(parts[1])}"
        raise HTTPException(422, "冻结用例 ID 无效")
    if kind in NUMERIC:
        if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
            raise HTTPException(422, "产物 ID 必须为正整数")
        return str(int(value))
    if not value or len(value) > 128:
        raise HTTPException(422, "产物 ID 无效")
    return value


def _report(project_id: int, run_id: int, db: Session) -> ExecutionReport | None:
    row = db.get(ExecutionReport, run_id)
    if row is None or row.project_id != project_id:
        return None
    cache = db.info.setdefault("_validated_artifact_reports", {})
    if (project_id, run_id) not in cache:
        cache[project_id, run_id] = _snapshot(project_id, run_id, db)
    return cache[project_id, run_id]


def _case_catalog_pages(project_id: int, db: Session):
    # The case catalog is often thousands of rows. Read only visible node fields
    # once instead of re-querying every row across all verified pages.
    rows = db.execute(select(Case.id, func.substr(Case.title, 1, 160), Case.version)
                      .where(Case.project_id == project_id).order_by(Case.id.desc())
                      .limit(MAX_VERIFIED_CATALOG_ITEMS + 1)).all()
    if len(rows) > MAX_VERIFIED_CATALOG_ITEMS:
        raise HTTPException(413, "目录超过一致性游标上限，请使用实时目录或缩小项目范围")
    for start in range(0, len(rows), PAGE_SIZE):
        part = rows[start:start + PAGE_SIZE]
        cursor = str(rows[start - 1].id) if start else None
        yield cursor, {"items": [{"type": "case", "id": str(row.id), "title": str(row[1]),
                                   "version": row.version} for row in part],
                       "next_before_id": str(part[-1].id) if start + PAGE_SIZE < len(rows) else None}
    if not rows:
        yield None, {"items": [], "next_before_id": None}


def _node(project_id: int, kind: ArtifactType, value: str, db: Session) -> dict | None:
    if kind == "governance_directory":
        row = db.get(GovernanceDirectory, (project_id, _directory_path(value)))
        if row is None:
            return None
        return {"type": kind, "id": value, "title": row.path[:160], "version": 1}
    if kind == "conversion_input":
        conversion = _node(project_id, "conversion", value, db)
        if conversion is None:
            return None
        record = db.get(ConversionRecord, value)
        metadata = source_metadata(record)
        if metadata is None:
            return None
        return {"type": kind, "id": value, "title": record.source_filename[:160],
                "version": 1, "sha256": metadata["sha256"], "size": metadata["size"]}
    if kind == "report_case":
        run_id, run_case_id = map(int, value.split(":"))
        report = _report(project_id, run_id, db)
        if report is None:
            return None
        item = next((entry for entry in report.payload["cases"] if entry["run_case_id"] == run_case_id), None)
        if item is None:
            return None
        return {"type": kind, "id": value, "title": item["title"][:160],
                "version": item["case_version"], "original_id": item["original_id"],
                "case_id": item["case_id"], "result": item["result"], "frozen": True}
    if kind == "execution_report":
        row = _report(project_id, int(value), db)
    else:
        row = db.get(MODELS[kind], int(value) if kind in NUMERIC else value)
    if not row or (kind not in ("smoke_item", "run_case", "duplicate_member") and row.project_id != project_id):
        return None
    if kind == "execution_report":
        return {"type": kind, "id": value, "title": row.payload["source"]["title"],
                "version": row.payload["source"]["suite_revision"], "sha256": row.payload_sha256}
    if kind == "smoke_item" and _node(project_id, "smoke_suite", str(row.suite_id), db) is None:
        return None
    if kind == "run_case" and _node(project_id, "execution_run", str(row.run_id), db) is None:
        return None
    if kind == "duplicate_member" and (_node(project_id, "duplicate_group", str(row.group_id), db) is None
                                        or _node(project_id, "case", str(row.case_id), db) is None):
        return None
    if kind == "duplicate_group" and _node(project_id, "import", str(row.batch_id), db) is None:
        return None
    if kind == "governance_metric" and _node(project_id, "import", str(row.batch_id), db) is None:
        return None
    if kind == "suggestion" and (_node(project_id, "case", str(row.case_id), db) is None
                                 or row.taxonomy_version_id is not None and
                                 _node(project_id, "taxonomy", str(row.taxonomy_version_id), db) is None
                                 or row.job_id is not None and
                                 _node(project_id, "suggestion_job", row.job_id, db) is None):
        return None
    if kind == "suggestion_job" and row.taxonomy_version_id is not None and _node(
            project_id, "taxonomy", str(row.taxonomy_version_id), db) is None:
        return None
    if kind == "governance_change" and row.entity in {"case", "duplicate-member"} and row.entity_id.isascii() and row.entity_id.isdecimal():
        referenced = db.get(Case, int(row.entity_id))
        if referenced is not None and referenced.project_id != project_id:
            return None
    title = {"import": lambda: row.filename, "document": lambda: row.filename, "analysis": lambda: "需求分析",
             "harness_task": lambda: row.skill_id, "draft": lambda: row.payload.get("title", "用例草稿"),
             "case": lambda: row.title, "conversion": lambda: row.output_filename,
             "smoke_suite": lambda: row.title, "smoke_item": lambda: row.chain_name,
             "execution_run": lambda: row.title, "run_case": lambda: row.title,
             "defect": lambda: row.title, "knowledge": lambda: row.title,
             "taxonomy": lambda: f"分类版本 {row.number}", "suggestion_job": lambda: f"建议任务 {row.status}",
             "suggestion": lambda: f"用例建议 {row.status}", "governance_change": lambda: f"治理变更 {row.action}",
             "duplicate_group": lambda: row.name, "duplicate_member": lambda: f"重复成员 {row.decision}",
             "governance_validation": lambda: row.filename, "governance_metric": lambda: row.key,
             "governance_export_job": lambda: f"治理导出 {row.format}"}[kind]()
    return {"type": kind, "id": value, "title": str(title)[:160],
            "version": getattr(row, "version", getattr(row, "task_version", 1))}


def _verified_import_pages(project_id: int, value: str, db: Session) -> list[dict]:
    center = _node(project_id, "import", value, db)
    if center is None:
        raise HTTPException(404, "项目内产物不存在")
    related = []

    def append(kind: str, relation: str, query, versioned: bool):
        remaining = MAX_VERIFIED_EDGES - len(related)
        rows = db.execute(query.limit(remaining + 1)).all()
        if len(rows) > remaining:
            raise HTTPException(413, "关系超过一致性游标上限，请缩小范围后重试")
        for row in rows:
            related.append(({"type": kind, "id": str(row[0]), "title": str(row[1])[:160],
                             "version": row[2] if versioned else 1}, relation))

    append("knowledge", "curated_as", select(KnowledgeEntry.id, KnowledgeEntry.title, KnowledgeEntry.version)
           .where(KnowledgeEntry.project_id == project_id, KnowledgeEntry.source_type == "import",
                  KnowledgeEntry.source_id == value).order_by(KnowledgeEntry.id), True)
    append("duplicate_group", "grouped_duplicates", select(GovernanceDuplicateGroup.id,
           func.substr(GovernanceDuplicateGroup.name, 1, 160))
           .where(GovernanceDuplicateGroup.project_id == project_id,
                  GovernanceDuplicateGroup.batch_id == int(value)).order_by(GovernanceDuplicateGroup.id), False)
    append("governance_metric", "measured_as", select(GovernanceMetricSnapshot.id, GovernanceMetricSnapshot.key)
           .where(GovernanceMetricSnapshot.project_id == project_id,
                  GovernanceMetricSnapshot.batch_id == int(value)).order_by(GovernanceMetricSnapshot.id), False)
    append("case", "imported_as", select(Case.id, func.substr(Case.title, 1, 160), Case.version)
           .where(Case.project_id == project_id, Case.import_id == int(value)).order_by(Case.id), True)

    pages = []
    for start in range(0, max(1, len(related)), MAX_EDGES):
        part = related[start:start + MAX_EDGES]
        next_offset = start + MAX_EDGES if start + MAX_EDGES < len(related) else None
        page = {"center": center, "nodes": [center, *(node for node, _ in part)],
                "edges": [{"from": {"type": "import", "id": value},
                           "to": {"type": node["type"], "id": node["id"]}, "relation": relation}
                          for node, relation in part],
                "next_offset": next_offset, "truncated": next_offset is not None, "scope": "direct"}
        page["page_checkpoint"] = hashlib.sha256(json.dumps(page, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        pages.append(page)
    return pages


def build_router(get_db, current_user, project_access) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects/{project_id}/artifacts", tags=["artifacts"])

    @router.get("")
    def catalog(project_id: int, kind: CatalogType, before_id: str | None = Query(default=None, max_length=5500),
                consistency: Literal["live", "verified"] = "live",
                revision: str | None = Query(default=None, max_length=64),
                user: User = Depends(current_user), db: Session = Depends(get_db)):
        if consistency == "verified":
            if ((before_id is None and revision is not None) or
                    (before_id is not None and (revision is None or not re.fullmatch(r"[a-f0-9]{64}", revision)))):
                raise HTTPException(422, "目录一致性游标参数无效")
            requested = _normalized(kind, before_id) if before_id is not None else None
            with consistent_read(db.get_bind()) as snapshot_db:
                project_access(project_id, user, snapshot_db)
                pages = {}
                digest = hashlib.sha256()
                count = size = 0
                def catalog_pages():
                    if kind == "case":
                        yield from _case_catalog_pages(project_id, snapshot_db)
                        return
                    cursor = None
                    while True:
                        page = catalog(project_id, kind, cursor, "live", None, user, snapshot_db)
                        yield cursor, page
                        cursor = page["next_before_id"]
                        if cursor is None:
                            break

                for cursor, page in catalog_pages():
                    encoded = json.dumps(page, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")
                    count += len(page["items"])
                    size += len(encoded)
                    if count > MAX_VERIFIED_CATALOG_ITEMS or size > MAX_VERIFIED_CATALOG_BYTES:
                        raise HTTPException(413, "目录超过一致性游标上限，请使用实时目录或缩小项目范围")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
                    pages[cursor] = page
                current_revision = digest.hexdigest()
                if revision is not None and not hmac.compare_digest(revision, current_revision):
                    raise HTTPException(409, "目录在翻页期间发生变化，请从头刷新")
                if requested not in pages:
                    raise HTTPException(409, "目录页已不存在，请从头刷新")
                return {**pages[requested], "revision": current_revision}
        if revision is not None:
            raise HTTPException(422, "实时目录不接受一致性游标")
        project_access(project_id, user, db)
        model = ExecutionReport if kind == "execution_report" else MODELS[kind]
        column = model.run_id if kind == "execution_report" else model.path if kind == "governance_directory" else model.id
        if kind == "smoke_item":
            query = select(model).join(SmokeSuite, model.suite_id == SmokeSuite.id).where(SmokeSuite.project_id == project_id)
        elif kind == "run_case":
            query = select(model).join(ExecutionRun, model.run_id == ExecutionRun.id).where(ExecutionRun.project_id == project_id)
        elif kind == "duplicate_member":
            query = select(model).join(GovernanceDuplicateGroup, model.group_id == GovernanceDuplicateGroup.id).where(
                GovernanceDuplicateGroup.project_id == project_id)
        else:
            query = select(model).where(model.project_id == project_id)
        if before_id is not None:
            value = _normalized(kind, before_id)
            query = query.where(column < (_directory_path(value) if kind == "governance_directory"
                                          else int(value) if kind in NUMERIC else value))
        rows = db.scalars(query.order_by(column.desc()).limit(PAGE_SIZE + 1)).all()
        items = []
        for row in rows[:PAGE_SIZE]:
            identifier = (_directory_id(row.path) if kind == "governance_directory" else
                          str(row.run_id if kind == "execution_report" else row.id))
            node = _node(project_id, kind, identifier, db)
            if node is None:
                raise HTTPException(409, "产物来源与项目归属不一致，请核对数据")
            items.append(node)
        cursor = rows[PAGE_SIZE - 1] if len(rows) > PAGE_SIZE else None
        return {"items": items, "next_before_id": (
            _directory_id(cursor.path) if kind == "governance_directory" else
            str(cursor.run_id if kind == "execution_report" else cursor.id)) if cursor else None}

    @router.get("/snapshot")
    def project_snapshot(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
        project_access(project_id, user, db)
        if not _active_project_snapshots.acquire(blocking=False):
            raise HTTPException(503, "项目关系快照服务忙，请稍后重试", headers={"Retry-After": "5"})
        try:
            return _project_snapshot(project_id, user, db)
        finally:
            _active_project_snapshots.release()

    def _project_snapshot(project_id: int, user: User, db: Session):
        deadline = time.monotonic() + MAX_PROJECT_SNAPSHOT_SECONDS
        with consistent_read(db.get_bind()) as snapshot_db:
            project_access(project_id, user, snapshot_db)
            nodes = {}
            edges = {}

            def check_limits():
                if time.monotonic() > deadline:
                    raise HTTPException(503, "项目关系快照生成超时，请稍后重试")
                if len(nodes) > MAX_PROJECT_SNAPSHOT_NODES or len(edges) > MAX_PROJECT_SNAPSHOT_EDGES:
                    raise HTTPException(413, "项目关系超出单次快照上限，请使用目录和证据包")

            # Catalogs also include disconnected artifacts. Resolve special
            # report/input nodes via their owning report or conversion links.
            for kind in get_args(CatalogType):
                cursor = None
                while True:
                    check_limits()
                    page = catalog(project_id, kind, cursor, "live", None, user, snapshot_db)
                    for node in page["items"]:
                        nodes[(node["type"], node["id"])] = node
                    check_limits()
                    cursor = page["next_before_id"]
                    if cursor is None:
                        break

            queue = list(nodes)
            position = 0
            while position < len(queue):
                check_limits()
                kind, identifier = queue[position]
                position += 1
                if nodes[kind, identifier].get("missing"):
                    continue
                offset = 0
                while True:
                    check_limits()
                    page = relations(project_id, kind, identifier, offset, None, "live", None, user, snapshot_db)
                    for node in page["nodes"]:
                        key = (node["type"], node["id"])
                        if key not in nodes:
                            nodes[key] = node
                            queue.append(key)
                    for edge in page["edges"]:
                        key = (edge["from"]["type"], edge["from"]["id"],
                               edge["to"]["type"], edge["to"]["id"], edge["relation"])
                        edges[key] = edge
                    check_limits()
                    if page["next_offset"] is None:
                        break
                    offset = page["next_offset"]

            snapshot = {"scope": "single-read-transaction", "project_id": project_id,
                        "nodes": [nodes[key] for key in sorted(nodes)],
                        "edges": [edges[key] for key in sorted(edges)]}
            encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            result = {**snapshot, "revision": hashlib.sha256(encoded).hexdigest(),
                      "node_count": len(nodes), "edge_count": len(edges)}
            response_bytes = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            check_limits()
            if len(response_bytes) > MAX_PROJECT_SNAPSHOT_BYTES:
                raise HTTPException(413, "项目关系快照超出响应大小上限，请使用目录和证据包")
            return result

    @router.get("/{kind}/{artifact_id}/relations")
    def relations(project_id: int, kind: ArtifactType, artifact_id: str,
                  offset: int = Query(default=0, ge=0),
                  previous_page: str | None = Query(default=None, max_length=64),
                  consistency: Literal["live", "verified"] = "live",
                  revision: str | None = Query(default=None, max_length=64),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
        if consistency == "verified":
            if (offset % MAX_EDGES or (offset == 0 and revision is not None) or
                    (offset > 0 and (revision is None or not re.fullmatch(r"[a-f0-9]{64}", revision))) or
                    previous_page is not None):
                raise HTTPException(422, "一致性游标参数无效")
            # Evaluate every page against one database read transaction. A digest
            # of the complete ordered graph makes a changed *later* page fail too.
            with consistent_read(db.get_bind()) as snapshot_db:
                project_access(project_id, user, snapshot_db)
                pages = _verified_import_pages(project_id, _normalized(kind, artifact_id), snapshot_db) if kind == "import" else []
                digest = hashlib.sha256()
                total_bytes = 0
                position = 0
                while True:
                    if kind == "import":
                        if position // MAX_EDGES >= len(pages):
                            break
                        page = pages[position // MAX_EDGES]
                    else:
                        page = relations(project_id, kind, artifact_id, position, None,
                                         "live", None, user, snapshot_db)
                    serialized = json.dumps(page, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")
                    total_bytes += len(serialized)
                    if total_bytes > MAX_VERIFIED_BYTES or position + len(page["edges"]) > MAX_VERIFIED_EDGES:
                        raise HTTPException(413, "关系超过一致性游标上限，请缩小范围后重试")
                    digest.update(len(serialized).to_bytes(8, "big"))
                    digest.update(serialized)
                    if kind != "import":
                        pages.append(page)
                    if page["next_offset"] is None:
                        break
                    position = page["next_offset"]
                current_revision = digest.hexdigest()
                if revision is not None and not hmac.compare_digest(revision, current_revision):
                    raise HTTPException(409, "关联在翻页期间发生变化，请从头刷新")
                if offset // MAX_EDGES >= len(pages):
                    raise HTTPException(409, "关联页已不存在，请从头刷新")
                return {**pages[offset // MAX_EDGES], "revision": current_revision}
        project_access(project_id, user, db)
        if previous_page is not None:
            if offset < MAX_EDGES or offset % MAX_EDGES or not re.fullmatch(r"[a-f0-9]{64}", previous_page):
                raise HTTPException(422, "关联翻页校验参数无效")
            prior = relations(project_id, kind, artifact_id, offset - MAX_EDGES, None,
                              "live", None, user, db)
            if (prior["next_offset"] != offset or
                    not hmac.compare_digest(prior["page_checkpoint"], previous_page)):
                raise HTTPException(409, "关联在翻页期间发生变化，请从头刷新")
        value = _normalized(kind, artifact_id)
        center = _node(project_id, kind, value, db)
        if center is None:
            raise HTTPException(404, "项目内产物不存在")
        nodes = {(kind, value): center}
        edges = []
        seen = 0
        has_more = False

        def add(source_kind, source_id, target_kind, target_id, relation, *, frozen=None):
            nonlocal seen, has_more
            if has_more:
                return
            source_id, target_id = str(source_id), str(target_id)
            resolved = {}
            for node_kind, node_id in ((source_kind, source_id), (target_kind, target_id)):
                node = nodes.get((node_kind, node_id)) or _node(project_id, node_kind, node_id, db)
                if node is None and frozen is not None and (node_kind, node_id) == (source_kind, source_id):
                    node = {"type": node_kind, "id": node_id,
                            "title": str(frozen.get("title", node_id))[:160],
                            "version": frozen.get("version", 1), "missing": True}
                if node is None:
                    return
                resolved[node_kind, node_id] = node
            seen += 1
            if seen <= offset:
                return
            if len(edges) >= MAX_EDGES:
                has_more = True
                return
            nodes.update(resolved)
            edges.append({"from": {"type": source_kind, "id": source_id},
                          "to": {"type": target_kind, "id": target_id}, "relation": relation})

        def scan(query, *, known_valid=False):
            nonlocal seen
            # Keep each database read bounded; stop as soon as the next valid
            # relation is found, without dropping high-ID or sparse references.
            start = 0
            if known_valid and seen < offset:
                # Only use this for rows whose query already proves both endpoints
                # belong to this project. Skipping unverified edges would shift pages.
                total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
                start = min(total, offset - seen)
                seen += start
                if start == total:
                    return
            while not has_more:
                rows = db.scalars(query.offset(start).limit(128)).all()
                for row in rows:
                    if has_more:
                        return
                    yield row
                if len(rows) < 128:
                    return
                start += len(rows)

        if kind in ("governance_directory", "case", "governance_change"):
            if kind == "governance_directory":
                directory = db.get(GovernanceDirectory, (project_id, _directory_path(value)))
                if directory.parent:
                    add("governance_directory", _directory_id(directory.parent), kind, value, "contains_directory")
                for row in scan(select(GovernanceDirectory).where(GovernanceDirectory.project_id == project_id,
                                                                  GovernanceDirectory.parent == directory.path)
                                .order_by(GovernanceDirectory.path), known_valid=True):
                    add(kind, value, kind, _directory_id(row.path), "contains_directory")
                for row in scan(select(Case).where(Case.project_id == project_id,
                                                  func.substr(Case.directory, 1, 128) == directory.path[:128],
                                                  Case.directory == directory.path).order_by(Case.id), known_valid=True):
                    add(kind, value, "case", row.id, "filed_in_directory")
                for row in scan(select(GovernanceChangeLog).where(GovernanceChangeLog.project_id == project_id,
                                                                 GovernanceChangeLog.entity == "directory",
                                                                 GovernanceChangeLog.entity_id == directory.path)
                                .order_by(GovernanceChangeLog.id)):
                    add(kind, value, "governance_change", row.id, "governed_as")
            elif kind == "case":
                case = db.get(Case, int(value))
                if case.directory:
                    add("governance_directory", _directory_id(case.directory), "case", value, "filed_in_directory")
            else:
                change = db.get(GovernanceChangeLog, int(value))
                if change.entity == "directory":
                    add("governance_directory", _directory_id(change.entity_id), kind, value, "governed_as")

        if kind == "knowledge":
            source = db.get(KnowledgeEntry, int(value))
            source_id = _normalized(source.source_type, source.source_id)
            add(source.source_type, source_id, "knowledge", value, "curated_as",
                frozen=source.source_snapshot)
        else:
            # A knowledge entry retains its verified source even after deletion.
            for row in scan(select(KnowledgeEntry).where(KnowledgeEntry.project_id == project_id,
                                                        KnowledgeEntry.source_type == kind,
                                                        KnowledgeEntry.source_id == value)
                            .order_by(KnowledgeEntry.id)):
                add(kind, value, "knowledge", row.id, "curated_as")

        if kind in ("taxonomy", "suggestion_job", "suggestion", "case"):
            if kind == "taxonomy":
                for row in scan(select(SuggestionJob).where(SuggestionJob.project_id == project_id,
                                                            SuggestionJob.taxonomy_version_id == int(value))
                                .order_by(SuggestionJob.id)):
                    add("taxonomy", value, "suggestion_job", row.id, "used_for_suggestions")
                for row in scan(select(CaseSuggestion).where(CaseSuggestion.project_id == project_id,
                                                             CaseSuggestion.taxonomy_version_id == int(value))
                                .order_by(CaseSuggestion.id)):
                    add("taxonomy", value, "suggestion", row.id, "suggested_with")
                for row in scan(select(Case).where(Case.project_id == project_id,
                                                 Case.final_taxonomy_version_id == int(value))
                                .order_by(Case.id)):
                    add("taxonomy", value, "case", row.id, "classified_with")
            elif kind == "suggestion_job":
                job = db.get(SuggestionJob, value)
                if job.taxonomy_version_id is not None:
                    add("taxonomy", job.taxonomy_version_id, "suggestion_job", value, "used_for_suggestions")
                for row in scan(select(CaseSuggestion).where(CaseSuggestion.project_id == project_id,
                                                             CaseSuggestion.job_id == value)
                                .order_by(CaseSuggestion.id)):
                    add("suggestion_job", value, "suggestion", row.id, "produced_suggestion")
            elif kind == "suggestion":
                suggestion = db.get(CaseSuggestion, int(value))
                add("case", suggestion.case_id, "suggestion", value, "suggested_for")
                if suggestion.taxonomy_version_id is not None:
                    add("taxonomy", suggestion.taxonomy_version_id, "suggestion", value, "suggested_with")
                if suggestion.job_id is not None:
                    add("suggestion_job", suggestion.job_id, "suggestion", value, "produced_suggestion")
            else:
                case = db.get(Case, int(value))
                if case.final_taxonomy_version_id is not None:
                    add("taxonomy", case.final_taxonomy_version_id, "case", value, "classified_with")
                for row in scan(select(CaseSuggestion).where(CaseSuggestion.project_id == project_id,
                                                             CaseSuggestion.case_id == int(value))
                                .order_by(CaseSuggestion.id)):
                    add("case", value, "suggestion", row.id, "suggested_for")
        if kind in ("import", "duplicate_group", "duplicate_member", "case", "governance_metric"):
            if kind == "import":
                for row in scan(select(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.project_id == project_id,
                                                                       GovernanceDuplicateGroup.batch_id == int(value))
                                .order_by(GovernanceDuplicateGroup.id)):
                    add("import", value, "duplicate_group", row.id, "grouped_duplicates")
                for row in scan(select(GovernanceMetricSnapshot).where(GovernanceMetricSnapshot.project_id == project_id,
                                                                        GovernanceMetricSnapshot.batch_id == int(value))
                                .order_by(GovernanceMetricSnapshot.id)):
                    add("import", value, "governance_metric", row.id, "measured_as")
            elif kind == "duplicate_group":
                group = db.get(GovernanceDuplicateGroup, int(value))
                add("import", group.batch_id, "duplicate_group", value, "grouped_duplicates")
                for row in scan(select(GovernanceDuplicateMember).where(
                        GovernanceDuplicateMember.group_id == int(value)).order_by(GovernanceDuplicateMember.id)):
                    add("duplicate_group", value, "duplicate_member", row.id, "contains_duplicate")
            elif kind == "duplicate_member":
                member = db.get(GovernanceDuplicateMember, int(value))
                add("duplicate_group", member.group_id, "duplicate_member", value, "contains_duplicate")
                add("case", member.case_id, "duplicate_member", value, "reviewed_as_duplicate")
            elif kind == "governance_metric":
                metric = db.get(GovernanceMetricSnapshot, int(value))
                add("import", metric.batch_id, "governance_metric", value, "measured_as")
            else:
                for row in scan(select(GovernanceDuplicateMember).join(
                        GovernanceDuplicateGroup, GovernanceDuplicateMember.group_id == GovernanceDuplicateGroup.id)
                        .where(GovernanceDuplicateGroup.project_id == project_id,
                               GovernanceDuplicateMember.case_id == int(value))
                        .order_by(GovernanceDuplicateMember.id)):
                    add("case", value, "duplicate_member", row.id, "reviewed_as_duplicate")
        if kind in ("case", "governance_change"):
            if kind == "case":
                for row in scan(select(GovernanceChangeLog).where(GovernanceChangeLog.project_id == project_id,
                                                                  GovernanceChangeLog.entity.in_(("case", "duplicate-member")),
                                                                  GovernanceChangeLog.entity_id == value)
                                .order_by(GovernanceChangeLog.id)):
                    add("case", value, "governance_change", row.id, "governed_as")
            else:
                change = db.get(GovernanceChangeLog, int(value))
                if change.entity in ("case", "duplicate-member") and change.entity_id.isascii() and change.entity_id.isdecimal():
                    add("case", int(change.entity_id), "governance_change", value, "governed_as")
                if change.revert_of is not None:
                    add("governance_change", change.revert_of, "governance_change", value, "reverts")
                for row in scan(select(GovernanceChangeLog).where(GovernanceChangeLog.project_id == project_id,
                                                                  GovernanceChangeLog.revert_of == int(value))
                                .order_by(GovernanceChangeLog.id)):
                    add("governance_change", value, "governance_change", row.id, "reverts")

        if kind in ("document", "analysis", "harness_task"):
            if kind == "document":
                if inspect(db.connection()).has_table("document_references"):
                    for source_kind, relation in (("analysis", "analyzed_from"), ("harness_task", "submitted_to")):
                        query = select(DOCUMENT_REFS.c.source_id).where(
                            DOCUMENT_REFS.c.document_id == value,
                            DOCUMENT_REFS.c.source_kind == source_kind,
                        ).order_by(DOCUMENT_REFS.c.source_id)
                        for source_id in scan(query):
                            add("document", value, source_kind, source_id, relation)
                else:
                    # Unmigrated local databases have no index. Retain a correct
                    # (but slower) read path instead of dropping old references.
                    for row in scan(select(AnalysisRun).where(AnalysisRun.project_id == project_id)
                                    .order_by(AnalysisRun.id)):
                        if value in (row.source_ids or []):
                            add("document", value, "analysis", row.id, "analyzed_from")
                    for row in scan(select(HarnessTask).where(HarnessTask.project_id == project_id)
                                    .order_by(HarnessTask.id)):
                        if value in (row.source_document_ids or []):
                            add("document", value, "harness_task", row.id, "submitted_to")
            elif kind == "analysis":
                analysis = db.get(AnalysisRun, value)
                for source_id in analysis.source_ids or []:
                    add("document", source_id, "analysis", value, "analyzed_from")
                for row in scan(select(HarnessTask).where(HarnessTask.project_id == project_id,
                                                         HarnessTask.analysis_run_id == value)
                                .order_by(HarnessTask.id)):
                    add("analysis", value, "harness_task", row.id, "generated_by")
                for row in scan(select(CaseDraft).where(CaseDraft.project_id == project_id,
                                                       CaseDraft.analysis_id == value)
                                .order_by(CaseDraft.id)):
                    add("analysis", value, "draft", row.id, "drafted_from")
            else:
                task = db.get(HarnessTask, value)
                for source_id in task.source_document_ids or []:
                    add("document", source_id, "harness_task", value, "submitted_to")
                if task.analysis_run_id:
                    add("analysis", task.analysis_run_id, "harness_task", value, "generated_by")
        if kind in ("draft", "case"):
            drafts = ([db.get(CaseDraft, value)] if kind == "draft" else
                      scan(select(CaseDraft).where(CaseDraft.project_id == project_id,
                                                       CaseDraft.case_id == int(value))
                           .order_by(CaseDraft.id)))
            for draft in drafts:
                add("analysis", draft.analysis_id, "draft", draft.id, "drafted_from")
                if draft.case_id:
                    add("draft", draft.id, "case", draft.case_id, "published_as")
        if kind in ("conversion", "conversion_input"):
            record = db.get(ConversionRecord, value)
            if source_metadata(record) is not None:
                add("conversion_input", value, "conversion", value, "converted_into")
        if kind in ("import", "case"):
            if kind == "case":
                case = db.get(Case, int(value))
                add("import", case.import_id, "case", value, "imported_as")
            else:
                for row in scan(select(Case).where(Case.project_id == project_id,
                                                   Case.import_id == int(value))
                                .order_by(Case.id), known_valid=True):
                    add("import", value, "case", row.id, "imported_as")
        if kind in ("case", "smoke_suite", "smoke_item"):
            if kind == "smoke_item":
                item = db.get(SmokeItem, int(value))
                add("smoke_suite", item.suite_id, "smoke_item", value, "contains_case")
                add("case", item.case_id, "smoke_item", value, "selected_for_smoke")
            elif kind == "smoke_suite":
                for row in scan(select(SmokeItem).where(SmokeItem.suite_id == int(value)).order_by(SmokeItem.id)):
                    add("smoke_suite", value, "smoke_item", row.id, "contains_case")
                for row in scan(select(ExecutionRun).where(ExecutionRun.project_id == project_id,
                                                         ExecutionRun.suite_id == int(value))
                                .order_by(ExecutionRun.id)):
                    add("smoke_suite", value, "execution_run", row.id, "executed_as")
            else:
                for row in scan(select(SmokeItem).join(SmokeSuite, SmokeItem.suite_id == SmokeSuite.id)
                                .where(SmokeSuite.project_id == project_id, SmokeItem.case_id == int(value))
                                .order_by(SmokeItem.id)):
                    add("case", value, "smoke_item", row.id, "selected_for_smoke")
        if kind in ("case", "execution_run", "run_case"):
            if kind == "run_case":
                item = db.get(RunCase, int(value))
                add("execution_run", item.run_id, "run_case", value, "contains_case")
                add("case", item.case_id, "run_case", value, "executed_as")
                if _report(project_id, item.run_id, db):
                    add("run_case", value, "report_case", f"{item.run_id}:{item.id}", "frozen_as")
            elif kind == "execution_run":
                run = db.get(ExecutionRun, int(value))
                add("smoke_suite", run.suite_id, "execution_run", value, "executed_as")
                if _report(project_id, int(value), db):
                    add("execution_run", value, "execution_report", value, "completed_as")
                for row in scan(select(RunCase).where(RunCase.run_id == int(value)).order_by(RunCase.id)):
                    add("execution_run", value, "run_case", row.id, "contains_case")
            else:
                for row in scan(select(RunCase).join(ExecutionRun, RunCase.run_id == ExecutionRun.id)
                                .where(ExecutionRun.project_id == project_id, RunCase.case_id == int(value))
                                .order_by(RunCase.id)):
                    add("case", value, "run_case", row.id, "executed_as")
                    if _report(project_id, row.run_id, db):
                        add("run_case", row.id, "report_case", f"{row.run_id}:{row.id}", "frozen_as")
        if kind in ("execution_report", "defect"):
            if kind == "defect":
                defect = db.get(ExecutionDefect, int(value))
                add("execution_report", defect.run_id, "defect", value, "records_defect")
                add("report_case", f"{defect.run_id}:{defect.run_case_id}", "defect", value, "found_defect")
            else:
                for row in scan(select(ExecutionDefect).where(ExecutionDefect.project_id == project_id,
                                                             ExecutionDefect.run_id == int(value))
                                .order_by(ExecutionDefect.id)):
                    add("execution_report", value, "defect", row.id, "records_defect")
                add("execution_run", value, "execution_report", value, "completed_as")
                report = _report(project_id, int(value), db)
                for item in report.payload["cases"]:
                    add("execution_report", value, "report_case", f"{value}:{item['run_case_id']}", "contains_case")
        if kind == "report_case":
            run_id, case_id = map(int, value.split(":"))
            item = next(item for item in _report(project_id, run_id, db).payload["cases"]
                        if item["run_case_id"] == case_id)
            add("execution_report", run_id, "report_case", value, "contains_case")
            add("run_case", item["run_case_id"], "report_case", value, "frozen_as")
            add("case", item["case_id"], "report_case", value, "snapshot_of")
            for row in scan(select(ExecutionDefect).where(ExecutionDefect.project_id == project_id,
                                                         ExecutionDefect.run_id == run_id,
                                                         ExecutionDefect.run_case_id == case_id)
                            .order_by(ExecutionDefect.id)):
                add("report_case", value, "defect", row.id, "found_defect")
        result = {"center": center, "nodes": list(nodes.values()), "edges": edges,
                  "next_offset": offset + MAX_EDGES if has_more else None,
                  "truncated": has_more, "scope": "direct"}
        result["page_checkpoint"] = hashlib.sha256(json.dumps(result, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return result

    return router
