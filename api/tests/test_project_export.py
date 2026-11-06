"""Full project evidence export is complete-or-fail, scoped and independently verifiable."""

import io
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api import ai_routes, conversion_routes, project_export_routes
from api.db import (AnalysisRun, Base, Case, CaseDraft, CaseSuggestion, ConversionRecord, ExecutionReport,
                    GovernanceChangeLog, GovernanceDirectory, GovernanceDuplicateGroup, GovernanceDuplicateMember,
                    GovernanceExportJob, GovernanceMetricSnapshot, GovernanceValidationRun, HarnessTask, ImportBatch,
                    KnowledgeEntry, KnowledgeRevision, Project, SessionLocal,
                    SuggestionJob, TaxonomyVersion)
from api.read_snapshot import consistent_read
from api.tests.test_smoke import context, create_suite, review_yes  # noqa: F401
from scripts.verify_project_export import verify


def test_export_uses_one_database_snapshot_when_cases_change(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'export-snapshot.db'}", connect_args={"timeout": 2})
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    with Session(engine) as db:
        project = Project(name="导出一致性")
        db.add(project)
        db.flush()
        batch = ImportBatch(project_id=project.id, filename="original.xls", headers=[], rows=[], warnings=[], status="done")
        db.add(batch)
        db.flush()
        case = Case(project_id=project.id, import_id=batch.id, original_id="TC-1", title="原始标题", original=[])
        db.add(case)
        db.commit()
        project_id, batch_id, case_id = project.id, batch.id, case.id

    with consistent_read(engine) as snapshot:
        assert snapshot.scalar(select(Case.title).where(Case.id == case_id)) == "原始标题"
        with Session(engine) as writer:
            writer.get(Case, case_id).title = "后来修改的标题"
            writer.add(Case(project_id=project_id, import_id=batch_id, original_id="TC-2",
                            title="新用例", original=[]))
            writer.commit()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            project_export_routes._export(project_id, snapshot, archive)
    output.seek(0)
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["counts"]["case"] == 1
        assert json.loads(archive.read(f"case/{case_id}.json"))["title"] == "原始标题"
    with Session(engine) as db:
        assert db.scalars(select(Case.title).where(Case.project_id == project_id).order_by(Case.id)).all() == [
            "后来修改的标题", "新用例"]
    with consistent_read(engine) as snapshot:
        with zipfile.ZipFile(io.BytesIO(), "w") as archive:
            with pytest.raises(HTTPException) as expired:
                project_export_routes._export(project_id, snapshot, archive, deadline=time.monotonic() - 1)
    assert expired.value.status_code == 503
    engine.dispose()


def test_export_rejects_same_size_in_place_change_to_legacy_file(context, monkeypatch, tmp_path):
    from api.db import SourceDocument

    client, headers, root, _ = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    response = client.post(f"{root}/documents", files={"file": ("legacy.md", b"# old material")},
                           headers=headers)
    assert response.status_code == 200, response.text
    document_id = response.json()["id"]
    with SessionLocal.begin() as db:
        document = db.get(SourceDocument, document_id)
        document.source_sha256 = None
        document.source_size = None
    path = ai_routes.DOCUMENT_DIR / root.rsplit("/", 1)[-1] / f"{document_id}.bin"
    real_open = project_export_routes.open_private_regular_file

    class ConcurrentWriter:
        def __init__(self, source):
            self.source = source
            self.modified = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def fileno(self):
            return self.source.fileno()

        def read(self, count):
            if not self.modified:
                self.modified = True
                old_mtime = os.stat(path).st_mtime_ns
                with path.open("r+b") as target:
                    target.write(b"# new material")
                os.utime(path, ns=(old_mtime + 2_000_000_000, old_mtime + 2_000_000_000))
            return self.source.read(count)

    def changing_open(candidate, *, root):
        stream, size = real_open(candidate, root=root)
        return (ConcurrentWriter(stream), size) if candidate == path else (stream, size)

    monkeypatch.setattr(project_export_routes, "open_private_regular_file", changing_open)
    assert client.get(f"{root}/evidence/export").status_code == 409


def test_export_endpoint_rechecks_scope_inside_consistent_read(context, monkeypatch):
    client, _, root, _ = context
    original_export = project_export_routes._export

    def inspect_snapshot(project_id, db, archive, **kwargs):
        assert db.connection().exec_driver_sql("PRAGMA read_uncommitted").scalar() == 0
        assert kwargs["deadline"] > time.monotonic()
        return original_export(project_id, db, archive, **kwargs)

    monkeypatch.setattr(project_export_routes, "_export", inspect_snapshot)
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text


def test_offline_verifier_rejects_false_inclusion_flags_for_normal_relationships(context, tmp_path):
    client, _, root, _ = context
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    original = tmp_path / "project-evidence.zip"
    original.write_bytes(response.content)
    assert verify(original)["reference_integrity"] == "complete"

    for change in ("omit_existing", "claim_missing"):
        altered = tmp_path / f"{change}.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(altered, "w") as target:
            references = json.loads(source.read("indexes/references.json"))
            link = next(edge for edge in references["links"] if edge["relation"] == "imported_as")
            if change == "omit_existing":
                link["to"]["included"] = False
            else:
                link["to"]["id"] = "999999999"
            index_bytes = json.dumps(references, ensure_ascii=False).encode("utf-8")
            manifest = json.loads(source.read("manifest.json"))
            entry = next(item for item in manifest["files"] if item["path"] == "indexes/references.json")
            entry.update(size=len(index_bytes), sha256=hashlib.sha256(index_bytes).hexdigest())
            for name in source.namelist():
                content = (index_bytes if name == "indexes/references.json" else
                           json.dumps(manifest, ensure_ascii=False).encode("utf-8") if name == "manifest.json"
                           else source.read(name))
                target.writestr(name, content)
        with pytest.raises(ValueError, match="project reference inclusion does not match archive"):
            verify(altered)


def test_v2_reference_index_rejects_missing_or_extra_provenance(context, tmp_path):
    client, _, root, _ = context
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    original = tmp_path / "complete-links.zip"
    original.write_bytes(response.content)
    assert verify(original)["integrity"] == "verified"
    with zipfile.ZipFile(original) as source:
        assert json.loads(source.read("indexes/references.json"))["schema_version"] == "project-references.v2"

    for change in ("missing", "duplicate", "legacy"):
        altered = tmp_path / f"{change}-links.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(altered, "w") as target:
            references = json.loads(source.read("indexes/references.json"))
            link = next(edge for edge in references["links"] if edge["relation"] == "imported_as")
            if change == "missing":
                references["links"].remove(link)
            elif change == "duplicate":
                references["links"].append(link.copy())
            else:
                references["schema_version"] = "project-references.v1"
            index_bytes = json.dumps(references, ensure_ascii=False).encode("utf-8")
            manifest = json.loads(source.read("manifest.json"))
            entry = next(item for item in manifest["files"] if item["path"] == "indexes/references.json")
            entry.update(size=len(index_bytes), sha256=hashlib.sha256(index_bytes).hexdigest())
            for name in source.namelist():
                content = (index_bytes if name == "indexes/references.json" else
                           json.dumps(manifest, ensure_ascii=False).encode("utf-8") if name == "manifest.json"
                           else source.read(name))
                target.writestr(name, content)
        if change == "legacy":
            assert verify(altered)["reference_integrity"] == "legacy-membership-only"
        else:
            with pytest.raises(ValueError, match="project reference index does not match archived provenance"):
                verify(altered)


def test_export_uses_safe_directory_ids_and_omits_private_export_paths(context, tmp_path, monkeypatch):
    client, _, root, _ = context
    project_id = int(root.rsplit("/", 1)[-1])
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    export_path = tmp_path / "governance-snapshots" / str(project_id) / f"export-{'a' * 32}.xlsx"
    export_path.parent.mkdir(parents=True)
    export_path.write_bytes(b"governance export")
    with SessionLocal() as db:
        db.add_all([
            GovernanceDirectory(project_id=project_id, path="业务", parent="", level=0, domain="业务"),
            GovernanceDirectory(project_id=project_id, path="业务/登录", parent="业务", level=1,
                                whitelisted=True, domain="业务", subdomain="登录"),
            GovernanceChangeLog(project_id=project_id, entity="directory", entity_id="业务/登录",
                                action="whitelist", before_json={}, after_json={"whitelisted": True},
                                batch_key="directories", operator_id=1),
            GovernanceExportJob(project_id=project_id, kind="cases", format="xlsx",
                                path=str(export_path), rows=3,
                                selfcheck={"readback": True}, created_by=1),
        ])
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    path = tmp_path / "directories.zip"
    path.write_bytes(response.content)
    counts = verify(path)["counts"]
    assert counts["governance_directory"] == 2 and counts["governance_export_job"] == 1
    directory_id = hashlib.sha256("业务/登录".encode()).hexdigest()
    parent_id = hashlib.sha256("业务".encode()).hexdigest()
    with zipfile.ZipFile(path) as archive:
        directory = json.loads(archive.read(f"governance_directory/{directory_id}.json"))
        assert directory["path"] == "业务/登录" and directory["whitelisted"] is True
        assert f"governance_directory/{parent_id}.json" in archive.namelist()
        job_name = next(name for name in archive.namelist() if name.startswith("governance_export_job/"))
        job = json.loads(archive.read(job_name))
        assert "path" not in job and job["selfcheck"] == {"readback": True}
        assert archive.read(f"governance-export-files/{job['id']}.bin") == b"governance export"
        assert job["file_sha256"] == hashlib.sha256(b"governance export").hexdigest()
        assert job["creation_integrity"] == "unverified"
        assert export_path.name not in archive.namelist()
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert {edge["relation"] for edge in links if edge["from"]["type"] == "governance_directory"} == {
            "contains_directory", "governed_as"}
    changed = tmp_path / "wrong-directory-hash.zip"
    record_path = f"governance_directory/{directory_id}.json"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(changed, "w") as target:
        directory["path"] = "业务/退出"
        broken = json.dumps(directory, ensure_ascii=False).encode()
        manifest = json.loads(source.read("manifest.json"))
        entry = next(item for item in manifest["files"] if item["path"] == record_path)
        entry.update(size=len(broken), sha256=hashlib.sha256(broken).hexdigest())
        for name in source.namelist():
            content = broken if name == record_path else (json.dumps(manifest, ensure_ascii=False).encode()
                                                           if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    with pytest.raises(ValueError, match="directory identity mismatch"):
        verify(changed)
    legacy = tmp_path / "prior-metadata-only.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(legacy, "w") as target:
        metadata = json.loads(source.read(job_name))
        metadata.pop("file_sha256")
        metadata.pop("file_size")
        metadata.pop("creation_integrity")
        old_record = json.dumps(metadata, ensure_ascii=False).encode()
        manifest = json.loads(source.read("manifest.json"))
        file_path = f"governance-export-files/{job['id']}.bin"
        manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != file_path]
        entry = next(item for item in manifest["files"] if item["path"] == job_name)
        entry.update(size=len(old_record), sha256=hashlib.sha256(old_record).hexdigest())
        for name in source.namelist():
            if name == file_path:
                continue
            content = old_record if name == job_name else (json.dumps(manifest, ensure_ascii=False).encode()
                                                          if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    assert verify(legacy)["integrity"] == "verified"
    orphaned = tmp_path / "orphaned-governance-export.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(orphaned, "w") as target:
        orphan_manifest = json.loads(source.read("manifest.json"))
        orphan_manifest["files"] = [entry for entry in orphan_manifest["files"] if entry["path"] != file_path]
        orphan_record = json.loads(source.read(job_name))
        orphan_record.pop("creation_integrity")
        orphan_bytes = json.dumps(orphan_record, ensure_ascii=False).encode()
        next(entry for entry in orphan_manifest["files"] if entry["path"] == job_name).update(
            size=len(orphan_bytes), sha256=hashlib.sha256(orphan_bytes).hexdigest())
        for name in source.namelist():
            if name != file_path:
                target.writestr(name, json.dumps(orphan_manifest, ensure_ascii=False).encode()
                                if name == "manifest.json" else orphan_bytes if name == job_name else source.read(name))
    with pytest.raises(ValueError, match="export file does not match"):
        verify(orphaned)
    export_path.unlink()
    assert client.get(f"{root}/evidence/export").status_code == 409


def test_new_governance_export_keeps_creation_digest_across_download_and_evidence(context, tmp_path, monkeypatch):
    client, headers, root, _ = context
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    created = client.post(f"{root}/governance/exports?format=xlsx", headers=headers)
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]
    selfcheck = created.json()["selfcheck"]
    with SessionLocal() as db:
        export_path = Path(db.get(GovernanceExportJob, job_id).path)
    original = export_path.read_bytes()
    assert selfcheck["created_sha256"] == hashlib.sha256(original).hexdigest()
    assert selfcheck["created_size"] == len(original)
    assert client.get(f"{root}/governance/exports/{job_id}/download").content == original

    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    original_zip = tmp_path / "original-evidence.zip"
    original_zip.write_bytes(response.content)
    assert verify(original_zip)["integrity"] == "verified"
    binary_name = f"governance-export-files/{job_id}.bin"
    with zipfile.ZipFile(original_zip) as archive:
        job = json.loads(archive.read(f"governance_export_job/{job_id}.json"))
        assert job["creation_integrity"] == "verified"
        assert job["selfcheck"]["created_sha256"] == selfcheck["created_sha256"]

    modified = bytes([original[0] ^ 1]) + original[1:]
    assert len(modified) == len(original)
    forged_zip = tmp_path / "forged-evidence.zip"
    with zipfile.ZipFile(original_zip) as source, zipfile.ZipFile(forged_zip, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        record_name = f"governance_export_job/{job_id}.json"
        job["file_sha256"] = hashlib.sha256(modified).hexdigest()
        job_bytes = json.dumps(job, ensure_ascii=False).encode()
        for entry in manifest["files"]:
            if entry["path"] == binary_name:
                entry["sha256"] = hashlib.sha256(modified).hexdigest()
            if entry["path"] == record_name:
                entry.update(size=len(job_bytes), sha256=hashlib.sha256(job_bytes).hexdigest())
        for name in source.namelist():
            data = (modified if name == binary_name else job_bytes if name == record_name
                    else json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json"
                    else source.read(name))
            target.writestr(name, data)
    with pytest.raises(ValueError, match="creation checksum mismatch"):
        verify(forged_zip)

    export_path.write_bytes(modified)
    assert client.get(f"{root}/governance/exports/{job_id}/download").status_code == 409
    assert client.get(f"{root}/evidence/export").status_code == 409


def test_export_checks_saved_governance_import_snapshot(context, tmp_path, monkeypatch):
    client, _, root, _ = context
    project_id = int(root.rsplit("/", 1)[-1])
    monkeypatch.setenv("AI_STUDIO_DATA_DIR", str(tmp_path))
    original = b"case_id,title\n1,login\n"
    digest = hashlib.sha256(original).hexdigest()
    source_path = tmp_path / "governance-snapshots" / str(project_id) / f"{digest}.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(original)
    with SessionLocal() as db:
        batch = ImportBatch(project_id=project_id, filename="incoming.csv", headers=["case_id", "title"],
                            rows=[["1", "login"]], warnings=[], kind="governance", status="preview",
                            sha256=digest, snapshot_path=str(source_path))
        db.add(batch)
        db.flush()
        batch_id = batch.id
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    archive_path = tmp_path / "governance-import.zip"
    archive_path.write_bytes(response.content)
    assert verify(archive_path)["integrity"] == "verified"
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read(f"governance-import-files/{batch_id}.bin") == original
        assert json.loads(archive.read(f"import/{batch_id}.json"))["source_saved"] is True
    legacy = tmp_path / "prior-import-metadata.zip"
    record_name = f"import/{batch_id}.json"
    binary_name = f"governance-import-files/{batch_id}.bin"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(legacy, "w") as target:
        record = json.loads(source.read(record_name))
        record.pop("source_saved")
        old_record = json.dumps(record, ensure_ascii=False).encode()
        manifest = json.loads(source.read("manifest.json"))
        manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != binary_name]
        entry = next(item for item in manifest["files"] if item["path"] == record_name)
        entry.update(size=len(old_record), sha256=hashlib.sha256(old_record).hexdigest())
        for name in source.namelist():
            if name == binary_name:
                continue
            content = old_record if name == record_name else (json.dumps(manifest, ensure_ascii=False).encode()
                                                             if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    assert verify(legacy)["integrity"] == "verified"
    source_path.write_bytes(b"tampered")
    assert client.get(f"{root}/evidence/export").status_code == 409
    source_path.unlink()
    outside = tmp_path / "outside.csv"
    outside.write_bytes(original)
    try:
        source_path.symlink_to(outside)
    except OSError:
        return
    assert client.get(f"{root}/evidence/export").status_code == 409


def test_export_preserves_scoped_duplicate_decisions_and_metrics(context, tmp_path):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    batch_id = cases[0]["import_id"]
    with SessionLocal() as db:
        group = GovernanceDuplicateGroup(project_id=project_id, batch_id=batch_id, gid="group-1",
                                         name_key="login", name="登录", size=2, cluster_count=1,
                                         status="reviewed", decided_by=1)
        db.add(group)
        db.flush()
        members = [GovernanceDuplicateMember(group_id=group.id, case_id=case["id"],
                                             cluster_fp="cluster-1", score=90 - index,
                                             decision="keep" if index == 0 else "discard",
                                             reason="人工复核") for index, case in enumerate(cases[:2])]
        db.add_all([*members,
                    GovernanceMetricSnapshot(project_id=project_id, batch_id=batch_id,
                                             key="duplicate_count", value=2),
                    GovernanceValidationRun(project_id=project_id, filename="incoming.csv",
                                            rows=[{"id": "candidate"}], result={"can_promote": False},
                                            status="blocked", created_by=1)])
        db.flush()
        group_id, member_ids = group.id, [member.id for member in members]
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    path = tmp_path / "duplicate-evidence.zip"
    path.write_bytes(response.content)
    counts = verify(path)["counts"]
    assert {kind: counts[kind] for kind in ("duplicate_group", "duplicate_member",
                                            "governance_validation", "governance_metric")} == {
        "duplicate_group": 1, "duplicate_member": 2, "governance_validation": 1, "governance_metric": 1,
    }
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read(f"duplicate_group/{group_id}.json"))["batch_id"] == batch_id
        assert {json.loads(archive.read(f"duplicate_member/{identifier}.json"))["decision"]
                for identifier in member_ids} == {"keep", "discard"}
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert sum(edge["relation"] == "reviewed_as_duplicate" for edge in links) == 2
        assert sum(edge["relation"] == "measured_as" for edge in links) == 1
    changed = tmp_path / "broken-duplicate.zip"
    member_path = f"duplicate_member/{member_ids[0]}.json"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(changed, "w") as target:
        payload = json.loads(source.read(member_path))
        payload["group_id"] = group_id + 999
        broken = json.dumps(payload, ensure_ascii=False).encode()
        manifest = json.loads(source.read("manifest.json"))
        entry = next(item for item in manifest["files"] if item["path"] == member_path)
        entry.update(size=len(broken), sha256=hashlib.sha256(broken).hexdigest())
        for name in source.namelist():
            content = broken if name == member_path else (json.dumps(manifest, ensure_ascii=False).encode()
                                                           if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    with pytest.raises(ValueError, match="duplicate member has incomplete archived provenance"):
        verify(changed)


def test_export_rejects_duplicate_member_pointing_to_other_project(context):
    client, headers, root, cases = context
    other_id = client.post("/api/v1/projects", json={"name": "Foreign group"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        batch = ImportBatch(project_id=other_id, filename="other.xlsx", headers=[], rows=[], warnings=[])
        db.add(batch)
        db.flush()
        group = GovernanceDuplicateGroup(project_id=other_id, batch_id=batch.id,
                                         gid="wrong", name_key="wrong", name="wrong", size=1)
        db.add(group)
        db.flush()
        db.add(GovernanceDuplicateMember(group_id=group.id, case_id=cases[0]["id"], cluster_fp="other"))
        db.commit()
    assert client.get(f"{root}/evidence/export").status_code == 200
    assert client.get(f"/api/v1/projects/{other_id}/evidence/export").status_code == 409


def test_export_preserves_governance_case_history_and_nulls(context, tmp_path):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    case_id = cases[0]["id"]
    with SessionLocal() as db:
        case = db.get(Case, case_id)
        case.platform_id = "platform-001"
        case.governance_status = "active"
        db.add_all([
            GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case_id),
                                action="edit", before_json={"smoke": None}, after_json={"smoke": "是"},
                                batch_key="review-batch", operator_id=1),
            GovernanceChangeLog(project_id=project_id, entity="directory", entity_id="业务/登录",
                                action="whitelist", before_json={}, after_json={"whitelisted": True},
                                batch_key="directory-batch", operator_id=1),
        ])
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    archive_path = tmp_path / "governance.zip"
    archive_path.write_bytes(response.content)
    assert verify(archive_path)["counts"]["governance_change"] == 2
    with zipfile.ZipFile(archive_path) as archive:
        case = json.loads(archive.read(f"case/{case_id}.json"))
        assert case["platform_id"] == "platform-001"
        assert case["tag_requirement"] is None
        assert case["governance_status"] == "active"
        changes = [json.loads(archive.read(name)) for name in archive.namelist()
                   if name.startswith("governance_change/")]
        assert {item["action"] for item in changes} == {"edit", "whitelist"}
        assert next(item for item in changes if item["action"] == "edit")["before"]["smoke"] is None
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert sum(edge["relation"] == "governed_as" and edge["from"]["id"] == str(case_id)
                   for edge in links) == 1


def test_export_rejects_governance_log_for_foreign_project_case(context, tmp_path):
    client, headers, root, cases = context
    other_id = client.post("/api/v1/projects", json={"name": "Wrong governance owner"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(GovernanceChangeLog(project_id=other_id, entity="case", entity_id=str(cases[0]["id"]),
                                   action="edit", before_json={}, after_json={},
                                   batch_key="wrong-owner", operator_id=1))
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200
    # A v1 bundle created before these optional evidence kinds existed has
    # no zero-count entries for them; the offline verifier must still read it.
    prior = tmp_path / "prior-counts.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as source, zipfile.ZipFile(prior, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        for kind in ("taxonomy", "suggestion_job", "suggestion", "governance_change",
                     "duplicate_group", "duplicate_member", "governance_validation", "governance_metric",
                     "governance_directory", "governance_export_job"):
            manifest["counts"].pop(kind)
        for name in source.namelist():
            content = json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json" else source.read(name)
            target.writestr(name, content)
    assert verify(prior)["integrity"] == "verified"
    assert client.get(f"/api/v1/projects/{other_id}/evidence/export").status_code == 409


def test_evidence_verifier_rejects_duplicate_manifest_file_entry(context, tmp_path):
    client, _, root, _ = context
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    tampered = tmp_path / "duplicate-entry.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as source, zipfile.ZipFile(tampered, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        item = next(entry for entry in manifest["files"] if entry["path"].startswith("case/"))
        manifest["files"].append(dict(item))
        manifest["counts"]["case"] += 1
        for name in source.namelist():
            content = json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json" else source.read(name)
            target.writestr(name, content)
    with pytest.raises(ValueError, match="manifest does not cover"):
        verify(tampered)


def test_export_preserves_scoped_taxonomy_and_suggestion_decisions(context, tmp_path):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Other suggestions"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        taxonomy = TaxonomyVersion(project_id=project_id, number=1, status="published",
                                   definitions={"domains": ["登录"]}, created_by=1)
        db.add(taxonomy)
        db.flush()
        db.add_all([
            SuggestionJob(id="audit-job", project_id=project_id, created_by=1,
                          taxonomy_version_id=taxonomy.id, provider="local", status="completed",
                          case_ids=[cases[0]["id"]], total=1, completed=1, errors=[]),
            SuggestionJob(id="foreign-job", project_id=other_id, created_by=1,
                          provider="local", status="completed", case_ids=[], total=0, errors=[]),
        ])
        db.flush()
        suggestion = CaseSuggestion(project_id=project_id, case_id=cases[0]["id"], case_version=1,
                                    taxonomy_version_id=taxonomy.id, job_id="audit-job", source="ai",
                                    status="accepted", categories={"domain": "登录"}, level="1", smoke=None,
                                    rationale="Explicit suggestion", questions=[], created_by=1,
                                    decided_by=1, decision_reason="Checked by reviewer")
        db.add(suggestion)
        db.flush()
        suggestion_id, taxonomy_id = suggestion.id, taxonomy.id
        db.commit()
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    archive_path = tmp_path / "suggestions.zip"
    archive_path.write_bytes(response.content)
    counts = verify(archive_path)["counts"]
    assert {key: counts[key] for key in ("taxonomy", "suggestion_job", "suggestion")} == {
        "taxonomy": 1, "suggestion_job": 1, "suggestion": 1,
    }
    with zipfile.ZipFile(archive_path) as archive:
        saved = json.loads(archive.read(f"suggestion/{suggestion_id}.json"))
        assert saved["categories"] == {"domain": "登录"}
        assert saved["decision_reason"] == "Checked by reviewer"
        assert saved["smoke"] is None
        assert json.loads(archive.read(f"taxonomy/{taxonomy_id}.json"))["definitions"] == {"domains": ["登录"]}
        assert "suggestion_job/foreign-job.json" not in archive.namelist()
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert {(edge["from"]["type"], edge["to"]["type"], edge["relation"])
                for edge in links if edge["to"]["type"] == "suggestion"
                and edge["to"]["id"] == str(suggestion_id)} == {
            ("case", "suggestion", "suggested_for"),
            ("taxonomy", "suggestion", "suggested_with"),
            ("suggestion_job", "suggestion", "produced_suggestion"),
        }
    changed = tmp_path / "broken-suggestion.zip"
    suggestion_path = f"suggestion/{suggestion_id}.json"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(changed, "w") as target:
        payload = json.loads(source.read(suggestion_path))
        payload["taxonomy_version_id"] = 999
        broken = json.dumps(payload, ensure_ascii=False).encode()
        manifest = json.loads(source.read("manifest.json"))
        entry = next(item for item in manifest["files"] if item["path"] == suggestion_path)
        entry.update(size=len(broken), sha256=hashlib.sha256(broken).hexdigest())
        for name in source.namelist():
            content = broken if name == suggestion_path else (json.dumps(manifest, ensure_ascii=False).encode()
                                                               if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    with pytest.raises(ValueError, match="incomplete archived provenance"):
        verify(changed)
    missing_count = tmp_path / "missing-count.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(missing_count, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["counts"].pop("suggestion")
        for name in source.namelist():
            content = json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json" else source.read(name)
            target.writestr(name, content)
    with pytest.raises(ValueError, match="counts mismatch"):
        verify(missing_count)


def test_export_rejects_suggestion_with_foreign_project_case(context):
    client, headers, root, cases = context
    other_id = client.post("/api/v1/projects", json={"name": "Foreign suggestion"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(CaseSuggestion(project_id=other_id, case_id=cases[0]["id"], case_version=1,
                              source="rule", status="pending", categories={},
                              rationale="Not scoped", created_by=1))
        db.commit()
    assert client.get(f"{root}/evidence/export").status_code == 200
    assert client.get(f"/api/v1/projects/{other_id}/evidence/export").status_code == 409


def test_project_export_verifies_original_harness_markdown_as_a_separate_artifact(context, tmp_path, monkeypatch):
    client, headers, root, _ = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    project_id = int(root.rsplit("/", 1)[-1])
    uploaded = client.post(f"{root}/documents", files={"file": ("login.md", b"# Login")}, headers=headers)
    assert uploaded.status_code == 200
    document_id = uploaded.json()["id"]
    report = "\n# Login analysis\n\nModel-authored source report\n"
    original = {
        "schema": "requirement-analysis.v1", "task_id": "report-task", "session_id": "report-session",
        "result_id": "report-result", "source_document_ids": [document_id],
        "generated_at": "2026-10-25T00:00:00Z", "warnings": [], "report_markdown": report,
        "data": {"requirements": [], "ambiguities": [], "quality_warnings": []},
    }
    with SessionLocal() as db:
        db.add(AnalysisRun(id="report-run", project_id=project_id, user_id=1, provider="harness",
                           status="ready", source_ids=[document_id], result={"harness_result": original}))
        db.add(HarnessTask(id="report-task", project_id=project_id, user_id=1,
                           task_type="requirement-analysis", skill_id="requirement-analysis",
                           result_schema="requirement-analysis.v1", status="completed",
                           source_document_ids=[document_id], analysis_run_id="report-run",
                           result=original, idempotency_key="evidence-report"))
        historical = {**original, "task_id": "historical-task", "result_id": "historical-result"}
        historical.pop("report_markdown")
        db.add(AnalysisRun(id="historical-run", project_id=project_id, user_id=1, provider="harness",
                           status="ready", source_ids=[document_id], result={"harness_result": historical}))
        db.add(HarnessTask(id="historical-task", project_id=project_id, user_id=1,
                           task_type="requirement-analysis", skill_id="requirement-analysis",
                           result_schema="requirement-analysis.v1", status="completed",
                           source_document_ids=[document_id], analysis_run_id="historical-run",
                           result=historical, idempotency_key="evidence-historical"))
        db.commit()

    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    path = tmp_path / "original-report-evidence.zip"
    path.write_bytes(response.content)
    assert verify(path)["counts"]["analysis_report"] == 1
    with zipfile.ZipFile(path) as archive:
        report_path = "analysis_report/report-task.md"
        assert archive.read(report_path) == report.encode("utf-8")
        assert "analysis_report/historical-task.md" not in archive.namelist()
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert {(edge["from"]["type"], edge["to"]["type"], edge["relation"])
                for edge in links if "analysis_report" in (edge["from"]["type"], edge["to"]["type"])} == {
                    ("harness_task", "analysis_report", "contains_report"),
                    ("analysis_report", "analysis", "reviewed_as"),
                }

    forged = tmp_path / "forged-report-evidence.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(forged, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        altered = b"# Forged analysis\n"
        entry = next(item for item in manifest["files"] if item["path"] == report_path)
        entry.update(size=len(altered), sha256=hashlib.sha256(altered).hexdigest())
        for name in source.namelist():
            target.writestr(name, altered if name == report_path else
                            json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json"
                            else source.read(name))
    with pytest.raises(ValueError, match="does not match its task"):
        verify(forged)

    broken_links = tmp_path / "unlinked-report-evidence.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(broken_links, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        links_path = "indexes/references.json"
        references = json.loads(source.read(links_path))
        references["links"] = [edge for edge in references["links"] if edge["relation"] != "contains_report"]
        altered = json.dumps(references, ensure_ascii=False).encode()
        entry = next(item for item in manifest["files"] if item["path"] == links_path)
        entry.update(size=len(altered), sha256=hashlib.sha256(altered).hexdigest())
        for name in source.namelist():
            target.writestr(name, altered if name == links_path else
                            json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json"
                            else source.read(name))
    with pytest.raises(ValueError, match="report references do not match"):
        verify(broken_links)

    with SessionLocal() as db:
        run = db.get(AnalysisRun, "report-run")
        run.result = {"harness_result": {**original, "report_markdown": "# Changed"}}
        db.commit()
    assert client.get(f"{root}/evidence/export").status_code == 409


def test_v2_evidence_manifest_declares_report_scope_even_without_reports(context, tmp_path):
    client, _, root, _ = context
    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200
    path = tmp_path / "empty-report-scope.zip"
    path.write_bytes(response.content)
    with zipfile.ZipFile(path) as source:
        manifest = json.loads(source.read("manifest.json"))
        assert manifest["schema_version"] == "project-evidence.v2"
        assert manifest["counts"]["analysis_report"] == 0
        archived = {name: source.read(name) for name in source.namelist() if name != "manifest.json"}
    manifest["counts"].pop("analysis_report")
    downgraded = tmp_path / "legacy-evidence.zip"
    with zipfile.ZipFile(downgraded, "w") as target:
        for name, content in archived.items():
            target.writestr(name, content)
        target.writestr("manifest.json", json.dumps({**manifest, "schema_version": "project-evidence.v1"}))
    assert verify(downgraded)["integrity"] == "verified"
    tampered = tmp_path / "v2-missing-report-scope.zip"
    with zipfile.ZipFile(tampered, "w") as target:
        for name, content in archived.items():
            target.writestr(name, content)
        target.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="report delivery contract"):
        verify(tampered)


def test_project_export_includes_results_source_files_history_and_checksum(context, tmp_path, monkeypatch):
    client, headers, root, cases = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    project_id = int(root.rsplit("/", 1)[-1])
    doc = client.post(f"{root}/documents", files={"file": ("rule.md", b"# Subject")}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(AnalysisRun(id="export-analysis", project_id=project_id, user_id=1,
                           source_ids=[doc], status="ready", result={"requirements": [{"id": "R-001"}]}, version=2))
        db.add(HarnessTask(id="export-task", project_id=project_id, user_id=1, task_type="skill-execution",
                           skill_id="api-parsing", result_schema="skill-execution.v1", status="completed",
                           source_document_ids=[doc], analysis_run_id="export-analysis",
                           result={"summary": "Harness原始结果"}, idempotency_key="export"))
        db.add(CaseDraft(id="export-draft", project_id=project_id, analysis_id="export-analysis",
                         status="draft", payload={"title": "尚未发布", "requirement_ids": ["R-001"]}))
        db.commit()
    knowledge = client.post(f"{root}/knowledge", json={"source_type": "analysis",
                            "source_id": "export-analysis", "title": "结论", "body": "初版"}, headers=headers).json()
    client.patch(f"{root}/knowledge/{knowledge['id']}", json={"version": 1, "title": "结论更新",
                 "body": "二版", "status": "active"}, headers=headers)
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": reviewed["id"], "chain_name": "登录",
        "estimated_seconds": 20}, headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    run_case_id = run["items"][0]["id"]
    client.put(f"{root}/execution-runs/{run['id']}/cases/{run_case_id}", json={
        "version": 1, "result": "failed", "actual_seconds": 5, "note": "失败"}, headers=headers)
    client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers)
    defect = client.post(f"{root}/execution-defects", json={
        "run_id": run["id"], "run_case_id": run_case_id, "title": "故障"}, headers=headers).json()
    record_id = "conversion-export"
    with SessionLocal() as db:
        db.add(ConversionRecord(id=record_id, project_id=project_id, user_id=1,
                                source_filename="cases.xlsx", output_filename="result.xlsx",
                                conversion_mode="excel_to_excel", output_mode="after",
                                media_type="application/octet-stream", app_version="0.1",
                                row_count=1, file_size=9, preview={"rows": 1}))
        db.commit()
    output_file = conversion_routes.DATA_DIR / str(project_id) / f"{record_id}.bin"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(b"converted")
    uploaded = "用例名称,前置条件,用例步骤,预期结果\n新用例,已有账号,输入信息,进入首页\n".encode()
    conversion = client.post(f"{root}/conversion/export", files={"file": ("uploaded.csv", uploaded)},
                             data={"conversion_mode": "excel_to_excel", "output_mode": "after",
                                   "prefix_path": "迭代", "case_type": "业务功能用例", "case_status": "正常"}, headers=headers)
    assert conversion.status_code == 200, conversion.text
    actual_id = next(item["id"] for item in client.get(f"{root}/conversion/history").json()["records"]
                     if item["source_filename"] == "uploaded.csv")
    linked = client.get(f"{root}/artifacts/conversion/{actual_id}/relations").json()
    assert linked["edges"] == [{"from": {"type": "conversion_input", "id": actual_id},
                                "to": {"type": "conversion", "id": actual_id}, "relation": "converted_into"}]
    assert client.get(f"{root}/artifacts/conversion_input/{actual_id}/relations").json()["center"]["sha256"] == hashlib.sha256(uploaded).hexdigest()
    assert client.get(f"{root}/artifacts/conversion_input/{record_id}/relations").status_code == 404

    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    archive_path = tmp_path / "evidence.zip"
    archive_path.write_bytes(response.content)
    result = verify(archive_path)
    assert result["project_id"] == project_id
    assert result["counts"]["case"] == 3
    assert result["counts"]["report_case"] == 1
    command = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/verify_project_export.py"),
                              str(archive_path)], capture_output=True, text=True, check=True)
    assert json.loads(command.stdout)["integrity"] == "verified"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project"]["name"] == "冒烟验收"
        assert manifest["project"]["template_origin"] is None
        assert f"document-files/{doc}.bin" in names
        assert archive.read(f"document-files/{doc}.bin") == b"# Subject"
        assert archive.read(f"conversion-files/{record_id}.bin") == b"converted"
        legacy_conversion = json.loads(archive.read(f"conversion/{record_id}.json"))
        assert legacy_conversion["creation_integrity"] == "unverified"
        assert legacy_conversion["output_sha256"] is None
        assert archive.read(f"conversion-input-files/{actual_id}.bin") == uploaded
        new_conversion = json.loads(archive.read(f"conversion/{actual_id}.json"))
        assert new_conversion["creation_integrity"] == "verified"
        assert new_conversion["output_sha256"] == hashlib.sha256(archive.read(f"conversion-files/{actual_id}.bin")).hexdigest()
        input_record = json.loads(archive.read(f"conversion_input/{actual_id}.json"))
        assert input_record["sha256"] == hashlib.sha256(uploaded).hexdigest()
        assert f"conversion_input/{record_id}.json" not in names
        assert json.loads(archive.read("harness_task/export-task.json"))["result"] == {"summary": "Harness原始结果"}
        assert len(json.loads(archive.read(f"knowledge/{knowledge['id']}.json"))["revisions"]) == 2
        assert f"execution_report/{run['id']}.json" in names and f"defect/{defect['id']}.json" in names
        frozen_path = f"report_case/{run['id']}-{run_case_id}.json"
        frozen = json.loads(archive.read(frozen_path))
        assert frozen["id"] == f"{run['id']}:{run_case_id}"
        assert frozen["frozen"]["original_id"] == reviewed["original_id"]
        assert frozen["report_sha256"] == json.loads(archive.read(f"execution_report/{run['id']}.json"))["integrity"]["payload_sha256"]
        assert f"execution_run/{run['id']}.json" in names and f"run_case/{run_case_id}.json" in names
        assert json.loads(archive.read(f"run_case/{run_case_id}.json"))["events"]
        assert json.loads(archive.read(f"case/{reviewed['id']}.json"))["reviews"]
        assert json.loads(archive.read(f"defect/{defect['id']}.json"))["events"]
        assert "models.json" not in names and "sessions.json" not in names
        refs = json.loads(archive.read("indexes/references.json"))["links"]
        assert any(ref["relation"] == "records_defect" and ref["to"]["id"] == str(defect["id"])
                   for ref in refs)
        assert {ref["relation"] for ref in refs if ref["to"]["type"] == "report_case"} == {
            "contains_case", "frozen_as", "snapshot_of"}
        assert any(ref["relation"] == "found_defect" and ref["from"]["id"] == frozen["id"]
                   and ref["to"]["id"] == str(defect["id"]) for ref in refs)
        assert any(ref["relation"] == "converted_into" and ref["from"]["id"] == actual_id
                   for ref in refs)
    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(corrupted, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            target.writestr(item.filename, b"tampered!" if item.filename == "conversion-files/conversion-export.bin" else data)
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify(corrupted)
    output_file.unlink()


def test_deleted_case_retains_frozen_export_and_rejects_forged_frozen_copy(context, tmp_path):
    client, headers, root, cases = context
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": reviewed["id"], "chain_name": "登录", "estimated_seconds": 20},
        headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    item_id = run["items"][0]["id"]
    assert client.put(f"{root}/execution-runs/{run['id']}/cases/{item_id}", json={
        "version": 1, "result": "failed", "actual_seconds": 10, "note": "失败"}, headers=headers).status_code == 200
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    assert client.delete(f"{root}/cases/{reviewed['id']}", headers=headers).status_code == 200

    response = client.get(f"{root}/evidence/export")
    assert response.status_code == 200, response.text
    exported = tmp_path / "deleted-case-evidence.zip"
    exported.write_bytes(response.content)
    assert verify(exported)["counts"]["report_case"] == 1
    frozen_path = f"report_case/{run['id']}-{item_id}.json"
    with zipfile.ZipFile(exported) as archive:
        frozen = json.loads(archive.read(frozen_path))
        assert frozen["frozen"]["original_id"] == reviewed["original_id"]
        assert frozen["frozen"]["case_version"] == reviewed["version"]
        links = json.loads(archive.read("indexes/references.json"))["links"]
        assert any(link["relation"] == "snapshot_of" and link["from"]["id"] == str(reviewed["id"])
                   and link["from"]["included"] is False and link["to"]["included"] is True for link in links)

    forged = tmp_path / "forged-frozen-case.zip"
    with zipfile.ZipFile(exported) as source, zipfile.ZipFile(forged, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        frozen["frozen"]["title"] = "伪造标题"
        changed = json.dumps(frozen, ensure_ascii=False).encode()
        entry = next(item for item in manifest["files"] if item["path"] == frozen_path)
        entry.update(size=len(changed), sha256=hashlib.sha256(changed).hexdigest())
        for name in source.namelist():
            content = (changed if name == frozen_path else json.dumps(manifest, ensure_ascii=False).encode()
                       if name == "manifest.json" else source.read(name))
            target.writestr(name, content)
    with pytest.raises(ValueError, match="frozen report case does not match"):
        verify(forged)

    incomplete = tmp_path / "missing-frozen-case.zip"
    with zipfile.ZipFile(exported) as source, zipfile.ZipFile(incomplete, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["files"] = [item for item in manifest["files"] if item["path"] != frozen_path]
        manifest["counts"]["report_case"] = 0
        for name in source.namelist():
            if name != frozen_path:
                target.writestr(name, json.dumps(manifest, ensure_ascii=False).encode()
                                if name == "manifest.json" else source.read(name))
    with pytest.raises(ValueError, match="project reference inclusion does not match archive"):
        verify(incomplete)

    broken_links = tmp_path / "missing-frozen-relation.zip"
    with zipfile.ZipFile(exported) as source, zipfile.ZipFile(broken_links, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        index_path = "indexes/references.json"
        references = json.loads(source.read(index_path))
        references["links"] = [edge for edge in references["links"]
                               if not (edge["relation"] == "snapshot_of"
                                       and edge["to"]["id"] == f"{run['id']}:{item_id}")]
        changed = json.dumps(references, ensure_ascii=False).encode()
        entry = next(item for item in manifest["files"] if item["path"] == index_path)
        entry.update(size=len(changed), sha256=hashlib.sha256(changed).hexdigest())
        for name in source.namelist():
            target.writestr(name, changed if name == index_path else
                            json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json"
                            else source.read(name))
    with pytest.raises(ValueError, match="frozen report case references do not match"):
        verify(broken_links)


def test_project_export_rejects_missing_files_and_bad_report(context, monkeypatch, tmp_path):
    client, headers, root, cases = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    doc = client.post(f"{root}/documents", files={"file": ("rule.md", b"# Rule")}, headers=headers).json()["id"]
    path = ai_routes.DOCUMENT_DIR / root.rsplit("/", 1)[-1] / f"{doc}.bin"
    monkeypatch.setenv("TEST_ATELIER_EXPORT_MAX_BYTES", "32")
    assert client.get(f"{root}/evidence/export").status_code == 413
    monkeypatch.delenv("TEST_ATELIER_EXPORT_MAX_BYTES")
    path.unlink()
    assert client.get(f"{root}/evidence/export").status_code == 409
    secret = path.parent / "outside.bin"
    secret.write_bytes(b"DO NOT INCLUDE")
    try:
        path.symlink_to(secret)
    except OSError:
        pytest.skip("Target runner does not permit symlink creation")
    assert client.get(f"{root}/evidence/export").status_code == 409
    path.unlink()


def test_document_upload_identity_survives_download_export_and_offline_verification(context, monkeypatch, tmp_path):
    from api.db import SourceDocument

    client, headers, root, _ = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    project_id = int(root.rsplit("/", 1)[-1])
    original = b"# Original requirement"
    response = client.post(f"{root}/documents", files={"file": ("rule.md", original)}, headers=headers)
    assert response.status_code == 200, response.text
    document_id = response.json()["id"]
    path = ai_routes.DOCUMENT_DIR / str(project_id) / f"{document_id}.bin"
    with SessionLocal() as db:
        row = db.get(SourceDocument, document_id)
        assert (row.source_sha256, row.source_size) == (hashlib.sha256(original).hexdigest(), len(original))
    download = client.get(f"{root}/documents/{document_id}/download")
    assert (download.status_code, download.content, download.headers["x-source-integrity"]) == (
        200, original, "verified")
    exported = client.get(f"{root}/evidence/export")
    assert exported.status_code == 200, exported.text
    archive_path = tmp_path / "source-evidence.zip"
    archive_path.write_bytes(exported.content)
    assert verify(archive_path)["integrity"] == "verified"
    record_name = f"document/{document_id}.json"
    file_name = f"document-files/{document_id}.bin"
    with zipfile.ZipFile(archive_path) as archive:
        record = json.loads(archive.read(record_name))
        assert (record["creation_integrity"], record["source_sha256"], record["source_size"]) == (
            "verified", hashlib.sha256(original).hexdigest(), len(original))

    # Recompute the ZIP manifest to prove the independent upload identity catches a forged file.
    altered = b"!" + original[1:]
    forged_path = tmp_path / "forged-source-evidence.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(forged_path, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        next(item for item in manifest["files"] if item["path"] == file_name)["sha256"] = hashlib.sha256(altered).hexdigest()
        for name in source.namelist():
            target.writestr(name, altered if name == file_name else
                            json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json" else source.read(name))
    with pytest.raises(ValueError, match="document original file does not match upload identity"):
        verify(forged_path)

    path.write_bytes(altered)
    assert client.get(f"{root}/documents/{document_id}/download").status_code == 409
    assert client.get(f"{root}/evidence/export").status_code == 409
    path.write_bytes(original)
    with SessionLocal() as db:
        row = db.get(SourceDocument, document_id)
        row.source_sha256 = None
        row.source_size = None
        db.commit()
    assert client.get(f"{root}/documents/{document_id}/download").headers["x-source-integrity"] == "unverified"
    legacy_export = client.get(f"{root}/evidence/export")
    assert legacy_export.status_code == 200, legacy_export.text
    legacy_path = tmp_path / "legacy-source-evidence.zip"
    legacy_path.write_bytes(legacy_export.content)
    assert verify(legacy_path)["integrity"] == "verified"
    with zipfile.ZipFile(legacy_path) as archive:
        assert json.loads(archive.read(record_name))["creation_integrity"] == "unverified"


def test_conversion_output_creation_identity_detects_same_size_replacement(context, monkeypatch, tmp_path):
    client, headers, root, _ = context
    monkeypatch.setattr(conversion_routes, "DATA_DIR", tmp_path / "conversions")
    uploaded = "用例名称,前置条件,用例步骤,预期结果\n新用例,已有账号,输入信息,进入首页\n".encode()
    response = client.post(f"{root}/conversion/export", files={"file": ("uploaded.csv", uploaded)},
                           data={"conversion_mode": "excel_to_excel", "output_mode": "after",
                                 "prefix_path": "迭代", "case_type": "业务功能用例", "case_status": "正常"},
                           headers=headers)
    assert response.status_code == 200, response.text
    record = client.get(f"{root}/conversion/history").json()["records"][0]
    record_id = record["id"]
    with SessionLocal() as db:
        row = db.get(ConversionRecord, record_id)
        path = conversion_routes.output_path(row)
        assert row.output_sha256 == hashlib.sha256(response.content).hexdigest()
    download = client.get(f"{root}/conversion/history/{record_id}/download")
    assert (download.status_code, download.content, download.headers["x-output-integrity"]) == (
        200, response.content, "verified")
    archive_path = tmp_path / "original.zip"
    evidence = client.get(f"{root}/evidence/export")
    assert evidence.status_code == 200, evidence.text
    archive_path.write_bytes(evidence.content)
    assert verify(archive_path)["integrity"] == "verified"

    binary_name = f"conversion-files/{record_id}.bin"
    changed = bytes([response.content[0] ^ 1]) + response.content[1:]
    forged_path = tmp_path / "forged.zip"
    with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(forged_path, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        next(item for item in manifest["files"] if item["path"] == binary_name)["sha256"] = hashlib.sha256(changed).hexdigest()
        for name in source.namelist():
            target.writestr(name, changed if name == binary_name else
                            json.dumps(manifest, ensure_ascii=False).encode() if name == "manifest.json" else source.read(name))
    with pytest.raises(ValueError, match="conversion output does not match creation identity"):
        verify(forged_path)

    path.write_bytes(changed)
    assert client.get(f"{root}/conversion/history/{record_id}/download").status_code == 409
    assert client.get(f"{root}/evidence/export").status_code == 409
    path.write_bytes(response.content)
    with SessionLocal() as db:
        db.get(ConversionRecord, record_id).output_sha256 = None
        db.commit()
    old_download = client.get(f"{root}/conversion/history/{record_id}/download")
    assert (old_download.status_code, old_download.headers["x-output-integrity"]) == (200, "unverified")
    old_export = client.get(f"{root}/evidence/export")
    assert old_export.status_code == 200, old_export.text
    old_path = tmp_path / "legacy.zip"
    old_path.write_bytes(old_export.content)
    assert verify(old_path)["integrity"] == "verified"
    with zipfile.ZipFile(old_path) as archive:
        assert json.loads(archive.read(f"conversion/{record_id}.json"))["creation_integrity"] == "unverified"


def test_export_project_isolation_for_reader(context, tmp_path, monkeypatch):
    client, headers, root, cases = context
    monkeypatch.setattr(ai_routes, "DOCUMENT_DIR", tmp_path / "documents")
    other_id = client.post("/api/v1/projects", json={"name": "秘密项目"}, headers=headers).json()["id"]
    client.post(f"/api/v1/projects/{other_id}/documents", files={"file": ("private.md", b"PRIVATE-DATA")},
                headers=headers)
    reader = client.post("/api/v1/users", json={"username": "export-reader", "password": "reader-password-123",
                         "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    from api.tests.test_projects import login
    login(client, "export-reader", "reader-password-123")
    assert client.get(f"/api/v1/projects/{other_id}/evidence/export").status_code == 403
    own = client.get(f"{root}/evidence/export")
    assert own.status_code == 200
    assert b"PRIVATE-DATA" not in own.content


def test_export_rejects_when_concurrency_cap_is_reached(context):
    client, headers, root, cases = context
    assert project_export_routes._active_exports.acquire(blocking=False)
    assert project_export_routes._active_exports.acquire(blocking=False)
    try:
        response = client.get(f"{root}/evidence/export")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
    finally:
        project_export_routes._active_exports.release()
        project_export_routes._active_exports.release()
    assert client.get(f"{root}/evidence/export").status_code == 200


def test_export_batches_knowledge_revisions_without_omitting_last_page(context, tmp_path):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        for number in range(101):
            entry = KnowledgeEntry(project_id=project_id, source_type="document", source_id="removed-source",
                                   source_snapshot={"title": "removed.md", "version": 1},
                                   title=f"entry-{number}", body="revision-content", status="active",
                                   version=1, created_by=1, updated_by=1)
            db.add(entry)
            db.flush()
            db.add(KnowledgeRevision(entry_id=entry.id, version=1, actor_id=1,
                                     title=entry.title, body=entry.body, status="active"))
        db.commit()
    archive_path = tmp_path / "batch.zip"
    archive_path.write_bytes(client.get(f"{root}/evidence/export").content)
    verified = verify(archive_path)
    assert verified["counts"]["knowledge"] == 101
    with zipfile.ZipFile(archive_path) as archive:
        items = [name for name in archive.namelist() if name.startswith("knowledge/")]
        assert len(items) == 101
        assert all(json.loads(archive.read(name))["revisions"][0]["body"] == "revision-content" for name in items)


def test_corrupt_execution_report_blocks_project_export(context):
    client, headers, root, cases = context
    reviewed = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": reviewed["id"], "chain_name": "登录", "estimated_seconds": 20},
        headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    client.put(f"{root}/execution-runs/{run['id']}/cases/{run['items'][0]['id']}", json={
        "version": 1, "result": "passed", "actual_seconds": 10}, headers=headers)
    client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers)
    with SessionLocal() as db:
        report = db.get(ExecutionReport, run["id"])
        report.payload_sha256 = "0" * 64
        db.commit()
    assert client.get(f"{root}/evidence/export").status_code == 503
