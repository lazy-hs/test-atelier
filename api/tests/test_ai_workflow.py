import io
import os
import re
import time
import zipfile

import openpyxl
import pytest
from docx import Document
from PIL import Image
from pypdf import PdfWriter
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api import ai_engine, ai_routes  # noqa: E402
from api.db import Base, engine  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        csrf = client.get("/api/v1/auth/csrf").json()["token"]
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-admin-password-123"}, headers={"x-csrf-token": csrf}).json()
        headers = {"x-csrf-token": login["csrf"]}
        project = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()
        yield client, headers, f"/api/v1/projects/{project['id']}"


def wait_job(client, root, job_id):
    for _ in range(80):
        result = client.get(f"{root}/ai/jobs/{job_id}").json()
        if result["status"] in {"done", "failed"}:
            return result
        time.sleep(.025)
    pytest.fail("AI job did not finish")


def fake_model(prompt, schema, _provider):
    if "requirements" in schema["properties"]:
        source = re.search(r"文档 ID: ([a-f0-9]+)", prompt)
        return {"requirements": [{"id": "REQ-1", "title": "登录", "summary": "账号登录", "source_ids": [source.group(1)],
                                   "acceptance": ["正确账号进入首页"], "methods": ["EP"]}],
                "ambiguities": [{"id": "AQ-1", "description": "失败次数限制", "critical": True, "decision": ""}], "risks": ["连续失败"]}
    return {"cases": [{"module": ["登录"], "title": "有效账号登录", "priority": "P1", "precondition": "有账号",
                       "steps": [{"action": "输入正确账号", "expected": "进入首页"}], "requirement_ids": ["REQ-1"],
                       "method": "EP", "label": "Web", "remark": "", "smoke": ""}]}


def test_document_analysis_authoring_and_exports(context, monkeypatch):
    client, headers, root = context
    monkeypatch.setattr(ai_engine, "request_structured", fake_model)
    assert client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nCorrect account opens home")}).status_code == 403
    upload = client.post(f"{root}/documents", files={"file": ("r.md", b"# Login\nCorrect account opens home")}, headers=headers)
    assert upload.status_code == 200, upload.text
    doc_id = upload.json()["id"]
    assert client.get(f"{root}/documents/{doc_id}").json()["text"].startswith("# Login")
    assert client.get(f"{root}/documents/{doc_id}/download").content.startswith(b"# Login")
    response = client.post(f"{root}/analyses", json={"source_ids": [doc_id]}, headers=headers)
    assert response.status_code == 200, response.text
    analysis_id = response.json()["analysis_id"]
    assert wait_job(client, root, response.json()["job_id"])["status"] == "done"
    report = client.get(f"{root}/analyses/{analysis_id}").json()
    assert report["result"]["requirements"][0]["source_ids"] == [doc_id]
    assert client.post(f"{root}/analyses/{analysis_id}/generate", headers=headers).status_code == 409
    edited = client.patch(f"{root}/analyses/{analysis_id}/report", json={"version": report["version"], "report_markdown": "# 人工修订\n登录规则"}, headers=headers)
    assert edited.status_code == 200
    report = edited.json()
    decision = client.patch(f"{root}/analyses/{analysis_id}/ambiguities/AQ-1", json={"version": report["version"], "decision": "5 次锁定"}, headers=headers)
    assert decision.status_code == 200, decision.text
    assert decision.json()["result"]["report_markdown"] == "# 人工修订\n登录规则"
    assert client.patch(f"{root}/analyses/{analysis_id}/ambiguities/AQ-1", json={"version": report["version"], "decision": "8 次"}, headers=headers).status_code == 409
    generated = client.post(f"{root}/analyses/{analysis_id}/generate", headers=headers)
    assert generated.status_code == 200, generated.text
    assert wait_job(client, root, generated.json()["job_id"])["status"] == "done"
    drafts = client.get(f"{root}/drafts?analysis_id={analysis_id}").json()
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["payload"]["smoke"] == ""
    update = client.patch(f"{root}/drafts/{draft['id']}", json={"version": draft["version"], "payload": {**draft["payload"], "title": "人工确认登录"}}, headers=headers)
    assert update.status_code == 200, update.text
    xmind = client.get(f"{root}/drafts/export?analysis_id={analysis_id}&format=xmind")
    assert xmind.status_code == 200, xmind.text
    with zipfile.ZipFile(io.BytesIO(xmind.content)) as archive:
        assert "content.xml" in archive.namelist()
    excel = client.get(f"{root}/drafts/export?analysis_id={analysis_id}&format=xlsx")
    assert excel.status_code == 200
    sheet = openpyxl.load_workbook(io.BytesIO(excel.content)).active
    assert sheet.cell(2, 3).value == "人工确认登录"
    published = client.post(f"{root}/drafts/{draft['id']}/publish", headers=headers)
    assert published.status_code == 200, published.text
    cases = client.get(f"{root}/cases").json()["items"]
    assert cases[0]["smoke"] == ""
    assert cases[0]["level"] == "2"
    assert client.post(f"{root}/drafts/{draft['id']}/publish", headers=headers).json()["case_id"] == published.json()["case_id"]
    overview = client.get(f"{root}/overview").json()
    assert overview["case_count"] == 1
    assert overview["analysis_count"] == 1
    assert overview["document_count"] == 1
    assert overview["draft_count"] == 0
    assert {item["kind"] for item in overview["recent_activity"]} >= {"analysis", "draft", "document"}


def test_word_extraction_and_offline_failure(context, monkeypatch):
    client, headers, root = context
    word = Document(); word.add_paragraph("需求：登录后展示首页")
    buffer = io.BytesIO(); word.save(buffer)
    upload = client.post(f"{root}/documents", files={"file": ("req.docx", buffer.getvalue())}, headers=headers)
    assert "登录后展示首页" in upload.json()["text"]
    def unavailable(*_args):
        raise RuntimeError("模型离线")
    monkeypatch.setattr(ai_engine, "request_structured", unavailable)
    job = client.post(f"{root}/analyses", json={"source_ids": [upload.json()["id"]]}, headers=headers).json()
    assert "模型离线" in wait_job(client, root, job["job_id"])["error"]
    assert client.get(f"{root}/cases").status_code == 200
    assert client.post(f"{root}/ai/jobs/{job['job_id']}/retry", headers=headers).status_code == 200
    assert wait_job(client, root, job["job_id"])["status"] == "failed"


def test_image_and_scanned_pdf_require_real_text(context, monkeypatch):
    from api import document_extract
    client, headers, root = context
    image = io.BytesIO(); Image.new("RGB", (200, 80), "white").save(image, format="PNG")
    monkeypatch.setattr(document_extract, "_ocr", lambda _data: ("", "OCR 不可用"))
    picture = client.post(f"{root}/documents", files={"file": ("screen.png", image.getvalue())}, headers=headers).json()
    assert picture["text"] == ""
    assert client.post(f"{root}/analyses", json={"source_ids": [picture["id"]]}, headers=headers).status_code == 422
    writer = PdfWriter(); writer.add_blank_page(width=200, height=100)
    pdf = io.BytesIO(); writer.write(pdf)
    blank = client.post(f"{root}/documents", files={"file": ("scan.pdf", pdf.getvalue())}, headers=headers).json()
    assert blank["text"] == ""
    monkeypatch.setattr(document_extract, "_ocr", lambda _data: ("登录按钮", None))
    scanned = client.post(f"{root}/documents", files={"file": ("scan.pdf", pdf.getvalue())}, headers=headers).json()
    assert "登录按钮" in scanned["text"]
    assert any("使用 OCR" in warning for warning in scanned["warnings"])


def test_document_project_isolation_and_reader_access(context):
    client, headers, root = context
    uploaded = client.post(f"{root}/documents", files={"file": ("r.md", b"# Private")}, headers=headers).json()
    other = client.post("/api/v1/projects", json={"name": "Other"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/documents/{uploaded['id']}").status_code == 404
    assert client.post(f"{root}/analyses", json={"source_ids": [uploaded["id"]]}, headers={}).status_code == 403
    user = client.post("/api/v1/users", json={"username": "reader", "password": "reader-password-123", "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": user["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    login = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password-123"}, headers={"x-csrf-token": token}).json()
    reader_headers = {"x-csrf-token": login["csrf"]}
    assert client.get(f"{root}/documents/{uploaded['id']}").status_code == 200
    assert client.get(f"/api/v1/projects/{other}/documents").status_code == 403
    assert client.post(f"{root}/documents", files={"file": ("r.md", b"# No")}, headers=reader_headers).status_code == 403
    assert client.post(f"{root}/analyses", json={"source_ids": [uploaded["id"]]}, headers=reader_headers).status_code == 403


def test_multidocument_dedup_and_quality_gate(monkeypatch):
    def model(prompt, schema, _provider):
        ids = re.findall(r'"source_ids": \["([a-f0-9]+)"\]', prompt)
        if "文档 ID:" in prompt:
            ids = re.findall(r"文档 ID: ([a-f0-9]+)", prompt)
        return {"requirements": [{"id": "REQ-1", "title": "登录", "summary": "登录成功", "source_ids": ids,
                                   "acceptance": ["展示首页"], "methods": ["EP"]}], "ambiguities": [], "risks": []}
    monkeypatch.setattr(ai_engine, "request_structured", model)
    documents = [{"id": "a" * 32, "filename": "a.md", "text": "登录成功展示首页"},
                 {"id": "b" * 32, "filename": "b.md", "text": "有效账号可进入首页"}]
    result = ai_engine.analyze(documents, "local")
    assert len(result["requirements"]) == 1
    assert set(result["requirements"][0]["source_ids"]) == {"a" * 32, "b" * 32}
    duplicate = [{"module": ["登录"], "title": "同名", "requirement_ids": ["REQ-1"], "method": "EP",
                  "steps": [{"action": "操作", "expected": "结果"}]}] * 2
    assert "存在重复的模块/用例标题" in ai_engine.assess_cases(duplicate, result)["errors"]
