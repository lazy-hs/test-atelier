"""Template instantiation copies configuration, never project data or secrets."""

from sqlalchemy import func, select

from api.db import (Case, ConversionSettings, Membership, Project, ProjectSkill, ProjectTemplate,
                    ProjectTemplateApplication, SessionLocal, TaxonomyVersion)
from api.tests.test_projects import client, login, workbook  # noqa: F401


def test_template_freezes_configuration_and_retains_origin_after_source_delete(client):
    admin = login(client)
    source_id = client.post("/api/v1/projects", json={"name": "结构源"}, headers=admin).json()["id"]
    path = f"/api/v1/projects/{source_id}/templates"
    with SessionLocal() as db:
        skill = db.scalar(select(ProjectSkill).where(ProjectSkill.project_id == source_id))
        assert skill is not None
        skill.enabled = False
        skill_id = skill.skill_id
        db.add(ConversionSettings(project_id=source_id, default_prefix_path="/private/never/copy",
                                  default_case_type="业务功能用例", default_case_status="正常",
                                  history_retention_days=60))
        taxonomy = {key: [{"id": f"{key}-1", "name": key, "description": "确认"}]
                    for key in ("business_module", "business_scenario", "test_dimension", "endpoint_type")}
        db.add(TaxonomyVersion(project_id=source_id, number=1, status="published", revision=2,
                               definitions=taxonomy, created_by=1))
        db.commit()
    source_case = client.post(f"/api/v1/projects/{source_id}/imports/preview",
                              files={"file": ("cases.xlsx", workbook())}, headers=admin).json()
    client.post(f"/api/v1/projects/{source_id}/imports/{source_case['id']}/confirm", headers=admin)
    assert client.post(path, json={"name": "模板"}).status_code == 403
    created = client.post(path, json={"name": "配置基线"}, headers=admin)
    assert created.status_code == 200, created.text
    template = created.json()
    assert template["has_taxonomy"] and template["has_conversion_settings"]
    assert template["skill_count"] >= 1
    detail = client.get(f"{path}/{template['id']}").json()
    assert detail["snapshot"]["conversion"] == {"default_case_type": "业务功能用例",
                                               "default_case_status": "正常", "history_retention_days": 60}
    assert "prefix_path" not in str(detail["snapshot"])
    assert "private/never/copy" not in str(detail["snapshot"])
    assert detail["snapshot"]["taxonomy"]["definitions"] == taxonomy
    with SessionLocal() as db:
        db.get(ProjectSkill, (source_id, skill_id)).enabled = True
        db.get(ConversionSettings, source_id).history_retention_days = 30
        db.commit()
    assert client.get(f"{path}/{template['id']}").json()["snapshot"] == detail["snapshot"]
    assert client.post(f"{path}/{template['id']}/instantiate", json={"name": "bad"}).status_code == 403
    generated = client.post(f"{path}/{template['id']}/instantiate", json={"name": "新项目"}, headers=admin)
    assert generated.status_code == 200, generated.text
    target_id = generated.json()["id"]
    origin = client.get(f"/api/v1/projects/{target_id}/templates/origin").json()
    assert origin["source_project_id"] == source_id
    assert origin["snapshot_sha256"] == template["snapshot_sha256"]
    with SessionLocal() as db:
        assert db.get(ProjectSkill, (target_id, skill_id)).enabled is False
        settings = db.get(ConversionSettings, target_id)
        assert settings.history_retention_days == 60 and settings.default_prefix_path == ""
        taxonomy_copy = db.scalar(select(TaxonomyVersion).where(TaxonomyVersion.project_id == target_id))
        assert taxonomy_copy.number == 1 and taxonomy_copy.status == "published"
        assert taxonomy_copy.definitions == taxonomy
        assert db.scalar(select(func.count(Case.id)).where(Case.project_id == target_id)) == 0
        assert db.scalar(select(func.count(Membership.user_id)).where(Membership.project_id == target_id)) == 1
        application = db.get(ProjectTemplateApplication, target_id)
        assert application.template_id == template["id"]
        assert application.snapshot_sha256 == template["snapshot_sha256"]
    assert client.delete(f"/api/v1/projects/{source_id}", headers=admin).status_code == 200
    assert client.get(f"/api/v1/projects/{target_id}/templates/origin").json() == origin
    with SessionLocal() as db:
        assert db.get(ProjectTemplate, template["id"]) is None
        assert db.get(ProjectTemplateApplication, target_id).source_project_id == source_id
        assert db.get(Project, target_id) is not None


def test_template_rejects_cross_project_access_and_missing_skill_without_creating_project(client, monkeypatch):
    from api import project_template_routes

    admin = login(client)
    source_id = client.post("/api/v1/projects", json={"name": "源配置"}, headers=admin).json()["id"]
    other_id = client.post("/api/v1/projects", json={"name": "隔离项目"}, headers=admin).json()["id"]
    path = f"/api/v1/projects/{source_id}/templates"
    template = client.post(path, json={"name": "技能配置"}, headers=admin).json()
    assert client.get(f"/api/v1/projects/{other_id}/templates/{template['id']}").status_code == 404
    assert client.post(f"/api/v1/projects/{other_id}/templates/{template['id']}/instantiate",
                       json={"name": "wrong"}, headers=admin).status_code == 404
    reader = client.post("/api/v1/users", json={"username": "template-reader",
                                              "password": "reader-password-123", "role": "reader"}, headers=admin).json()
    client.post(f"/api/v1/projects/{source_id}/members", json={"user_id": reader["id"]}, headers=admin)
    client.post("/api/v1/auth/logout", headers=admin)
    reader_headers = login(client, "template-reader", "reader-password-123")
    assert client.get(path).json()[0]["id"] == template["id"]
    assert client.post(path, json={"name": "no"}, headers=reader_headers).status_code == 403
    assert client.post(f"{path}/{template['id']}/instantiate", json={"name": "no"},
                       headers=reader_headers).status_code == 403
    client.post("/api/v1/auth/logout", headers=reader_headers)
    admin = login(client)
    monkeypatch.setattr(project_template_routes, "discover_skills", lambda: [])
    before = len(client.get("/api/v1/projects").json())
    result = client.post(f"{path}/{template['id']}/instantiate", json={"name": "cannot"}, headers=admin)
    assert result.status_code == 409
    assert len(client.get("/api/v1/projects").json()) == before
