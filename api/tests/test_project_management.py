import os
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api import ai_routes, conversion_routes, governance_routes, main as api_main  # noqa: E402
from api.db import (Base, CodexSession, ConversionRecord, GovernanceExportJob, ImportBatch, Project, ProjectSkill, SessionLocal,
                    SourceDocument, User, engine, utcnow)  # noqa: E402
from api.main import app, writable  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


def login(client):
    token = client.get("/api/v1/auth/csrf").json()["token"]
    response = client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "test-admin-password-123",
    }, headers={"x-csrf-token": token})
    assert response.status_code == 200, response.text
    return {"x-csrf-token": response.json()["csrf"]}


def test_project_lifecycle_and_archived_filter(client):
    csrf = login(client)
    project = client.post("/api/v1/projects", json={"name": "原项目"}, headers=csrf).json()
    project_id = project["id"]
    renamed = client.patch(f"/api/v1/projects/{project_id}", json={"name": "新名称"}, headers=csrf)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新名称"

    archived = client.post(f"/api/v1/projects/{project_id}/archive", headers=csrf)
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert client.get("/api/v1/projects").json() == []
    assert client.get("/api/v1/projects?include_archived=true").json()[0]["archived"] is True
    restored = client.post(f"/api/v1/projects/{project_id}/restore", headers=csrf)
    assert restored.status_code == 200 and restored.json()["archived"] is False


def test_duplicate_does_not_copy_members_or_case_data(client):
    csrf = login(client)
    source = client.post("/api/v1/projects", json={"name": "源"}, headers=csrf).json()
    clone = client.post(f"/api/v1/projects/{source['id']}/duplicate", json={"name": "副本"}, headers=csrf)
    assert clone.status_code == 200, clone.text
    assert clone.json()["name"] == "副本"
    assert {item["name"] for item in client.get("/api/v1/projects").json()} == {"源", "副本"}


def test_delete_project_cascades_owned_rows(client):
    csrf = login(client)
    project = client.post("/api/v1/projects", json={"name": "待删"}, headers=csrf).json()
    project_id = project["id"]
    deleted = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/v1/projects/{project_id}/overview").status_code == 404


def _codex_workspace(project_id: int, *, status: str = "ready"):
    session_id = uuid.uuid4().hex
    workspace = ai_routes._create_codex_session_workspace(session_id)
    with SessionLocal() as db:
        db.add(CodexSession(id=session_id, project_id=project_id, user_id=1,
                            title="隔离会话", status=status, model="", working_directory=workspace,
                            created_at=utcnow(), updated_at=utcnow()))
        db.commit()
    return session_id, ai_routes.Path(workspace)


def test_project_delete_waits_for_codex_turn_and_cleans_its_workspace(client):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "执行中的 Codex"}, headers=csrf).json()["id"]
    session_id, workspace = _codex_workspace(project_id, status="running")
    private_file = workspace / "generated.txt"
    private_file.write_bytes(b"session output")
    blocked = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert blocked.status_code == 409 and private_file.exists()
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at is None
        db.get(CodexSession, session_id).status = "ready"
        db.commit()
    removed = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert removed.status_code == 200, removed.text
    assert not workspace.exists()


def test_project_delete_retries_failed_codex_workspace_cleanup(client, monkeypatch):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "Codex 清理重试"}, headers=csrf).json()["id"]
    session_id, workspace = _codex_workspace(project_id)
    (workspace / "generated.txt").write_bytes(b"session output")
    original = api_main.shutil.rmtree

    def reject_workspace(path, *args, **kwargs):
        if ai_routes.Path(path) == workspace:
            raise OSError("injected cleanup failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(api_main.shutil, "rmtree", reject_workspace)
        failed = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert failed.status_code == 503 and workspace.exists()
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at
        assert db.get(CodexSession, session_id)
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200
    assert not workspace.exists()


def test_project_delete_rejects_symlinked_codex_workspace(client, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "Codex 链接"}, headers=csrf).json()["id"]
    _, workspace = _codex_workspace(project_id)
    workspace.rmdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "generated.txt").write_bytes(b"keep")
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This host cannot create symlinks")
    failed = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert failed.status_code == 503
    assert (outside / "generated.txt").read_bytes() == b"keep"
    workspace.unlink()
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200


def test_project_delete_rejects_junction_codex_workspace(client, monkeypatch):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "Codex junction"}, headers=csrf).json()["id"]
    _, workspace = _codex_workspace(project_id)
    (workspace / "keep.txt").write_text("private", encoding="utf-8")
    original = api_main.is_link_like
    with monkeypatch.context() as patch:
        patch.setattr(api_main, "is_link_like", lambda path: path == workspace or original(path))
        response = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert response.status_code == 503
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "private"
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200


@pytest.mark.parametrize("kind", ["documents", "conversions"])
def test_project_delete_rejects_junction_in_owned_file_directory(client, monkeypatch, kind):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": f"{kind} junction"}, headers=csrf).json()["id"]
    root = ai_routes.DOCUMENT_DIR if kind == "documents" else conversion_routes.DATA_DIR
    folder = root / str(project_id)
    folder.mkdir(parents=True)
    artifact = folder / "owned.bin"
    artifact.write_bytes(b"keep")
    with SessionLocal() as db:
        if kind == "documents":
            db.add(SourceDocument(id="owned", project_id=project_id, user_id=1,
                                  filename="owned.md", extracted_text="sample", warnings=[]))
        else:
            db.add(ConversionRecord(id="owned", project_id=project_id, user_id=1,
                                    source_filename="source.xlsx", output_filename="owned.xlsx",
                                    conversion_mode="excel_to_excel", output_mode="after",
                                    media_type="application/octet-stream", app_version="test",
                                    row_count=1, file_size=4, preview={}))
        db.commit()
    original = ai_routes.Path.is_junction
    with monkeypatch.context() as patch:
        patch.setattr(ai_routes.Path, "is_junction", lambda self: self == folder or original(self))
        response = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert response.status_code == 503
    assert artifact.read_bytes() == b"keep"
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200
    assert not artifact.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction runtime contract")
def test_project_delete_rejects_real_windows_document_junction(client, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "Windows junction"}, headers=csrf).json()["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    artifact = outside / "owned.bin"
    artifact.write_bytes(b"private")
    ai_routes.DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
    linked = ai_routes.DOCUMENT_DIR / str(project_id)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert linked.is_junction()
    with SessionLocal() as db:
        db.add(SourceDocument(id="owned", project_id=project_id, user_id=1,
                              filename="owned.md", extracted_text="sample", warnings=[]))
        db.commit()
    response = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert response.status_code == 503
    assert artifact.read_bytes() == b"private"
    linked.rmdir()
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200
    assert artifact.read_bytes() == b"private"


def test_project_delete_never_removes_legacy_external_codex_workspace(client, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "历史 Codex"}, headers=csrf).json()["id"]
    external = tmp_path / "user-checkout"
    external.mkdir()
    (external / "keep.txt").write_text("user data")
    with SessionLocal() as db:
        db.add(CodexSession(id="legacy-codex", project_id=project_id, user_id=1,
                            title="旧会话", status="ready", model="", working_directory=str(external),
                            created_at=utcnow(), updated_at=utcnow()))
        db.commit()
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200
    assert (external / "keep.txt").read_text() == "user data"


def test_delete_project_preserves_retryable_intent_if_private_files_cannot_be_cleaned(client):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "待清理"}, headers=csrf).json()["id"]
    directory = ai_routes.DOCUMENT_DIR / str(project_id)
    directory.mkdir(parents=True)
    extra = directory / "unattributed.bin"
    extra.write_bytes(b"private")
    failed = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert failed.status_code == 503
    visible = client.get("/api/v1/projects").json()
    assert visible[0]["id"] == project_id and visible[0]["deletion_pending_at"]
    assert extra.exists()
    extra.unlink()
    retry = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert retry.status_code == 200
    assert client.get("/api/v1/projects").json() == []


def test_delete_project_cleans_governance_artifacts_and_rows(client):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "治理文件"}, headers=csrf).json()["id"]
    root = f"/api/v1/projects/{project_id}/governance"
    content = "测试用例ID,用例名称\n001,新增用例\n".encode()
    preview = client.post(root + "/imports/preview", files={"file": ("cases.csv", content)}, headers=csrf)
    assert preview.status_code == 200, preview.text
    confirmed = client.post(root + f"/imports/{preview.json()['id']}/confirm", headers=csrf)
    assert confirmed.status_code == 200, confirmed.text
    export = client.post(root + "/exports", headers=csrf)
    assert export.status_code == 200, export.text
    with SessionLocal() as db:
        snapshot = db.get(ImportBatch, preview.json()["id"]).snapshot_path
        exported = db.get(GovernanceExportJob, export.json()["id"]).path
    assert snapshot and exported
    assert governance_routes.Path(snapshot).exists() and governance_routes.Path(exported).exists()
    assert governance_routes.Path(snapshot).parent.name == str(project_id)
    assert governance_routes.Path(exported).parent.name == str(project_id)
    download = client.get(root + f"/exports/{export.json()['id']}/download")
    assert download.status_code == 200 and download.content == governance_routes.Path(exported).read_bytes()
    deleted = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert deleted.status_code == 200, deleted.text
    assert not governance_routes.Path(snapshot).exists() and not governance_routes.Path(exported).exists()
    assert not governance_routes.Path(snapshot).parent.exists()
    with SessionLocal() as db:
        assert db.get(ImportBatch, preview.json()["id"]) is None
        assert db.get(GovernanceExportJob, export.json()["id"]) is None


def test_delete_project_preserves_shared_legacy_snapshot(client, tmp_path):
    csrf = login(client)
    ids = [client.post("/api/v1/projects", json={"name": f"共享-{i}"}, headers=csrf).json()["id"] for i in range(2)]
    root = tmp_path / "governance-snapshots"
    root.mkdir()
    snapshot = root / ("a" * 64 + ".csv")
    snapshot.write_bytes(b"original")
    with SessionLocal() as db:
        for project_id in ids:
            db.add(ImportBatch(project_id=project_id, filename="original.csv", headers=[], rows=[], warnings=[],
                               kind="governance", snapshot_path=str(snapshot)))
        db.commit()
    assert client.delete(f"/api/v1/projects/{ids[0]}", headers=csrf).status_code == 200
    assert snapshot.read_bytes() == b"original"
    assert client.delete(f"/api/v1/projects/{ids[1]}", headers=csrf).status_code == 200
    assert not snapshot.exists()


@pytest.mark.parametrize("unsafe", ["unattributed", "symlink", "unknown_path"])
def test_delete_project_rejects_unowned_governance_artifacts(client, tmp_path, unsafe):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": unsafe}, headers=csrf).json()["id"]
    root = tmp_path / "governance-snapshots"
    project_dir = root / str(project_id)
    project_dir.mkdir(parents=True)
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"private")
    path = project_dir / ("b" * 64 + ".csv")
    if unsafe == "unattributed":
        path.write_bytes(b"private")
    elif unsafe == "symlink":
        path.symlink_to(outside)
    else:
        path = outside
    if unsafe != "unattributed":
        with SessionLocal() as db:
            db.add(ImportBatch(project_id=project_id, filename="source.csv", headers=[], rows=[], warnings=[],
                               kind="governance", snapshot_path=str(path)))
            db.commit()
    response = client.delete(f"/api/v1/projects/{project_id}", headers=csrf)
    assert response.status_code == 503, response.text
    assert outside.read_bytes() == b"private"
    assert path.is_symlink() if unsafe == "symlink" else path.exists()
    with SessionLocal() as db:
        assert db.get(Project, project_id).deletion_pending_at


def test_governance_preview_rechecks_deletion_after_parsing(client, monkeypatch, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "解析中"}, headers=csrf).json()["id"]
    original = governance_routes._parse_file

    def parse_after_deletion(filename, content):
        result = original(filename, content)
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return result

    monkeypatch.setattr(governance_routes, "_parse_file", parse_after_deletion)
    response = client.post(f"/api/v1/projects/{project_id}/governance/imports/preview",
                           files={"file": ("source.csv", "测试用例ID,用例名称\n001,新增用例\n".encode())}, headers=csrf)
    assert response.status_code == 409, response.text
    assert not (tmp_path / "governance-snapshots" / str(project_id)).exists()
    with SessionLocal() as db:
        assert db.query(ImportBatch).filter_by(project_id=project_id).count() == 0


def test_governance_export_rechecks_deletion_after_rendering(client, monkeypatch, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "导出中"}, headers=csrf).json()["id"]
    original = governance_routes.Workbook.save

    def save_after_deletion(workbook, target):
        original(workbook, target)
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()

    monkeypatch.setattr(governance_routes.Workbook, "save", save_after_deletion)
    response = client.post(f"/api/v1/projects/{project_id}/governance/exports", headers=csrf)
    assert response.status_code == 409, response.text
    assert not (tmp_path / "governance-snapshots" / str(project_id)).exists()
    with SessionLocal() as db:
        assert db.query(GovernanceExportJob).filter_by(project_id=project_id).count() == 0


def test_governance_write_failure_does_not_leave_file_or_batch(client, monkeypatch, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "写入失败"}, headers=csrf).json()["id"]
    original = governance_routes.write_private_atomic

    def fail_after_write(path, data, *, root):
        original(path, data, root=root)
        raise OSError("injected persist failure")

    monkeypatch.setattr(governance_routes, "write_private_atomic", fail_after_write)
    with pytest.raises(OSError, match="injected persist failure"):
        client.post(f"/api/v1/projects/{project_id}/governance/imports/preview",
                    files={"file": ("source.csv", "测试用例ID,用例名称\n001,新增用例\n".encode())}, headers=csrf)
    assert not list((tmp_path / "governance-snapshots" / str(project_id)).glob("*"))
    with SessionLocal() as db:
        assert db.query(ImportBatch).filter_by(project_id=project_id).count() == 0


def test_governance_download_does_not_follow_symlinks(client, tmp_path):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "下载保护"}, headers=csrf).json()["id"]
    root = tmp_path / "governance-snapshots" / str(project_id)
    root.mkdir(parents=True)
    outside = tmp_path / "private.xlsx"
    outside.write_bytes(b"private")
    link = root / ("export-" + "a" * 32 + ".xlsx")
    link.symlink_to(outside)
    with SessionLocal() as db:
        db.add(GovernanceExportJob(project_id=project_id, path=str(link), created_by=1))
        db.commit()
        job_id = db.query(GovernanceExportJob.id).filter_by(project_id=project_id).scalar()
    response = client.get(f"/api/v1/projects/{project_id}/governance/exports/{job_id}/download")
    assert response.status_code == 404
    assert outside.read_bytes() == b"private"


def test_upload_rechecks_deletion_intent_after_document_extraction(client, monkeypatch):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "提取中"}, headers=csrf).json()["id"]
    extract = ai_routes.extract

    def delete_during_extraction(filename, data):
        result = extract(filename, data)
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return result

    monkeypatch.setattr(ai_routes, "extract", delete_during_extraction)
    response = client.post(f"/api/v1/projects/{project_id}/documents",
                           files={"file": ("req.md", b"# Requirement")}, headers=csrf)
    assert response.status_code == 409, response.text
    assert not (ai_routes.DOCUMENT_DIR / str(project_id)).exists()
    with SessionLocal() as db:
        assert db.query(SourceDocument).filter_by(project_id=project_id).count() == 0


def test_file_backed_sqlite_writer_fence_blocks_deletion_intent_until_commit(tmp_path):
    isolated_engine = create_engine(f"sqlite:///{tmp_path / 'fence.db'}", connect_args={"timeout": 0.1})
    Base.metadata.create_all(isolated_engine)
    sessions = sessionmaker(bind=isolated_engine)
    try:
        with sessions() as db:
            user = User(username="writer", password_hash="test", role="admin")
            project = Project(name="writer fence")
            db.add_all([user, project])
            db.commit()
            user_id, project_id = user.id, project.id
        with sessions() as writer, sessions() as deleter:
            writable(writer.get(User, user_id), project_id, writer, fence=True)
            with pytest.raises(OperationalError, match="database is locked"):
                deleter.execute(update(Project).where(Project.id == project_id).values(deletion_pending_at=utcnow()))
            deleter.rollback()
            writer.commit()
            deleter.execute(update(Project).where(Project.id == project_id).values(deletion_pending_at=utcnow()))
            deleter.commit()
    finally:
        isolated_engine.dispose()


def test_skills_read_does_not_recreate_project_rows_after_deletion_intent(client, monkeypatch):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "技能注册竞态"}, headers=csrf).json()["id"]
    with SessionLocal() as db:
        skill = db.query(ProjectSkill).filter_by(project_id=project_id).first()
        skill_id = skill.skill_id
        db.delete(skill)
        db.commit()
    discover = api_main.discover_skills

    def mark_before_materialization():
        specs = discover()
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return specs

    monkeypatch.setattr(api_main, "discover_skills", mark_before_materialization)
    response = client.get(f"/api/v1/projects/{project_id}/skills", headers=csrf)
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(ProjectSkill, (project_id, skill_id)) is None


def test_archived_project_pending_deletion_remains_visible_for_retry(client):
    csrf = login(client)
    project_id = client.post("/api/v1/projects", json={"name": "历史项目"}, headers=csrf).json()["id"]
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        project.archived_at = "2026-10-25T00:00:00Z"
        project.deletion_pending_at = "2026-10-25T00:00:00Z"
        db.commit()
    visible = client.get("/api/v1/projects").json()
    assert [item["id"] for item in visible] == [project_id]
    assert visible[0]["deletion_pending_at"]
    assert client.delete(f"/api/v1/projects/{project_id}", headers=csrf).status_code == 200

