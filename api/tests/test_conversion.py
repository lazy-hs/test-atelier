import io
import hashlib
import os
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AI_STUDIO_SESSION_SECRET"] = "test-only-session-secret-32-characters-long"
os.environ["AI_STUDIO_ADMIN_PASSWORD"] = "test-admin-password-123"

from api.db import Base, ConversionRecord, Project, SessionLocal, engine, utcnow  # noqa: E402
from api.main import app  # noqa: E402
from api import ai_routes, conversion_routes  # noqa: E402
from api.conversion.service import preview as convert_preview  # noqa: E402


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        token = client.get("/api/v1/auth/csrf").json()["token"]
        logged = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test-admin-password-123"}, headers={"x-csrf-token": token})
        headers = {"x-csrf-token": logged.json()["csrf"]}
        project = client.post("/api/v1/projects", json={"name": "P"}, headers=headers).json()
        yield client, headers, f"/api/v1/projects/{project['id']}"


def source_file():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["用例目录", "用例名称", "用例等级", "前置条件", "用例步骤", "预期结果", "是否是冒烟用例"])
    sheet.append(["登录", "账号登录", "2", "账号存在", "输入账号", "进入首页", None])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def sample_xmind():
    xml = b'<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0"><sheet><topic><title>Root</title><children><topics type="attached"><topic><title>Login</title><children><topics type="attached"><topic><title>Works</title><marker-refs><marker-ref marker-id="priority-2"/></marker-refs><children><topics type="attached"><topic><title>Step 1</title><children><topics type="attached"><topic><title>Expected</title></topic></topics></children></topic></topics></children></topic></topics></children></topic></topics></children></topic></sheet></xmap-content>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", xml)
    return buffer.getvalue()


def form(mode, output="after"):
    return {"conversion_mode": mode, "output_mode": output, "prefix_path": "迭代", "case_type": "业务功能用例", "case_status": "正常"}


@pytest.mark.parametrize("mode,output,filename,content,target_kind,columns", [
    ("excel_to_excel", "after", "cases.xlsx", source_file(), "table", 15),
    ("excel_to_excel", "after", "cases.csv", "用例名称,前置条件,用例步骤,预期结果\nA,B,C,D\n".encode(), "table", 15),
    ("xmind_to_excel", "before", "cases.xmind", sample_xmind(), "table", 7),
    ("xmind_to_final_excel", "after", "cases.xmind", sample_xmind(), "table", 15),
    ("excel_to_xmind", "after", "cases.xlsx", source_file(), "tree", 0),
])
def test_conversion_modes_and_history(context, mode, output, filename, content, target_kind, columns):
    client, headers, root = context
    url = root + "/conversion"
    args = {"data": form(mode, output), "files": {"file": (filename, content)}, "headers": headers}
    assert client.post(url + "/preview", **{**args, "headers": {}}).status_code == 403
    result = client.post(url + "/preview", **args)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["target_kind"] == target_kind
    assert len(payload["target_headers"]) == columns
    if target_kind == "tree":
        assert payload["target_tree"]["node_type"] == "root"
        assert payload["target_tree"]["children"][0]["node_type"] == "directory"
    response = client.post(url + "/export", **args)
    assert response.status_code == 200, response.text
    if target_kind == "tree":
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "content.xml" in archive.namelist()
    else:
        book = openpyxl.load_workbook(io.BytesIO(response.content))
        assert book.active.max_column == columns
    history = client.get(url + "/history").json()
    assert len(history["records"]) == 1
    overview = client.get(root + "/overview").json()
    assert overview["conversion_count"] == 1
    assert any(item["kind"] == "conversion" for item in overview["recent_activity"])
    record_id = history["records"][0]["id"]
    assert history["records"][0]["source"] == {"schema_version": "conversion-upload.v1",
                                                   "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    with SessionLocal() as db:
        uploaded_path = conversion_routes.source_path(db.get(ConversionRecord, record_id))
    assert uploaded_path.is_file()
    assert client.get(url + f"/history/{record_id}").json()["preview"]["row_count"] == 1
    assert client.get(url + f"/history/{record_id}/source").content == content
    assert client.get(url + f"/history/{record_id}/download").content == response.content
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 200
    assert not uploaded_path.exists()
    assert client.get(url + "/history").json()["records"] == []


def test_export_rechecks_deletion_intent_after_conversion(context, monkeypatch):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    convert = conversion_routes.service.export

    def delete_during_conversion(*args, **kwargs):
        result = convert(*args, **kwargs)
        with SessionLocal() as db:
            db.get(Project, project_id).deletion_pending_at = utcnow()
            db.commit()
        return result

    monkeypatch.setattr(conversion_routes.service, "export", delete_during_conversion)
    response = client.post(root + "/conversion/export", files={"file": ("cases.xlsx", source_file())},
                           data=form("excel_to_excel"), headers=headers)
    assert response.status_code == 409, response.text
    assert not (conversion_routes.DATA_DIR / str(project_id)).exists()
    with SessionLocal() as db:
        assert db.query(ConversionRecord).filter_by(project_id=project_id).count() == 0


def test_original_upload_is_project_scoped_and_detects_tampering(context):
    client, headers, root = context
    data = source_file()
    url = root + "/conversion"
    assert client.post(url + "/export", files={"file": ("source.xlsx", data)},
                       data=form("excel_to_excel"), headers=headers).status_code == 200
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    other = client.post("/api/v1/projects", json={"name": "Private conversion"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/conversion/history/{record_id}/source").status_code == 404
    with SessionLocal() as db:
        path = conversion_routes.source_path(db.get(ConversionRecord, record_id))
    path.write_bytes(b"tampered")
    assert client.get(url + f"/history/{record_id}/source").status_code == 409
    assert client.get(root + "/evidence/export").status_code == 409


def test_original_upload_streams_verified_bytes_when_file_changes_after_validation(context, monkeypatch):
    from fastapi.responses import StreamingResponse

    client, headers, root = context
    original = source_file()
    url = root + "/conversion"
    created = client.post(url + "/export", files={"file": ("source.xlsx", original)},
                          data=form("excel_to_excel"), headers=headers)
    assert created.status_code == 200, created.text
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        path = conversion_routes.source_path(db.get(ConversionRecord, record_id))
    altered = bytes([original[0] ^ 1]) + original[1:]

    def replace_after_validation(content, **kwargs):
        path.write_bytes(altered)
        return StreamingResponse(content, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(conversion_routes, "StreamingResponse", replace_after_validation)
        downloaded = client.get(url + f"/history/{record_id}/source")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original
    assert path.read_bytes() == altered
    assert client.get(url + f"/history/{record_id}/source").status_code == 409


def test_replaced_original_upload_link_blocks_source_export_and_deletion(context, tmp_path):
    client, headers, root = context
    url = root + "/conversion"
    uploaded = source_file()
    assert client.post(url + "/export", files={"file": ("source.xlsx", uploaded)},
                       data=form("excel_to_excel"), headers=headers).status_code == 200
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        record = db.get(ConversionRecord, record_id)
        original = conversion_routes.source_path(record)
        converted = conversion_routes.output_path(record)
    secret = tmp_path / "outside-secret.bin"
    secret.write_bytes(b"not a conversion source")
    original.unlink()
    try:
        original.symlink_to(secret)
    except OSError:
        pytest.skip("Target runner does not permit symlink creation")

    assert client.get(url + f"/history/{record_id}/source").status_code == 409
    assert client.get(root + "/evidence/export").status_code == 409
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 503
    assert secret.read_bytes() == b"not a conversion source"
    assert converted.is_file()
    with SessionLocal() as db:
        assert db.get(ConversionRecord, record_id) is not None

    original.unlink()
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 200
    assert not converted.exists()


def test_original_upload_junction_blocks_source_export_and_deletion(context, monkeypatch):
    client, headers, root = context
    url = root + "/conversion"
    assert client.post(url + "/export", files={"file": ("source.xlsx", source_file())},
                       data=form("excel_to_excel"), headers=headers).status_code == 200
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        original = conversion_routes.source_path(db.get(ConversionRecord, record_id))
    real_is_junction = Path.is_junction
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_junction", lambda self: self == original or real_is_junction(self))
        assert client.get(url + f"/history/{record_id}/source").status_code == 409
        assert client.get(root + "/evidence/export").status_code == 409
        assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 503
    with SessionLocal() as db:
        assert db.get(ConversionRecord, record_id) is not None
    assert original.is_file()
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 200


def test_partial_conversion_file_delete_can_be_retried(context, monkeypatch):
    client, headers, root = context
    url = root + "/conversion"
    assert client.post(url + "/export", files={"file": ("source.xlsx", source_file())},
                       data=form("excel_to_excel"), headers=headers).status_code == 200
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        record = db.get(ConversionRecord, record_id)
        original = conversion_routes.source_path(record)
        converted = conversion_routes.output_path(record)
    remove = conversion_routes.remove_private_file

    def fail_output_once(path, *, root):
        if path == converted:
            raise OSError("temporary storage error")
        return remove(path, root=root)

    with monkeypatch.context() as patch:
        patch.setattr(conversion_routes, "remove_private_file", fail_output_once)
        assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 503
    assert not original.exists() and converted.is_file()
    with SessionLocal() as db:
        assert db.get(ConversionRecord, record_id) is not None
    assert client.get(root + "/evidence/export").status_code == 409
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 200
    assert not converted.exists()
    with SessionLocal() as db:
        assert db.get(ConversionRecord, record_id) is None


def test_project_deletion_removes_original_upload_and_conversion_output(context):
    client, headers, root = context
    data = source_file()
    assert client.post(root + "/conversion/export", files={"file": ("source.xlsx", data)},
                       data=form("excel_to_excel"), headers=headers).status_code == 200
    record_id = client.get(root + "/conversion/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        record = db.get(ConversionRecord, record_id)
        original = conversion_routes.source_path(record)
        converted = conversion_routes.output_path(record)
    assert original.exists() and converted.exists()
    deleted = client.delete(root, headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert not original.exists() and not converted.exists()


def test_history_retention_does_not_prune_after_deletion_intent(context, monkeypatch):
    client, headers, root = context
    project_id = int(root.rsplit("/", 1)[-1])
    exported = client.post(root + "/conversion/export", files={"file": ("cases.xlsx", source_file())},
                           data=form("excel_to_excel"), headers=headers)
    assert exported.status_code == 200, exported.text
    original = conversion_routes.settings_for

    def mark_before_prune(db, current_project_id):
        settings = original(db, current_project_id)
        with SessionLocal() as independent:
            independent.get(Project, project_id).deletion_pending_at = utcnow()
            independent.commit()
        return settings

    monkeypatch.setattr(conversion_routes, "settings_for", mark_before_prune)
    history = client.get(root + "/conversion/history", headers=headers)
    assert history.status_code == 409, history.text
    with SessionLocal() as db:
        record = db.query(ConversionRecord).filter_by(project_id=project_id).one()
        assert conversion_routes.output_path(record).is_file()


def test_history_delete_rejects_junction_project_directory(context, monkeypatch):
    client, headers, root = context
    url = root + "/conversion"
    exported = client.post(url + "/export", files={"file": ("cases.xlsx", source_file())},
                           data=form("excel_to_excel"), headers=headers)
    assert exported.status_code == 200, exported.text
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    with SessionLocal() as db:
        artifact = conversion_routes.output_path(db.get(ConversionRecord, record_id))
    original = Path.is_junction
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_junction", lambda self: self == artifact.parent or original(self))
        response = client.delete(url + f"/history/{record_id}", headers=headers)
    assert response.status_code == 503
    assert artifact.is_file()
    with SessionLocal() as db:
        assert db.get(ConversionRecord, record_id)
    assert client.delete(url + f"/history/{record_id}", headers=headers).status_code == 200


def test_project_config_and_isolated_history(context):
    client, headers, root = context
    url = root + "/conversion"
    result = client.patch(url + "/config", json={"default_prefix_path": "另一个目录", "history_retention_days": 30}, headers=headers)
    assert result.status_code == 200
    assert result.json()["default_prefix_path"] == "另一个目录"
    assert client.get(url + "/config").json()["history_retention_days"] == 30
    assert client.patch(url + "/config", json={"history_retention_days": 4}, headers=headers).status_code == 422
    other = client.post("/api/v1/projects", json={"name": "Other"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/conversion/config").json()["default_prefix_path"] == ""
    response = client.post(url + "/export", files={"file": ("cases.xlsx", source_file())}, data=form("excel_to_excel"), headers=headers)
    assert response.status_code == 200
    assert client.get(f"/api/v1/projects/{other}/conversion/history").json()["records"] == []
    assert client.delete(url + "/history", headers=headers).json()["deleted_count"] == 1


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_history_download_rejects_replaced_symlink(context):
    client, headers, root = context
    url = root + "/conversion"
    response = client.post(url + "/export", files={"file": ("cases.xlsx", source_file())},
                           data=form("excel_to_excel"), headers=headers)
    assert response.status_code == 200
    record_id = client.get(url + "/history").json()["records"][0]["id"]
    path = conversion_routes.output_path(type("Record", (), {"project_id": int(root.split("/")[-1]), "id": record_id})())
    outside = path.parent / "outside-secret.txt"
    outside.write_bytes(b"not an export")
    path.unlink()
    path.symlink_to(outside)
    downloaded = client.get(url + f"/history/{record_id}/download")
    assert downloaded.status_code == 404


def test_first_reads_do_not_write_and_reader_permissions(context):
    client, headers, root = context
    url = root + "/conversion"
    responses = [client.get(url + "/config"), client.get(url + "/history")]
    assert [response.status_code for response in responses] == [200, 200]
    from api.db import ConversionSettings, SessionLocal
    with SessionLocal() as db:
        assert db.get(ConversionSettings, int(root.split("/")[-1])) is None
    assert client.delete(url + "/history").status_code == 403
    user = client.post("/api/v1/users", json={"username": "reader", "password": "reader-password-123", "role": "reader"}, headers=headers).json()
    project_id = int(root.split("/")[-1])
    client.post(f"{root}/members", json={"user_id": user["id"]}, headers=headers)
    other = client.post("/api/v1/projects", json={"name": "Private"}, headers=headers).json()["id"]
    client.post("/api/v1/auth/logout", headers=headers)
    token = client.get("/api/v1/auth/csrf").json()["token"]
    logged = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader-password-123"}, headers={"x-csrf-token": token}).json()
    reader_headers = {"x-csrf-token": logged["csrf"]}
    assert project_id != other
    assert client.get(url + "/history").status_code == 200
    assert client.get(f"/api/v1/projects/{other}/conversion/history").status_code == 403
    assert client.patch(url + "/config", json={"default_prefix_path": "other"}, headers=reader_headers).status_code == 403
    assert client.post(url + "/preview", data=form("excel_to_excel"), files={"file": ("cases.xlsx", source_file())}, headers=reader_headers).status_code == 403


def test_xmind_bridge_does_not_populate_internal_smoke():
    result = convert_preview(sample_xmind(), "a.xmind", "xmind_to_excel", "before", "", "", "")
    assert result["row_count"] == 1
    assert result["target_rows"][0]["用例等级"] == "2"


def test_excel_formula_text_is_escaped_and_xmind_expansion_limited():
    from api.conversion.converter import export_source_workbook
    from api.conversion.xmind_converter import parse_xmind
    book = openpyxl.load_workbook(io.BytesIO(export_source_workbook([{"用例名称": "=HYPERLINK(\"bad\")"}])))
    assert book.active.cell(2, 2).data_type == "s"
    assert book.active.cell(2, 2).value.startswith("'")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", b"<x/>")
        archive.writestr("large.bin", b"a" * (81 * 1024 * 1024))
    with pytest.raises(ValueError, match="安全限制"):
        parse_xmind(buffer.getvalue(), "large.xmind", " - ")
