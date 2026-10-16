import io
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, Case, ImportBatch, Membership, Project, User, engine  # noqa: E402
from api.main import app, apply_review  # noqa: E402


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as active:
        yield active


def login(client, username="admin", password="test-admin-password-123"):
    token = client.get("/api/v1/auth/csrf").json()["token"]
    result = client.post("/api/v1/auth/login", json={"username": username, "password": password}, headers={"x-csrf-token": token})
    assert result.status_code == 200, result.text
    return {"x-csrf-token": result.json()["csrf"]}


def source():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["测试用例ID", "用例名称", "用例等级", "是否冒烟用例"])
    sheet.append(["001", "登录", "1", None])
    sheet.append(["002", "退出", "3", "否"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def setup_project(client):
    admin = login(client)
    project = client.post("/api/v1/projects", json={"name": "Team"}, headers=admin).json()["id"]
    users = {}
    for username, role in (("alice", "reviewer"), ("bob", "reviewer"), ("reader", "reader")):
        created = client.post("/api/v1/users", json={"username": username, "password": f"{username}-password-123", "role": role}, headers=admin).json()
        users[username] = created["id"]
        client.post(f"/api/v1/projects/{project}/members", json={"user_id": created["id"]}, headers=admin)
    root = f"/api/v1/projects/{project}"
    preview = client.post(f"{root}/imports/preview", files={"file": ("cases.xlsx", source())}, headers=admin).json()
    assert client.post(f"{root}/imports/{preview['id']}/confirm", headers=admin).status_code == 200
    ids = [item["id"] for item in client.get(f"{root}/cases").json()["items"]]
    return admin, root, users, ids


def switch(client, current_headers, username, password):
    client.post("/api/v1/auth/logout", headers=current_headers)
    return login(client, username, password)


def test_assignment_review_comments_and_project_isolation(client):
    admin, root, users, ids = setup_project(client)
    case_id = ids[0]
    assert client.put(f"{root}/cases/{case_id}/assignment", json={"version": 1, "assignee_id": users["alice"]}).status_code == 403
    assigned = client.put(f"{root}/cases/{case_id}/assignment", json={"version": 1, "assignee_id": users["alice"]}, headers=admin)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assignee"] == "alice" and assigned.json()["version"] == 2
    assert client.put(f"{root}/cases/{case_id}/assignment", json={"version": 1, "assignee_id": users["bob"]}, headers=admin).status_code == 409
    assert client.put(f"{root}/cases/{case_id}/assignment", json={"version": 2, "assignee_id": users["reader"]}, headers=admin).status_code == 422
    other = client.post("/api/v1/projects", json={"name": "Other"}, headers=admin).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/cases/{case_id}/activity").status_code == 404
    alice = switch(client, admin, "alice", "alice-password-123")
    assert client.get(f"{root}/overview").json()["my_pending_count"] == 1
    filtered = client.get(f"{root}/cases?assignee=me&status=pending").json()
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == case_id
    assert client.get(f"{root}/cases?assignee=unknown").status_code == 400
    comment = client.post(f"{root}/cases/{case_id}/comments", json={"text": "  需核对账号限制  "}, headers=alice)
    assert comment.status_code == 200 and comment.json()["text"] == "需核对账号限制"
    assert client.post(f"{root}/cases/{case_id}/comments", json={"text": "  "}, headers=alice).status_code == 422
    reviewed = client.put(f"{root}/cases/{case_id}/review", json={"version": 2, "final_level": "1", "final_smoke": "", "reason": "保留待判定"}, headers=alice)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["version"] == 3 and reviewed.json()["assignee"] == "alice"
    assert client.get(f"{root}/overview").json()["my_pending_count"] == 0
    assert client.put(f"{root}/cases/{case_id}/review", json={"version": 2, "final_level": "2", "final_smoke": "否", "reason": "旧版本"}, headers=alice).status_code == 409
    history = client.get(f"{root}/cases/{case_id}/activity").json()
    assert [entry["action"] for entry in history["events"]] == ["review", "assignment"]
    assert history["comments"][0]["actor"] == "alice"
    alice = switch(client, alice, "bob", "bob-password-123")
    assert client.put(f"{root}/cases/{case_id}/review", json={"version": 3, "final_level": "2", "final_smoke": "否", "reason": "越权"}, headers=alice).status_code == 403
    assert client.put(f"{root}/cases/{case_id}/assignment", json={"version": 3, "assignee_id": users["bob"]}, headers=alice).status_code == 403
    batch = client.put(f"{root}/cases/batch-review", json={"items": [
        {"id": case_id, "version": 3, "final_level": "2", "final_smoke": "否"},
        {"id": ids[1], "version": 1, "final_level": "3", "final_smoke": "否"},
    ], "reason": "按分派逐条核对"}, headers=alice)
    assert [entry["status"] for entry in batch.json()["results"]] == ["forbidden", "saved"]


def test_batch_reports_individual_conflicts_and_downgrade_releases_owner(client):
    admin, root, users, ids = setup_project(client)
    assigned = client.put(f"{root}/cases/{ids[0]}/assignment", json={"version": 1, "assignee_id": users["alice"]}, headers=admin)
    assert assigned.status_code == 200
    payload = {"items": [
        {"id": ids[0], "version": 1, "final_level": "1", "final_smoke": ""},
        {"id": ids[1], "version": 1, "final_level": "3", "final_smoke": "否"},
        {"id": ids[1], "version": 1, "final_level": "3", "final_smoke": "否"},
        {"id": 9999, "version": 1, "final_level": "2", "final_smoke": ""},
    ], "reason": "批量核对"}
    result = client.put(f"{root}/cases/batch-review", json=payload, headers=admin)
    assert result.status_code == 200, result.text
    assert [item["status"] for item in result.json()["results"]] == ["conflict", "saved", "invalid", "not_found"]
    assert result.json()["saved"] == 1 and result.json()["conflicts"] == 1
    assert client.get(f"{root}/cases/{ids[0]}").json()["review_status"] == "pending"
    assert client.get(f"{root}/cases/{ids[1]}").json()["review_status"] == "reviewed"
    assert client.put(f"{root}/cases/batch-review", json={"items": payload["items"][:1], "reason": " "}, headers=admin).status_code == 422
    updated = client.patch(f"{root}/members/{users['alice']}", json={"role": "reader"}, headers=admin)
    assert updated.status_code == 200
    released = client.get(f"{root}/cases/{ids[0]}").json()
    assert released["assignee_id"] is None and released["version"] == 3
    assert client.get(f"{root}/cases/{ids[0]}/activity").json()["events"][0]["reason"] == "成员权限变更，负责人已清除"
    reader = switch(client, admin, "alice", "alice-password-123")
    assert client.put(f"{root}/cases/batch-review", json=payload, headers=reader).status_code == 403
    assert client.get(f"{root}/cases/{ids[0]}/activity").status_code == 200


def test_reviewer_can_claim_and_release_only_self(client):
    admin, root, users, ids = setup_project(client)
    alice = switch(client, admin, "alice", "alice-password-123")
    claimed = client.put(f"{root}/cases/{ids[0]}/assignment", json={"version": 1, "assignee_id": users["alice"]}, headers=alice)
    assert claimed.status_code == 200 and claimed.json()["version"] == 2
    assert client.put(f"{root}/cases/{ids[1]}/assignment", json={"version": 1, "assignee_id": users["bob"]}, headers=alice).status_code == 403
    released = client.put(f"{root}/cases/{ids[0]}/assignment", json={"version": 2, "assignee_id": None}, headers=alice)
    assert released.status_code == 200 and released.json()["assignee_id"] is None


def test_case_delete_single_and_batch_are_project_scoped(client):
    admin, root, users, ids = setup_project(client)
    # A reader cannot use either delete surface.
    reader = switch(client, admin, "reader", "reader-password-123")
    assert client.delete(f"{root}/cases/{ids[0]}", headers=reader).status_code == 403
    assert client.request("DELETE", f"{root}/cases/batch-delete", json={"case_ids": ids}, headers=reader).status_code == 403
    admin = switch(client, reader, "admin", "test-admin-password-123")

    single = client.delete(f"{root}/cases/{ids[0]}", headers=admin)
    assert single.status_code == 200 and single.json() == {"id": ids[0], "deleted": 1}
    assert client.get(f"{root}/cases/{ids[0]}").status_code == 404

    batch = client.request("DELETE", f"{root}/cases/batch-delete", json={"case_ids": [ids[1], ids[1], 99999]}, headers=admin)
    assert batch.status_code == 200
    assert [(entry["id"], entry["status"]) for entry in batch.json()["results"]] == [
        (ids[1], "deleted"), (ids[1], "invalid"), (99999, "not_found")
    ]
    assert batch.json()["deleted"] == 1
    assert client.get(f"{root}/cases").json()["total"] == 0


def test_two_sessions_cannot_overwrite_same_version(tmp_path):
    file_engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(file_engine)
    session = sessionmaker(bind=file_engine, expire_on_commit=False)
    with session() as db:
        db.add(Project(id=1, name="并发项目"))
        db.add_all([User(id=1, username="alice", password_hash="unused", role="reviewer"),
                    User(id=2, username="bob", password_hash="unused", role="reviewer")])
        db.add_all([Membership(project_id=1, user_id=1, role="reviewer"), Membership(project_id=1, user_id=2, role="reviewer")])
        db.add(ImportBatch(id=1, project_id=1, filename="source.xlsx", headers=["测试用例ID"], rows=[["001"]], warnings=[], status="done"))
        db.add(Case(id=1, project_id=1, import_id=1, original_id="001", title="并发审核", original=["001"]))
        db.commit()
    barrier = Barrier(2)

    def submit(user_id, level):
        with session() as db:
            user = db.get(User, user_id)
            barrier.wait(timeout=5)
            return apply_review(1, 1, 1, level, "", "双人同时审核", user, "reviewer", db)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit, 1, "1")
        second = pool.submit(submit, 2, "2")
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == ["conflict", "saved"]
    with session() as db:
        item = db.get(Case, 1)
        assert item.version == 2 and item.final_level in {"1", "2"}
    file_engine.dispose()
