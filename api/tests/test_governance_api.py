import io
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("AI_STUDIO_SESSION_SECRET", "test-only-session-secret-32-characters-long")
os.environ.setdefault("AI_STUDIO_ADMIN_PASSWORD", "test-admin-password-123")

import openpyxl
import pytest
from fastapi.testclient import TestClient

from api.db import Base, engine
from api.main import app


@pytest.fixture
def governance_client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        csrf = client.get("/api/v1/auth/csrf").json()["token"]
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-admin-password-123"}, headers={"x-csrf-token": csrf})
        if login.status_code != 200:
            pytest.skip("test database does not have the shared admin fixture")
        headers = {"x-csrf-token": login.json()["csrf"]}
        project = client.post("/api/v1/projects", json={"name": "治理 API"}, headers=headers).json()["id"]
        yield client, headers, f"/api/v1/projects/{project}/governance"


def _source():
    book = openpyxl.Workbook(); sheet = book.active; sheet.title = "Tcase_list"
    sheet.append(["测试用例ID", "用例名称", "用例类型", "用例状态", "用例等级", "创建人", "用例目录", "用例步骤", "前置条件", "预期结果", "备注", "是否冒烟用例", "端类型"])
    root = "测试用例新(该目录下的所有用例都需要评审) - 监控中心模块用例 - 02_功能测试 - 00_能力基线 - 01_设备接入"
    for cid, step in (("001", "1、添加"), ("002", "1、添加")):
        sheet.append([cid, "添加摄像机", "业务功能用例", "正常", "1", "tester", root, step, "设备在线", "1、添加成功", "", "否", "Web"])
    output = io.BytesIO(); book.save(output); return output.getvalue()


def test_governance_import_dashboard_and_reversible_apply(governance_client):
    client, headers, root = governance_client
    preview = client.post(root + "/imports/preview", files={"file": ("snapshot.xlsx", _source())}, headers=headers)
    assert preview.status_code == 200, preview.text
    confirmed = client.post(root + f"/imports/{preview.json()['id']}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    detail = client.get(root + "/duplicates").json()["items"][0]
    full = client.get(root + f"/duplicates/{detail['id']}").json()
    member = full["members"][0]
    assert member["reason"]
    edited = client.put(root + f"/cases/{member['case_id']}", json={
        "directory": "测试用例新(该目录下的所有用例都需要评审) - 监控中心模块用例 - 02_功能测试",
        "endpoint_type": "win/mac/web",
    }, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["case"]["endpoint_type"] == "win/mac/web"
    dashboard = client.get(root + "/dashboard").json()
    assert dashboard["metrics"]["duplicate_group_count"] == 1
    group = client.get(root + "/duplicates").json()["items"][0]
    applied = client.post(root + "/duplicates/apply", json={"group_ids": [group["id"]], "preview": True}, headers=headers)
    assert applied.status_code == 200 and applied.json()["preview"] is True
    applied = client.post(root + "/duplicates/apply", json={"group_ids": [group["id"]]}, headers=headers)
    assert applied.status_code == 200 and applied.json()["changed"] >= 1
    undone = client.post(root + f"/duplicates/{group['id']}/undo", headers=headers)
    assert undone.status_code == 200 and undone.json()["restored"] >= 1
    exported = client.post(root + "/exports?format=xls", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.json()["selfcheck"]["readback"] is True
    assert exported.json()["selfcheck"]["readback_rows"] == 2
    directories = client.get(root + "/directories/tree")
    assert directories.status_code == 200 and directories.json()[0]["case_ids"]
    validation = client.post(root + "/validations", json={"filename": "new.csv", "rows": [{
        "测试用例ID": "", "用例名称": "新增唯一场景", "用例类型": "业务功能用例", "用例状态": "正常",
        "用例等级": "1", "创建人": "tester", "用例目录": "测试用例新(该目录下的所有用例都需要评审) - 监控中心模块用例 - 02_功能测试",
        "用例步骤": "1、打开页面", "前置条件": "设备在线", "预期结果": "1、页面打开", "备注": "[迭代:20261021][需求:REQ-1]",
        "是否冒烟用例": "否", "端类型": "Web"
    }]}, headers=headers)
    assert validation.status_code == 200 and validation.json()["can_promote"] is True
    promoted = client.post(root + f"/validations/{validation.json()['id']}/promote", headers=headers)
    assert promoted.status_code == 200 and promoted.json()["status"] == "promoted"



def test_directory_move_and_whitelist_apply(governance_client):
    """Directory migration and whitelist application must not fail with 500.

    Both endpoints previously referenced request fields or helpers that were
    never declared, so any UI interaction returned a server error.
    """
    client, headers, root = governance_client
    preview = client.post(root + "/imports/preview", files={"file": ("snapshot.xlsx", _source())}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert client.post(root + f"/imports/{preview.json()['id']}/confirm", headers=headers).status_code == 200
    tree = client.get(root + "/directories/tree")
    assert tree.status_code == 200, tree.text
    source = next(node for node in tree.json() if node["case_ids"])
    directory = source["path"]
    case_ids = source["case_ids"]
    assert case_ids

    moved = client.post(root + "/directories/move", json={"case_ids": case_ids, "directory": directory}, headers=headers)
    assert moved.status_code == 200, moved.text
    assert moved.json()["changed"] == len(case_ids)
    moved_tree = client.get(root + "/directories/tree").json()
    assert sum(node["case_count"] for node in moved_tree if node["path"] == directory) == len(case_ids)

    whitelist = client.post(root + "/directories/whitelist/apply", json={"paths": [directory], "state": "active"}, headers=headers)
    assert whitelist.status_code == 200, whitelist.text
    assert whitelist.json()["changed"] == 1
    assert whitelist.json()["state"] == "active"


def test_delete_all_cases_endpoint(governance_client):
    client, headers, root = governance_client
    preview = client.post(root + "/imports/preview", files={"file": ("snapshot.xlsx", _source())}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert client.post(root + f"/imports/{preview.json()['id']}/confirm", headers=headers).status_code == 200
    # The operation is project-scoped and requires the same CSRF/write gate as
    # single and batch deletion.
    deleted = client.delete(root.replace("/governance", "") + "/cases/all", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == 2


def test_delete_all_refreshes_governance_dashboard(governance_client):
    client, headers, root = governance_client
    preview = client.post(root + "/imports/preview", files={"file": ("snapshot.xlsx", _source())}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert client.post(root + f"/imports/{preview.json()['id']}/confirm", headers=headers).status_code == 200
    base = root.replace("/governance", "")
    assert client.delete(base + "/cases/all", headers=headers).json()["deleted"] == 2
    dashboard = client.get(root + "/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["batch"] is None
    assert dashboard.json()["metrics"]["case_count"] == 0
