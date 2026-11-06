"""Project knowledge must retain provenance without crossing project boundaries."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

from api.db import (AnalysisRun, ConversionRecord, HarnessTask, KnowledgeEntry, KnowledgeRevision,
                    SessionLocal, SourceDocument)
from api.tests.test_projects import client, login  # noqa: F401
from api.tests.test_smoke import context, create_suite, review_yes  # noqa: F401
from scripts.verify_project_export import verify


def test_knowledge_versions_access_and_source_snapshot_survive_source_change(client):
    headers = login(client)
    first = client.post("/api/v1/projects", json={"name": "知识一"}, headers=headers).json()["id"]
    second = client.post("/api/v1/projects", json={"name": "知识二"}, headers=headers).json()["id"]
    root = f"/api/v1/projects/{first}/knowledge"
    document = client.post(f"/api/v1/projects/{first}/documents", files={"file": ("rules.md", b"# API rule")},
                           headers=headers)
    assert document.status_code == 200, document.text
    source_id = document.json()["id"]
    other_doc = client.post(f"/api/v1/projects/{second}/documents", files={"file": ("private.md", b"# secret")},
                            headers=headers).json()["id"]
    draft = {"source_type": "document", "source_id": source_id, "title": "接口错误处理", "body": "人工确认 500 需要缺陷"}
    assert client.post(root, json={**draft, "source_id": other_doc}, headers=headers).status_code == 404
    assert client.post(root, json={**draft, "source_id": "unknown"}, headers=headers).status_code == 404
    assert client.post(root, json=draft).status_code == 403
    created = client.post(root, json=draft, headers=headers)
    assert created.status_code == 200, created.text
    entry = created.json()
    assert entry["source_snapshot"] == {"title": "rules.md", "version": 1,
                                        "source_integrity": "recorded", "source_size": len(b"# API rule"),
                                        "source_sha256": hashlib.sha256(b"# API rule").hexdigest()}
    assert entry["version"] == 1 and entry["source_id"] == source_id
    with SessionLocal() as db:
        source = db.get(SourceDocument, source_id)
        source.filename = "renamed.md"
        db.commit()
    assert client.get(f"{root}/{entry['id']}").json()["source_snapshot"] == entry["source_snapshot"]
    patch = {"version": 1, "title": "接口错误处理", "body": "加上对重试的人工核对", "status": "active"}
    assert client.patch(f"{root}/{entry['id']}", json=patch).status_code == 403
    updated = client.patch(f"{root}/{entry['id']}", json=patch, headers=headers)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert client.patch(f"{root}/{entry['id']}", json=patch, headers=headers).status_code == 409
    patch["version"] = 2
    patch["status"] = "archived"
    assert client.patch(f"{root}/{entry['id']}", json=patch, headers=headers).json()["version"] == 3
    assert client.get(root).json()["items"] == []
    assert client.get(f"{root}?status=archived").json()["items"][0]["id"] == entry["id"]
    history = client.get(f"{root}/{entry['id']}/revisions").json()["items"]
    assert [item["version"] for item in history] == [3, 2, 1]
    assert history[-1]["body"] == draft["body"]
    assert client.get(f"/api/v1/projects/{second}/knowledge/{entry['id']}").status_code == 404
    assert client.get(f"/api/v1/projects/{second}/knowledge/{entry['id']}/revisions").status_code == 404
    assert client.patch(f"/api/v1/projects/{second}/knowledge/{entry['id']}", json=patch,
                        headers=headers).status_code == 404

    reader = client.post("/api/v1/users", json={"username": "knowledge-reader", "password": "reader-password-123",
                                              "role": "reader"}, headers=headers).json()
    client.post(f"/api/v1/projects/{first}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    reader_headers = login(client, "knowledge-reader", "reader-password-123")
    assert client.get(f"{root}?status=archived").json()["items"][0]["body"] == patch["body"]
    assert len(client.get(f"{root}/{entry['id']}/revisions").json()["items"]) == 3
    assert client.get(f"/api/v1/projects/{second}/knowledge").status_code == 403
    assert client.post(root, json=draft, headers=reader_headers).status_code == 403
    assert client.patch(f"{root}/{entry['id']}", json={**patch, "version": 3},
                        headers=reader_headers).status_code == 403

    client.post("/api/v1/auth/logout", headers=reader_headers)
    headers = login(client)
    deleted = client.delete(f"/api/v1/projects/{first}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    with SessionLocal() as db:
        assert db.get(KnowledgeEntry, entry["id"]) is None
        assert db.query(KnowledgeRevision).count() == 0


def test_knowledge_document_snapshot_marks_legacy_identity_as_unrecorded(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "旧材料知识"}, headers=headers).json()["id"]
    root = f"/api/v1/projects/{project}/knowledge"
    doc_id = client.post(f"/api/v1/projects/{project}/documents", files={"file": ("old.md", b"# Old")},
                         headers=headers).json()["id"]
    with SessionLocal() as db:
        source = db.get(SourceDocument, doc_id)
        source.source_sha256 = None
        source.source_size = None
        db.commit()
    payload = {"source_type": "document", "source_id": doc_id, "title": "旧材料", "body": "人工复核"}
    response = client.post(root, json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["source_snapshot"] == {"title": "old.md", "version": 1,
                                                    "source_integrity": "unrecorded"}
    with SessionLocal() as db:
        source = db.get(SourceDocument, doc_id)
        source.source_sha256 = hashlib.sha256(b"# Old").hexdigest()
        db.commit()
    assert client.post(root, json=payload, headers=headers).status_code == 409


def test_knowledge_conversion_snapshot_keeps_recorded_output_identity(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "转换知识"}, headers=headers).json()["id"]
    other_project = client.post("/api/v1/projects", json={"name": "其他转换"}, headers=headers).json()["id"]
    raw = b"converted"
    digest = hashlib.sha256(raw).hexdigest()
    with SessionLocal() as db:
        for identifier, owner, recorded in (("new-conversion", project, digest),
                                            ("old-conversion", project, None),
                                            ("foreign-conversion", other_project, digest)):
            db.add(ConversionRecord(id=identifier, project_id=owner, user_id=1,
                                    source_filename="input.xlsx", output_filename="cases.xmind",
                                    conversion_mode="excel_to_xmind", output_mode="after",
                                    media_type="application/vnd.xmind.workbook", app_version="0.1",
                                    row_count=1, file_size=len(raw), output_sha256=recorded, preview={}))
        db.commit()
    root = f"/api/v1/projects/{project}/knowledge"
    payload = {"source_type": "conversion", "source_id": "new-conversion",
               "title": "导出规则", "body": "人工复核"}
    assert client.post(root, json={**payload, "source_id": "foreign-conversion"}, headers=headers).status_code == 404
    saved = client.post(root, json=payload, headers=headers)
    assert saved.status_code == 200
    snapshot = saved.json()["source_snapshot"]
    assert snapshot == {"title": "cases.xmind", "version": 1, "source_filename": "input.xlsx",
                        "output_integrity": "recorded", "output_sha256": digest, "output_size": len(raw)}
    with SessionLocal() as db:
        record = db.get(ConversionRecord, "new-conversion")
        record.output_filename = "changed.xmind"
        record.output_sha256 = "x" * 64
        db.commit()
    assert client.get(f"{root}/{saved.json()['id']}").json()["source_snapshot"] == snapshot
    assert client.post(root, json=payload, headers=headers).status_code == 409
    legacy = client.post(root, json={**payload, "source_id": "old-conversion"}, headers=headers)
    assert legacy.status_code == 200
    assert legacy.json()["source_snapshot"]["output_integrity"] == "unrecorded"
    assert "output_sha256" not in legacy.json()["source_snapshot"]


def test_completed_harness_and_analysis_are_allowed_but_running_sources_are_rejected(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "Harness 来源"}, headers=headers).json()["id"]
    other = client.post("/api/v1/projects", json={"name": "隔离来源"}, headers=headers).json()["id"]
    raw_result = {"schema": "skill-execution.v1", "result_id": "result-1",
                  "task_id": "task-completed", "session_id": "session-1",
                  "skill_id": "api-parsing", "source_document_ids": ["doc-2", "doc-1"],
                  "generated_at": "2026-10-27T00:00:00Z",
                  "data": {"output": "private skill output"}}
    with SessionLocal() as db:
        db.add(AnalysisRun(id="analysis-running", project_id=project, user_id=1, source_ids=[], status="running", result={}))
        db.add(AnalysisRun(id="analysis-ready", project_id=project, user_id=1, source_ids=[], status="ready", result={}, version=2))
        db.add(AnalysisRun(id="analysis-other", project_id=other, user_id=1, source_ids=[], status="ready", result={}))
        db.add(HarnessTask(id="task-completed", project_id=project, user_id=1, task_type="skill-execution",
                           skill_id="api-parsing", result_schema="skill-execution.v1", status="completed",
                           idempotency_key="unique", session_id="session-1", source_document_ids=["doc-1", "doc-2"],
                           result=raw_result))
        db.commit()
    root = f"/api/v1/projects/{project}/knowledge"
    payload = {"source_type": "analysis", "source_id": "analysis-running", "title": "确认", "body": "人工确认"}
    assert client.post(root, json=payload, headers=headers).status_code == 409
    assert client.post(root, json={**payload, "source_id": "analysis-other"}, headers=headers).status_code == 404
    assert client.post(root, json={**payload, "source_id": "analysis-ready"}, headers=headers).status_code == 409
    analysis_result = {"requirements": [{"id": "REQ-1"}], "report_markdown": "# Review"}
    with SessionLocal() as db:
        db.get(AnalysisRun, "analysis-ready").result = analysis_result
        db.commit()
    result = client.post(root, json={**payload, "source_id": "analysis-ready"}, headers=headers)
    assert result.status_code == 200 and result.json()["source_snapshot"]["version"] == 2
    analysis_snapshot = result.json()["source_snapshot"]
    assert analysis_snapshot["provider"] == "remote"
    assert analysis_snapshot["analysis_result_snapshot_sha256"] == hashlib.sha256(json.dumps(
        analysis_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    result = client.post(root, json={**payload, "source_type": "harness_task", "source_id": "task-completed"},
                         headers=headers)
    assert result.status_code == 200, result.text
    snapshot = result.json()["source_snapshot"]
    assert snapshot["session_id"] == "session-1" and snapshot["result_schema"] == "skill-execution.v1"
    assert snapshot["result_id"] == "result-1"
    assert snapshot["result_snapshot_sha256"] == hashlib.sha256(json.dumps(
        raw_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    for patch in ({"schema": "test-cases.v1"}, {"task_id": "another-task"},
                  {"session_id": "unbound-session"}, {"source_document_ids": ["doc-1"]},
                  {"skill_id": "figma-to-prd"}, {"data": None}, {"data": {}},
                  {"generated_at": None}):
        with SessionLocal() as db:
            db.get(HarnessTask, "task-completed").result = {**raw_result, **patch}
            db.commit()
        assert client.post(root, json={**payload, "source_type": "harness_task", "source_id": "task-completed"},
                           headers=headers).status_code == 409
        assert client.get(f"{root}/{result.json()['id']}").json()["source_snapshot"] == snapshot
    with SessionLocal() as db:
        db.get(HarnessTask, "task-completed").result = {}
        db.commit()
    assert client.post(root, json={**payload, "source_type": "harness_task", "source_id": "task-completed"},
                       headers=headers).status_code == 409


def test_harness_analysis_knowledge_binds_the_original_completed_task(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "原始分析"}, headers=headers).json()["id"]
    document_id = client.post(f"/api/v1/projects/{project}/documents",
                              files={"file": ("source.md", b"# Source")}, headers=headers).json()["id"]
    raw = {"schema": "requirement-analysis.v1", "task_id": "analysis-task", "result_id": "result-1",
           "session_id": "session-1", "source_document_ids": [document_id],
           "generated_at": "2026-10-27T00:00:00Z", "report_markdown": "# Original report",
           "data": {"requirements": [{"id": "REQ-1", "title": "登录", "description": "允许登录",
                                      "priority": "high", "source_refs": [
                                          {"document_id": document_id, "locator": "paragraph:1"}],
                                      "acceptance_criteria": []}]}}
    projected = {"requirements": [{"id": "REQ-1"}], "report_markdown": "# Original report",
                 "harness_result": raw}
    with SessionLocal() as db:
        db.add(AnalysisRun(id="analysis-harness", project_id=project, user_id=1,
                           source_ids=[document_id], status="ready", provider="harness",
                           result=projected, version=2))
        db.flush()
        db.add(HarnessTask(id="analysis-task", project_id=project, user_id=1,
                           task_type="requirement-analysis", skill_id="requirement-analysis",
                           result_schema="requirement-analysis.v1", status="completed",
                           idempotency_key="analysis-key", session_id="session-1",
                           analysis_run_id="analysis-harness", source_document_ids=[document_id], result=raw))
        db.commit()
    root = f"/api/v1/projects/{project}/knowledge"
    payload = {"source_type": "analysis", "source_id": "analysis-harness",
               "title": "分析结论", "body": "人工核对"}
    created = client.post(root, json=payload, headers=headers)
    assert created.status_code == 200, created.text
    snapshot = created.json()["source_snapshot"]
    assert snapshot["provider"] == "harness" and snapshot["harness_task_id"] == "analysis-task"
    assert snapshot["analysis_result_snapshot_sha256"] == hashlib.sha256(json.dumps(
        projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    task_reference = client.post(root, json={**payload, "source_type": "harness_task",
                                             "source_id": "analysis-task"}, headers=headers)
    assert task_reference.status_code == 200, task_reference.text
    assert task_reference.json()["source_snapshot"]["result_id"] == "result-1"
    for changed in ({**projected, "harness_result": {**raw, "task_id": "different-task"}},
                    {**projected, "harness_result": {**raw, "report_markdown": "# Altered"}},
                    {"requirements": projected["requirements"]},
                    {**projected, "harness_result": {key: value for key, value in raw.items()
                                                      if key != "report_markdown"}}):
        with SessionLocal() as db:
            db.get(AnalysisRun, "analysis-harness").result = changed
            db.commit()
        assert client.post(root, json=payload, headers=headers).status_code == 409
        assert client.post(root, json={**payload, "source_type": "harness_task",
                                       "source_id": "analysis-task"}, headers=headers).status_code == 409
        assert client.get(f"{root}/{created.json()['id']}").json()["source_snapshot"] == snapshot
    for field, value in (("result_schema", "test-cases.v1"), ("skill_id", "test-cases"),
                         ("task_version", "v2"),
                         ("session_id", "unbound-session"), ("session_ids", [False])):
        with SessionLocal() as db:
            db.get(AnalysisRun, "analysis-harness").result = projected
            setattr(db.get(HarnessTask, "analysis-task"), field, value)
            db.commit()
        assert client.post(root, json=payload, headers=headers).status_code == 409
        assert client.get(f"{root}/{created.json()['id']}").json()["source_snapshot"] == snapshot
        with SessionLocal() as db:
            setattr(db.get(HarnessTask, "analysis-task"), field,
                    {"result_schema": "requirement-analysis.v1", "skill_id": "requirement-analysis",
                     "task_version": "v1",
                     "session_id": "session-1", "session_ids": []}[field])
            db.commit()
    invalid_source = deepcopy(raw)
    invalid_source["data"]["requirements"][0]["source_refs"][0]["document_id"] = "foreign-document"
    for malformed in ({**raw, "result_id": ""}, {**raw, "session_id": "ta-analysis-task"},
                      invalid_source, {**raw, "generated_at": None}):
        with SessionLocal() as db:
            db.get(AnalysisRun, "analysis-harness").result = {**projected, "harness_result": malformed}
            db.get(HarnessTask, "analysis-task").result = malformed
            db.commit()
        assert client.post(root, json=payload, headers=headers).status_code == 409
        assert client.get(f"{root}/{created.json()['id']}").json()["source_snapshot"] == snapshot
    with SessionLocal() as db:
        db.get(AnalysisRun, "analysis-harness").result = projected
        db.get(HarnessTask, "analysis-task").result = raw
        db.get(HarnessTask, "analysis-task").status = "failed"
        db.commit()
    assert client.post(root, json=payload, headers=headers).status_code == 409


def test_completed_case_knowledge_rechecks_confirmed_analysis_traceability(client, tmp_path):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "用例溯源"}, headers=headers).json()["id"]
    foreign = client.post("/api/v1/projects", json={"name": "其他项目"}, headers=headers).json()["id"]
    document_id = client.post(f"/api/v1/projects/{project}/documents",
                              files={"file": ("source.md", b"# Source")}, headers=headers).json()["id"]
    original = {"schema": "requirement-analysis.v1", "result_id": "analysis-result",
                "task_id": "analysis-task", "session_id": "analysis-session",
                "source_document_ids": [document_id], "generated_at": "2026-10-27T00:00:00Z",
                "data": {"requirements": [{"id": "REQ-1", "title": "登录", "description": "允许登录",
                                          "priority": "high", "source_refs": [
                                              {"document_id": document_id, "locator": "paragraph:1"}],
                                          "acceptance_criteria": [{"id": "AC-1", "text": "正确账号可登录",
                                                                   "source_refs": [
                                                                       {"document_id": document_id,
                                                                        "locator": "paragraph:1"}]}]}]}}
    case_result = {"schema": "test-cases.v1", "result_id": "case-result", "task_id": "case-task",
                   "session_id": "case-session", "source_document_ids": [document_id],
                   "analysis_run_id": "analysis-for-cases", "generated_at": "2026-10-27T00:01:00Z",
                   "data": {"cases": [{"id": "TC-1", "title": "正确账号登录",
                                      "requirement_ids": ["REQ-1"], "preconditions": [],
                                      "steps": [{"order": 1, "action": "输入正确账号", "expected": "成功登录"}],
                                      "expected": "成功登录", "method": "EP", "priority": "high", "labels": [],
                                      "traceability": [{"requirement_id": "REQ-1",
                                                        "acceptance_criterion_id": "AC-1"}]}]}}
    with SessionLocal() as db:
        db.add(AnalysisRun(id="analysis-for-cases", project_id=project, user_id=1,
                           source_ids=[document_id], provider="harness", status="ready",
                           result={"harness_result": original}))
        db.flush()
        db.add(HarnessTask(id="case-task", project_id=project, user_id=1,
                           task_type="test-cases", skill_id="test-cases", result_schema="test-cases.v1",
                           analysis_run_id="analysis-for-cases", session_id="case-session",
                           source_document_ids=[document_id], status="completed",
                           idempotency_key="case-key", result=case_result))
        db.commit()
    root = f"/api/v1/projects/{project}/knowledge"
    payload = {"source_type": "harness_task", "source_id": "case-task",
               "title": "用例核对", "body": "人工检查通过"}
    created = client.post(root, json=payload, headers=headers)
    assert created.status_code == 200, created.text
    snapshot = created.json()["source_snapshot"]
    assert snapshot["analysis_run_id"] == "analysis-for-cases"
    assert snapshot["source_analysis_snapshot_sha256"] == hashlib.sha256(json.dumps(
        original, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    exported = client.get(f"/api/v1/projects/{project}/evidence/export")
    assert exported.status_code == 200, exported.text[:250]
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        packaged = json.loads(archive.read(f"knowledge/{created.json()['id']}.json"))
    assert packaged["source_snapshot"] == snapshot
    archive_path = tmp_path / "case-knowledge-evidence.zip"
    archive_path.write_bytes(exported.content)
    assert verify(archive_path)["project_id"] == project
    malformed = deepcopy(case_result)
    malformed["data"]["cases"][0]["traceability"][0]["acceptance_criterion_id"] = "AC-OTHER"
    with SessionLocal() as db:
        db.get(HarnessTask, "case-task").result = malformed
        db.commit()
    assert client.post(root, json=payload, headers=headers).status_code == 409
    assert client.get(f"{root}/{created.json()['id']}").json()["source_snapshot"] == snapshot
    with SessionLocal() as db:
        db.get(HarnessTask, "case-task").result = case_result
        db.get(AnalysisRun, "analysis-for-cases").project_id = foreign
        db.commit()
    assert client.post(root, json=payload, headers=headers).status_code == 409
    assert client.get(f"{root}/{created.json()['id']}").json()["source_snapshot"] == snapshot


def test_knowledge_list_and_history_have_bounded_cursor_pagination(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "分页项目"}, headers=headers).json()["id"]
    doc = client.post(f"/api/v1/projects/{project}/documents", files={"file": ("input.md", b"# source")},
                      headers=headers).json()["id"]
    path = f"/api/v1/projects/{project}/knowledge"
    entry = client.post(path, json={"source_type": "document", "source_id": doc,
                                    "title": "主条目", "body": "第一版"}, headers=headers).json()
    with SessionLocal() as db:
        current = db.get(KnowledgeEntry, entry["id"])
        for version in range(2, 56):
            db.add(KnowledgeRevision(entry_id=current.id, version=version, actor_id=1,
                                     title=f"v{version}", body="content", status="active"))
        current.version = 55
        for number in range(50):
            db.add(KnowledgeEntry(project_id=project, source_type="document", source_id=doc,
                                  source_snapshot={"title": "input.md", "version": 1},
                                  title=f"entry-{number}", body="content", status="active", version=1,
                                  created_by=1, updated_by=1))
        db.commit()
    first = client.get(path).json()
    assert len(first["items"]) == 50 and first["next_before_id"] is not None
    second = client.get(f"{path}?before_id={first['next_before_id']}").json()
    assert len(second["items"]) == 1 and second["items"][0]["id"] == entry["id"]
    assert second["next_before_id"] is None
    page = client.get(f"{path}/{entry['id']}/revisions").json()
    assert len(page["items"]) == 50 and page["items"][0]["version"] == 55
    older = client.get(f"{path}/{entry['id']}/revisions?before_version={page['next_before_version']}").json()
    assert [item["version"] for item in older["items"]] == [5, 4, 3, 2, 1]
    assert older["next_before_version"] is None


def test_execution_report_knowledge_is_frozen_and_project_scoped(context):
    client, headers, project_root, cases = context
    case = review_yes(client, headers, project_root, cases[0])
    suite = create_suite(client, headers, project_root, ["登录"])
    suite = client.post(f"{project_root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": case["id"], "chain_name": "登录", "estimated_seconds": 20},
        headers=headers).json()
    run = client.post(f"{project_root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    item = run["items"][0]
    client.put(f"{project_root}/execution-runs/{run['id']}/cases/{item['id']}", json={
        "version": 1, "result": "failed", "actual_seconds": 10, "note": "失败"}, headers=headers)
    client.post(f"{project_root}/execution-runs/{run['id']}/complete", headers=headers)
    report = client.get(f"{project_root}/execution-runs/{run['id']}/report").json()
    path = f"{project_root}/knowledge"
    payload = {"source_type": "execution_report", "source_id": str(run["id"]), "title": "执行结论", "body": "核对失败"}
    saved = client.post(path, json=payload, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["source_snapshot"]["sha256"] == report["integrity"]["payload_sha256"]
    assert client.post(path, json={**payload, "source_id": "001"}, headers=headers).status_code == 200
    assert client.post(path, json={**payload, "source_id": "not-int"}, headers=headers).status_code == 422
    assert client.delete(f"{project_root}/cases/{case['id']}", headers=headers).status_code == 200
    assert client.get(f"{path}/{saved.json()['id']}").json()["source_snapshot"]["sha256"] == report["integrity"]["payload_sha256"]
