import io
import os

import openpyxl
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, engine  # noqa: E402
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
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{first_item['id']}", json={
        "version": 2, "result": "failed", "actual_seconds": 400, "note": "不允许覆盖"}, headers=headers).status_code == 409
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 409
    other = client.post("/api/v1/projects", json={"name": "其他项目"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/smoke-suites/{suite['id']}").status_code == 404
    assert client.get(f"/api/v1/projects/{other}/execution-runs/{run['id']}").status_code == 404
    reader = client.post("/api/v1/users", json={"username": "reader", "password": "reader-password-123", "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    logged = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password-123"}, headers={"x-csrf-token": token}).json()
    readonly = {"x-csrf-token": logged["csrf"]}
    assert client.get(f"{root}/smoke-suites/{suite['id']}").status_code == 200
    assert client.get(f"{root}/execution-runs/{run['id']}").status_code == 200
    assert client.post(f"{root}/smoke-suites", json={"title": "无权", "target_version": "v2", "environment": "test"}, headers=readonly).status_code == 403
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=readonly).status_code == 403


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
