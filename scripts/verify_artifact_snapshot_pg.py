"""Check artifact projections against concurrent writes on a disposable PostgreSQL project."""

import os
from unittest.mock import patch

from sqlalchemy import delete, insert, select, text

from api import artifact_routes
from api.artifact_routes import _case_catalog_pages, _verified_import_pages
from api.db import (Case, GovernanceDuplicateGroup, GovernanceMetricSnapshot,
                    ImportBatch, Project, SessionLocal, engine)
from api.read_snapshot import consistent_read


def verify_project_graph_endpoint(client, sessions, project_id: int, batch_id: int,
                                  earliest_id: int, *, expected_nodes: int | None = None,
                                  expected_edges: int | None = None) -> None:
    """Check the public snapshot route against a write after its read view starts."""
    url = f"/api/v1/projects/{project_id}/artifacts/snapshot"
    baseline = client.get(url)
    assert baseline.status_code == 200, baseline.status_code
    graph = baseline.json()
    assert graph["scope"] == "single-read-transaction"
    assert graph["node_count"] == len(graph["nodes"])
    assert graph["edge_count"] == len(graph["edges"]) > 0
    if expected_nodes is not None:
        assert graph["node_count"] == expected_nodes
    if expected_edges is not None:
        assert graph["edge_count"] == expected_edges
    assert all(node["title"] != "另一个项目" for node in graph["nodes"])
    assert len(graph["revision"]) == 64

    original_node = artifact_routes._node
    committed = False

    def concurrent_node(scope, kind, value, snapshot_db):
        nonlocal committed
        result = original_node(scope, kind, value, snapshot_db)
        if kind == "import" and value == str(batch_id) and not committed:
            committed = True
            with sessions.begin() as writer:
                writer.get(Case, earliest_id).title = "图谱并发新标题"
                writer.add(Case(project_id=project_id, import_id=batch_id, original_id="new-graph",
                                title="图谱并发新增", original=[]))
        return result

    with patch.object(artifact_routes, "_node", concurrent_node):
        concurrent = client.get(url)
    assert concurrent.status_code == 200, concurrent.status_code
    old = concurrent.json()
    assert committed and old["revision"] == graph["revision"]
    assert old["node_count"] == graph["node_count"] and old["edge_count"] == graph["edge_count"]
    assert all(node["title"] != "图谱并发新增" for node in old["nodes"])
    changed = client.get(url)
    assert changed.status_code == 200, changed.status_code
    latest = changed.json()
    assert latest["node_count"] == graph["node_count"] + 1
    assert latest["edge_count"] == graph["edge_count"] + 1
    assert latest["revision"] != graph["revision"]
    assert "图谱并发新增" in {node["title"] for node in latest["nodes"]}
    with patch.object(artifact_routes, "MAX_PROJECT_SNAPSHOT_NODES", 1):
        rejected = client.get(url)
    assert rejected.status_code == 413 and "nodes" not in rejected.json()


def verify() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("artifact snapshot smoke requires PostgreSQL")
    if os.getenv("TEST_ATELIER_PG_SMOKE") != "1":
        raise RuntimeError("artifact snapshot smoke requires explicit TEST_ATELIER_PG_SMOKE=1")
    with SessionLocal() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        if head != "20261047_auth_session_revocations":
            raise RuntimeError(f"unexpected migration head: {head}")

    project_ids = []
    try:
        with SessionLocal() as db:
            project = Project(name="ci-artifact-snapshot-smoke")
            foreign = Project(name="ci-artifact-snapshot-foreign")
            db.add_all([project, foreign])
            db.flush()
            project_ids = [project.id, foreign.id]
            batch = ImportBatch(project_id=project.id, filename="smoke.xlsx", headers=[],
                                rows=[], warnings=[], status="done")
            db.add(batch)
            db.flush()
            db.execute(insert(Case), [
                {"project_id": project.id, "import_id": batch.id, "original_id": f"case-{number}",
                 "title": "边界" * 90 if number == 0 else f"用例 {number}", "original": []}
                for number in range(105)
            ])
            # A foreign row with the same import ID must not leak into either view.
            db.add(Case(project_id=foreign.id, import_id=batch.id, original_id="foreign",
                        title="另一个项目", original=[]))
            db.add_all([
                GovernanceDuplicateGroup(project_id=project.id, batch_id=batch.id, gid="smoke",
                                         name_key="smoke", name="重复组", size=1),
                GovernanceMetricSnapshot(project_id=project.id, batch_id=batch.id, key="count", value=105),
            ])
            db.commit()
            project_id, batch_id = project.id, batch.id
            earliest_id = db.scalar(select(Case.id).where(Case.project_id == project_id)
                                    .order_by(Case.id).limit(1))

        with consistent_read(engine) as snapshot:
            before_catalog = list(_case_catalog_pages(project_id, snapshot))
            before_relations = _verified_import_pages(project_id, str(batch_id), snapshot)
            assert len(before_catalog) == 3 and len(before_relations) == 2
            assert sum(len(page["items"]) for _, page in before_catalog) == 105
            assert sum(len(page["edges"]) for page in before_relations) == 107
            assert before_catalog[-1][1]["items"][-1]["title"] == ("边界" * 90)[:160]

            with SessionLocal() as writer:
                writer.get(Case, earliest_id).title = "并发改写的末页"
                writer.add(Case(project_id=project_id, import_id=batch_id, original_id="new",
                                title="并发新增", original=[]))
                writer.commit()

            assert list(_case_catalog_pages(project_id, snapshot)) == before_catalog
            assert _verified_import_pages(project_id, str(batch_id), snapshot) == before_relations

        with consistent_read(engine) as fresh:
            after_catalog = list(_case_catalog_pages(project_id, fresh))
            after_relations = _verified_import_pages(project_id, str(batch_id), fresh)
            assert after_catalog != before_catalog and after_relations != before_relations
            assert sum(len(page["items"]) for _, page in after_catalog) == 106
            assert sum(len(page["edges"]) for page in after_relations) == 108

        # Exercise the public project graph endpoint, not only its catalog
        # helpers, while a separate PostgreSQL writer commits mid-request.
        from fastapi.testclient import TestClient
        from api.main import app

        password = os.getenv("AI_STUDIO_ADMIN_PASSWORD")
        if not password:
            raise RuntimeError("project snapshot smoke requires the configured admin password")
        with TestClient(app, base_url="https://testserver") as client:
            csrf = client.get("/api/v1/auth/csrf").json()["token"]
            login = client.post("/api/v1/auth/login", json={"username": os.getenv("AI_STUDIO_ADMIN_USER", "admin"),
                                                             "password": password}, headers={"x-csrf-token": csrf})
            assert login.status_code == 200, login.status_code
            verify_project_graph_endpoint(client, SessionLocal, project_id, batch_id, earliest_id,
                                          expected_nodes=109, expected_edges=108)
    finally:
        if project_ids:
            with SessionLocal.begin() as db:
                db.execute(delete(Case).where(Case.project_id.in_(project_ids)))
                db.execute(delete(GovernanceMetricSnapshot).where(GovernanceMetricSnapshot.project_id.in_(project_ids)))
                db.execute(delete(GovernanceDuplicateGroup).where(GovernanceDuplicateGroup.project_id.in_(project_ids)))
                db.execute(delete(ImportBatch).where(ImportBatch.project_id.in_(project_ids)))
                db.execute(delete(Project).where(Project.id.in_(project_ids)))


if __name__ == "__main__":
    verify()
