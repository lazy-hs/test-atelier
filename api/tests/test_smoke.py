import io
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import openpyxl
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api import ai_routes, conversion_routes  # noqa: E402
from api.db import Base, ExecutionDefect, ExecutionDefectEvent, ExecutionReport, ExecutionRun, SessionLocal, engine  # noqa: E402
from api.main import app  # noqa: E402


@pytest.fixture
def context():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        token = client.get("/api/v1/auth/csrf").json()["token"]
        logged = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-admin-password-123"}, headers={"x-csrf-token": token})
        headers = {"x-csrf-token": logged.json()["csrf"]}
        project_id = client.post("/api/v1/projects", json={"name": "冒烟验收"}, headers=headers).json()["id"]
        root = f"/api/v1/projects/{project_id}"
        book = openpyxl.Workbook(); sheet = book.active
        sheet.append(["测试用例ID", "用例名称", "用例等级", "是否冒烟用例"])
        sheet.append(["001", "登录", "1", None])
        sheet.append(["002", "退出", "3", None])
        sheet.append(["003", "非冒烟", "4", "否"])
        output = io.BytesIO(); book.save(output)
        preview = client.post(f"{root}/imports/preview", files={"file": ("cases.xlsx", output.getvalue())}, headers=headers).json()
        assert client.post(f"{root}/imports/{preview['id']}/confirm", headers=headers).status_code == 200
        cases = client.get(f"{root}/cases").json()["items"]
        yield client, headers, root, cases


def review_yes(client, headers, root, case):
    response = client.put(f"{root}/cases/{case['id']}/review", json={
        "version": case["version"], "final_level": case["level"], "final_smoke": "是", "reason": "人工确认核心链路"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def create_suite(client, headers, root, chains=None):
    response = client.post(f"{root}/smoke-suites", json={"title": "冒烟集", "target_version": "v1.0",
        "environment": "预发布", "required_chains": chains if chains is not None else ["登录", "退出"]}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_human_confirmation_budget_coverage_and_snapshot(context):
    client, headers, root, cases = context
    suite = create_suite(client, headers, root)
    assert suite["budget_seconds"] == 1800 and suite["covered_count"] == 0
    assert client.get(f"{root}/smoke-suites/{suite['id']}/candidates").json() == []
    assert client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": 1,
        "case_id": cases[0]["id"], "chain_name": "登录", "estimated_seconds": 1200}, headers=headers).status_code == 422
    first, second = [review_yes(client, headers, root, case) for case in cases[:2]]
    candidates = client.get(f"{root}/smoke-suites/{suite['id']}/candidates").json()
    assert {item["id"] for item in candidates} == {first["id"], second["id"]}
    assert first["level"] == "1" and second["level"] == "3"
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": suite["revision"],
        "case_id": first["id"], "chain_name": "登录", "estimated_seconds": 1200}, headers=headers).json()
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": suite["revision"],
        "case_id": second["id"], "chain_name": "退出", "estimated_seconds": 900}, headers=headers).json()
    assert suite["estimated_seconds"] == 2100 and suite["over_budget"] is True
    assert suite["covered_count"] == 2 and suite["uncovered_chains"] == []
    assert len(suite["items"]) == 2
    assert client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": suite["revision"],
        "case_id": first["id"], "chain_name": "登录", "estimated_seconds": 1}, headers=headers).status_code == 409
    assert client.patch(f"{root}/smoke-suites/{suite['id']}", json={"revision": suite["revision"], "title": "新名称",
        "target_version": "v1.0", "environment": "预发布", "required_chains": ["登录"]}, headers=headers).status_code == 409
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={"suite_revision": suite["revision"]}, headers=headers)
    assert run.status_code == 200, run.text
    frozen = run.json()
    assert frozen["over_budget"] is True and frozen["estimated_seconds"] == 2100
    assert [item["level"] for item in frozen["items"]] == ["1", "3"]
    assert all(item["smoke"] == "是" and item["result"] == "not_run" for item in frozen["items"])
    assert client.post(f"{root}/execution-runs/{frozen['id']}/complete", headers=headers).status_code == 409
    edited = client.put(f"{root}/smoke-suites/{suite['id']}/items/{suite['items'][0]['id']}", json={
        "revision": suite["revision"], "chain_name": "登录", "estimated_seconds": 120}, headers=headers)
    assert edited.status_code == 200
    assert edited.json()["estimated_seconds"] == 1020
    assert client.get(f"{root}/execution-runs/{frozen['id']}").json()["estimated_seconds"] == 2100
    changed = client.put(f"{root}/cases/{first['id']}/review", json={"version": first["version"],
        "final_level": "1", "final_smoke": "否", "reason": "人工取消冒烟"}, headers=headers)
    assert changed.status_code == 200
    current = client.get(f"{root}/smoke-suites/{suite['id']}").json()
    assert current["items"][0]["stale"] is True
    assert client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={"suite_revision": current["revision"]}, headers=headers).status_code == 409
    assert client.get(f"{root}/execution-runs/{frozen['id']}").json()["items"][0]["smoke"] == "是"


def test_results_audit_conflicts_finish_and_permissions(context):
    client, headers, root, cases = context
    first, second = [review_yes(client, headers, root, case) for case in cases[:2]]
    suite = create_suite(client, headers, root)
    for case, chain in ((first, "登录"), (second, "退出")):
        suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": suite["revision"],
            "case_id": case["id"], "chain_name": chain, "estimated_seconds": 500}, headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={"suite_revision": suite["revision"]}, headers=headers).json()
    first_item, second_item = run["items"]
    assert client.get(f"{root}/execution-runs/{run['id']}/report").status_code == 409
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{first_item['id']}", json={
        "version": 1, "result": "failed", "actual_seconds": 300, "note": " "}, headers=headers).status_code == 422
    passed = client.put(f"{root}/execution-runs/{run['id']}/cases/{first_item['id']}", json={
        "version": 1, "result": "passed", "actual_seconds": 360, "note": ""}, headers=headers)
    assert passed.status_code == 200 and passed.json()["summary"]["passed"] == 1
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{first_item['id']}", json={
        "version": 1, "result": "failed", "actual_seconds": 400, "note": "失败"}, headers=headers).status_code == 409
    blocked = client.put(f"{root}/execution-runs/{run['id']}/cases/{second_item['id']}", json={
        "version": 1, "result": "blocked", "actual_seconds": 0, "note": "环境缺少账号"}, headers=headers)
    assert blocked.status_code == 200
    assert blocked.json()["summary"] == {"not_run": 0, "passed": 1, "failed": 0, "blocked": 1, "skipped": 0}
    assert len(blocked.json()["events"]) == 2
    completed = client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers)
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert completed.json()["completed_by"] == "admin"
    report_response = client.get(f"{root}/execution-runs/{run['id']}/report")
    assert report_response.status_code == 200
    assert report_response.headers["content-disposition"] == f'attachment; filename="execution-report-{run["id"]}.json"'
    assert report_response.headers["cache-control"] == "no-store"
    report = report_response.json()
    assert report["schema_version"] == "execution-report.v1"
    assert report["source"]["project_id"] == int(root.rsplit("/", 1)[-1])
    assert report["source"]["suite_revision"] == suite["revision"]
    assert report["summary"]["results"] == {"not_run": 0, "passed": 1, "failed": 0, "blocked": 1, "skipped": 0}
    assert report["summary"]["uncovered_chains"] == []
    assert report["chains"][0]["results"]["passed"] == 1
    assert report["cases"][1]["note"] == "环境缺少账号"
    assert "steps" not in report["cases"][0]
    payload = {key: value for key, value in report.items() if key != "integrity"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert report["integrity"]["payload_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_response.content
    revised = client.put(f"{root}/cases/{first['id']}/review", json={
        "version": first["version"], "final_level": "3", "final_smoke": "否", "reason": "执行后修改"}, headers=headers)
    assert revised.status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_response.content
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{first_item['id']}", json={
        "version": 2, "result": "failed", "actual_seconds": 400, "note": "不允许覆盖"}, headers=headers).status_code == 409
    assert client.delete(f"{root}/cases/{first['id']}", headers=headers).status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_response.content
    stats_after_delete = client.get(f"{root}/execution-statistics").json()
    assert stats_after_delete["case_executions"] == 2
    assert stats_after_delete["results"]["passed"] == 1
    assert stats_after_delete["unavailable_runs"] == 0
    with SessionLocal() as db:
        snapshot = db.get(ExecutionReport, run["id"])
        snapshot.payload_sha256 = "0" * 64
        db.commit()
    assert client.get(f"{root}/execution-runs/{run['id']}/report").status_code == 503
    assert client.get(f"{root}/execution-statistics").status_code == 503
    with SessionLocal() as db:
        snapshot = db.get(ExecutionReport, run["id"])
        snapshot.payload_sha256 = report["integrity"]["payload_sha256"]
        db.commit()
    with SessionLocal() as db:
        snapshot = db.get(ExecutionReport, run["id"])
        snapshot.passed_count += 1
        db.commit()
    assert client.get(f"{root}/execution-runs/{run['id']}/report").status_code == 503
    assert client.get(f"{root}/execution-statistics").status_code == 503
    with SessionLocal() as db:
        snapshot = db.get(ExecutionReport, run["id"])
        snapshot.passed_count -= 1
        db.commit()
    assert client.get(f"{root}/execution-statistics").json()["run_count"] == 1
    with SessionLocal() as db:
        db.get(ExecutionRun, run["id"]).target_version = "corrupt-target-version"
        db.commit()
    assert client.get(f"{root}/execution-statistics").status_code == 503
    with SessionLocal() as db:
        db.get(ExecutionRun, run["id"]).target_version = "v1.0"
        db.commit()
    assert client.get(f"{root}/execution-statistics").json()["run_count"] == 1
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 409
    other = client.post("/api/v1/projects", json={"name": "其他项目"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/smoke-suites/{suite['id']}").status_code == 404
    assert client.get(f"/api/v1/projects/{other}/execution-runs/{run['id']}").status_code == 404
    assert client.get(f"/api/v1/projects/{other}/execution-runs/{run['id']}/report").status_code == 404
    reader = client.post("/api/v1/users", json={"username": "reader", "password": "reader-password-123", "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    logged = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password-123"}, headers={"x-csrf-token": token}).json()
    readonly = {"x-csrf-token": logged["csrf"]}
    assert client.get(f"{root}/smoke-suites/{suite['id']}").status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}").status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_response.content
    assert client.get(f"/api/v1/projects/{other}/execution-runs/{run['id']}/report").status_code == 403
    assert client.post(f"{root}/smoke-suites", json={"title": "无权", "target_version": "v2", "environment": "test"}, headers=readonly).status_code == 403
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=readonly).status_code == 403


def test_old_active_run_can_resume_without_rewriting_its_frozen_source(context):
    client, headers, root, cases = context
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": reviewed["id"],
        "chain_name": "登录", "estimated_seconds": 120,
    }, headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    old_start = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with SessionLocal.begin() as db:
        db.get(ExecutionRun, run["id"]).started_at = old_start
    assert client.get(f"{root}/execution-runs/{run['id']}/report").status_code == 409
    resumed = client.get(f"{root}/execution-runs/{run['id']}").json()
    assert resumed["status"] == "active" and resumed["started_at"] == old_start
    assert resumed["items"][0]["case_version"] == reviewed["version"]
    skipped = client.put(f"{root}/execution-runs/{run['id']}/cases/{resumed['items'][0]['id']}", json={
        "version": resumed["items"][0]["version"], "result": "skipped",
        "actual_seconds": 0, "note": "等待窗口已结束，本次标记跳过",
    }, headers=headers)
    assert skipped.status_code == 200 and skipped.json()["summary"]["skipped"] == 1
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    report = client.get(f"{root}/execution-runs/{run['id']}/report").json()
    assert report["source"]["started_at"] == old_start
    assert report["cases"][0]["note"] == "等待窗口已结束，本次标记跳过"
    assert client.get(f"{root}/execution-statistics").json()["pass_rate"] is None


def test_uncovered_chain_is_reported_and_no_level_auto_smoke(context):
    client, headers, root, cases = context
    suite = create_suite(client, headers, root)
    assert cases[0]["level"] == "1"
    assert client.get(f"{root}/smoke-suites/{suite['id']}/candidates").json() == []
    first = review_yes(client, headers, root, cases[0])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={"revision": 1,
        "case_id": first["id"], "chain_name": "登录", "estimated_seconds": 300}, headers=headers).json()
    assert suite["covered_chains"] == ["登录"] and suite["uncovered_chains"] == ["退出"]
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={"suite_revision": suite["revision"]}, headers=headers)
    assert run.status_code == 200
    assert run.json()["uncovered_chains"] == ["退出"]


def test_execution_statistics_completed_only_filter_and_project_boundary(context):
    client, headers, root, cases = context
    reviewed = review_yes(client, headers, root, cases[0])
    first_suite = create_suite(client, headers, root, ["登录"])
    first_suite = client.post(f"{root}/smoke-suites/{first_suite['id']}/items", json={
        "revision": 1, "case_id": reviewed["id"], "chain_name": "登录", "estimated_seconds": 100}, headers=headers).json()
    second_suite = client.post(f"{root}/smoke-suites", json={"title": "预览验证", "target_version": "v2.0",
        "environment": "开发", "required_chains": ["登录"]}, headers=headers).json()
    second_suite = client.post(f"{root}/smoke-suites/{second_suite['id']}/items", json={
        "revision": 1, "case_id": reviewed["id"], "chain_name": "登录", "estimated_seconds": 200}, headers=headers).json()
    empty = client.get(f"{root}/execution-statistics").json()
    assert empty["run_count"] == 0 and empty["pass_rate"] is None
    for suite, result in ((first_suite, "passed"), (first_suite, "blocked"), (second_suite, "skipped")):
        run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
            "suite_revision": suite["revision"]}, headers=headers).json()
        updated = client.put(f"{root}/execution-runs/{run['id']}/cases/{run['items'][0]['id']}", json={
            "version": 1, "result": result, "actual_seconds": 80,
            "note": "环境异常" if result != "passed" else ""}, headers=headers)
        assert updated.status_code == 200
        assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    active = client.post(f"{root}/smoke-suites/{first_suite['id']}/runs", json={
        "suite_revision": first_suite["revision"]}, headers=headers).json()
    assert active["status"] == "active"
    stats = client.get(f"{root}/execution-statistics").json()
    assert stats["run_count"] == 3 and stats["case_executions"] == 3
    assert stats["results"] == {"not_run": 0, "passed": 1, "failed": 0, "blocked": 1, "skipped": 1}
    assert stats["pass_rate"] == 50.0
    assert stats["estimated_seconds"] == 400 and stats["actual_seconds"] == 240
    assert [item["run_id"] for item in stats["recent_runs"]] == [3, 2, 1]
    scoped = client.get(f"{root}/execution-statistics?suite_id={first_suite['id']}&target_version=v1.0&environment=预发布").json()
    assert scoped["run_count"] == 2 and scoped["pass_rate"] == 50.0
    assert client.get(f"{root}/execution-statistics?target_version=v2.0").json()["pass_rate"] is None
    assert client.get(f"{root}/execution-statistics?environment=missing").json()["run_count"] == 0
    other = client.post("/api/v1/projects", json={"name": "统计隔离"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/execution-statistics").json()["run_count"] == 0
    assert client.get(f"/api/v1/projects/{other}/execution-statistics?suite_id={first_suite['id']}").status_code == 404
    reader = client.post("/api/v1/users", json={"username": "stats-reader", "password": "reader-password-123",
        "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    client.post("/api/v1/auth/login", json={"username": "stats-reader", "password": "reader-password-123"},
                headers={"x-csrf-token": token})
    assert client.get(f"{root}/execution-statistics").json()["run_count"] == 3
    assert client.get(f"/api/v1/projects/{other}/execution-statistics").status_code == 403


def test_execution_statistics_verifies_reports_outside_recent_twenty(context):
    client, headers, root, cases = context
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": reviewed["id"],
        "chain_name": "登录", "estimated_seconds": 30}, headers=headers).json()
    oldest_id = None
    for _ in range(21):
        response = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
            "suite_revision": suite["revision"]}, headers=headers)
        assert response.status_code == 200, response.text
        run = response.json()
        oldest_id = oldest_id or run["id"]
        assert client.put(f"{root}/execution-runs/{run['id']}/cases/{run['items'][0]['id']}", json={
            "version": 1, "result": "passed", "actual_seconds": 10, "note": ""}, headers=headers).status_code == 200
        assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    url = f"{root}/execution-statistics"
    before = client.get(url).json()
    assert before["run_count"] == 21 and len(before["recent_runs"]) == 20
    assert oldest_id not in {item["run_id"] for item in before["recent_runs"]}
    with SessionLocal() as db:
        oldest = db.get(ExecutionReport, oldest_id)
        original = oldest.payload_sha256
        oldest.payload_sha256 = "0" * 64
        db.commit()
    assert client.get(url).status_code == 503
    assert client.get(url + "?target_version=missing").json()["run_count"] == 0
    with SessionLocal() as db:
        db.get(ExecutionReport, oldest_id).payload_sha256 = original
        db.commit()
    assert client.get(url).json()["run_count"] == 21


def test_project_delete_removes_execution_report_after_case_delete(context, tmp_path, monkeypatch):
    client, headers, root, cases = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": 1, "case_id": reviewed["id"], "chain_name": "登录", "estimated_seconds": 100}, headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    assert client.delete(f"{root}/cases/{reviewed['id']}", headers=headers).status_code == 409
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{run['items'][0]['id']}", json={
        "version": 1, "result": "failed", "actual_seconds": 90, "note": "登录失败"}, headers=headers).status_code == 200
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    defect = client.post(f"{root}/execution-defects", json={"run_id": run["id"],
        "run_case_id": run["items"][0]["id"], "title": "登录失败", "severity": "high"}, headers=headers)
    assert defect.status_code == 200, defect.text
    assert client.delete(f"{root}/cases/{reviewed['id']}", headers=headers).status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}/report").status_code == 200
    assert client.get(f"{root}/execution-defects?run_id={run['id']}").json()[0]["source_title"] == "登录"
    assert client.delete(root, headers=headers).status_code == 200
    with SessionLocal() as db:
        assert db.get(ExecutionReport, run["id"]) is None
        assert db.get(ExecutionDefect, defect.json()["id"]) is None
        assert db.query(ExecutionDefectEvent).count() == 0


def test_execution_defect_links_a_frozen_failed_case_and_preserves_report(context):
    client, headers, root, cases = context
    first, second = [review_yes(client, headers, root, case) for case in cases[:2]]
    suite = create_suite(client, headers, root)
    for case, chain in ((first, "登录"), (second, "退出")):
        suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
            "revision": suite["revision"], "case_id": case["id"], "chain_name": chain,
            "estimated_seconds": 120}, headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    failed_id, passed_id = [item["id"] for item in run["items"]]
    for item, result in ((failed_id, "failed"), (passed_id, "passed")):
        assert client.put(f"{root}/execution-runs/{run['id']}/cases/{item}", json={
            "version": 1, "result": result, "actual_seconds": 90,
            "note": "操作失败" if result == "failed" else ""}, headers=headers).status_code == 200
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    report_before = client.get(f"{root}/execution-runs/{run['id']}/report").content
    path = f"{root}/execution-defects"
    candidates = client.get(f"{path}/candidates?run_id={run['id']}")
    assert candidates.status_code == 200
    assert candidates.json() == {"report_completeness": "verified", "items": [{
        "run_case_id": failed_id, "original_id": "001", "title": "登录", "result": "failed",
        "case_version": first["version"]}]}
    assert client.get(f"{path}/candidates?run_id=999999").status_code == 404
    request = {"run_id": run["id"], "run_case_id": failed_id, "title": "  登录操作失败  ",
               "description": "服务返回异常", "severity": "high"}
    assert client.post(path, json={**request, "run_case_id": passed_id}, headers=headers).status_code == 422
    assert client.post(path, json={**request, "run_case_id": 999999}, headers=headers).status_code == 422
    assert client.post(path, json=request).status_code == 403
    created = client.post(path, json=request, headers=headers)
    assert created.status_code == 200, created.text
    defect = created.json()
    assert defect["source_original_id"] == "001" and defect["title"] == "登录操作失败"
    assert defect["version"] == 1 and defect["report_completeness"] == "verified"
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_before
    update_path = f"{path}/{defect['id']}"
    patch = {"version": 1, "title": "登录操作失败", "description": "定位服务异常", "severity": "high",
             "status": "resolved", "resolution": "  "}
    assert client.patch(update_path, json=patch, headers=headers).status_code == 422
    patch["resolution"] = "配置已修复"
    updated = client.patch(update_path, json=patch, headers=headers)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert client.patch(update_path, json=patch, headers=headers).status_code == 409
    events = client.get(f"{update_path}/events").json()
    assert len(events) == 2 and events[0]["before"] == {}
    assert events[1]["before"]["status"] == "open" and events[1]["after"]["status"] == "resolved"
    assert client.delete(f"{root}/cases/{first['id']}", headers=headers).status_code == 200
    assert client.get(f"{path}?run_id={run['id']}").json()[0]["source_case_version"] == first["version"]
    assert client.get(f"{root}/execution-runs/{run['id']}/report").content == report_before
    other = client.post("/api/v1/projects", json={"name": "缺陷隔离"}, headers=headers).json()["id"]
    wrong = f"/api/v1/projects/{other}/execution-defects"
    assert client.get(f"{wrong}?run_id={run['id']}").status_code == 404
    assert client.get(f"{wrong}/{defect['id']}/events").status_code == 404
    assert client.post(wrong, json=request, headers=headers).status_code == 404
    reader = client.post("/api/v1/users", json={"username": "defect-reader",
        "password": "reader-password-123", "role": "reader"}, headers=headers).json()
    assert client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers).status_code == 200
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    logged = client.post("/api/v1/auth/login", json={"username": "defect-reader",
        "password": "reader-password-123"}, headers={"x-csrf-token": token}).json()
    reader_headers = {"x-csrf-token": logged["csrf"]}
    assert client.get(f"{path}?run_id={run['id']}").json()[0]["id"] == defect["id"]
    assert client.get(f"{path}/candidates?run_id={run['id']}").json()["items"][0]["run_case_id"] == failed_id
    assert client.get(f"{update_path}/events").status_code == 200
    assert client.post(path, json=request, headers=reader_headers).status_code == 403
    assert client.patch(update_path, json={**patch, "version": 2}, headers=reader_headers).status_code == 403
    assert client.get(f"{wrong}?run_id={run['id']}").status_code == 403
    assert client.get(f"{wrong}/candidates?run_id={run['id']}").status_code == 403
