"""Project-local references must not expose cross-project artifacts or stale guesses."""

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from api import artifact_routes
from api.artifact_routes import _directory_id
from api.db import (AnalysisRun, Base, Case, CaseDraft, CaseSuggestion, GovernanceChangeLog, GovernanceDirectory,
                    GovernanceDuplicateGroup, GovernanceDuplicateMember, GovernanceExportJob,
                    GovernanceMetricSnapshot, GovernanceValidationRun, HarnessTask,
                    ImportBatch, KnowledgeEntry, Project, SessionLocal, SourceDocument, SuggestionJob, TaxonomyVersion)
from api.read_snapshot import consistent_read
from api.tests.test_projects import client, login  # noqa: F401
from api.tests.test_smoke import context, create_suite, review_yes  # noqa: F401


def test_project_snapshot_contains_disconnected_catalog_and_complete_direct_links(context):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other = client.post("/api/v1/projects", json={"name": "Other snapshot"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(KnowledgeEntry(project_id=project_id, title="独立知识", body="独立内容",
                              source_type="case", source_id=str(cases[0]["id"]),
                              source_snapshot={"title": cases[0]["title"]}, created_by=1, updated_by=1))
        db.add(Case(project_id=other, import_id=cases[0]["import_id"], original_id="foreign",
                    title="不属于本项目", original=[]))
        db.commit()

    response = client.get(f"{root}/artifacts/snapshot")
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["scope"] == "single-read-transaction"
    assert snapshot["project_id"] == project_id
    assert snapshot["node_count"] == len(snapshot["nodes"])
    assert snapshot["edge_count"] == len(snapshot["edges"])
    assert len(snapshot["revision"]) == 64
    assert len({(node["type"], node["id"]) for node in snapshot["nodes"]}) == snapshot["node_count"]
    assert all(node["title"] != "不属于本项目" for node in snapshot["nodes"])
    assert {(edge["from"]["type"], edge["to"]["type"], edge["relation"])
            for edge in snapshot["edges"]} >= {("import", "case", "imported_as"),
                                                 ("case", "knowledge", "curated_as")}
    assert all((end["type"], end["id"]) in {(node["type"], node["id"]) for node in snapshot["nodes"]}
               for edge in snapshot["edges"] for end in (edge["from"], edge["to"]))
    assert client.get(f"{root}/artifacts/snapshot").json()["revision"] == snapshot["revision"]
    with SessionLocal() as db:
        db.get(Case, cases[0]["id"]).title = "同项目变化"
        db.commit()
    assert client.get(f"{root}/artifacts/snapshot").json()["revision"] != snapshot["revision"]


def test_project_snapshot_rejects_over_limit_without_partial_graph(context, monkeypatch):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    monkeypatch.setattr(artifact_routes, "MAX_PROJECT_SNAPSHOT_NODES", 40)
    with SessionLocal() as db:
        db.add_all(Case(project_id=project_id, import_id=cases[0]["import_id"],
                        original_id=f"extra-{index}", title=f"增加的用例 {index}", original=[])
                   for index in range(artifact_routes.MAX_PROJECT_SNAPSHOT_NODES))
        db.commit()
    response = client.get(f"{root}/artifacts/snapshot")
    assert response.status_code == 413, response.text
    assert "快照上限" in response.json()["detail"]


def test_project_snapshot_rejects_edge_byte_and_time_limits(context, monkeypatch):
    client, _, root, _ = context
    endpoint = f"{root}/artifacts/snapshot"
    assert client.get(endpoint).json()["edge_count"] > 0
    for setting, limit, status in (("MAX_PROJECT_SNAPSHOT_EDGES", 0, 413),
                                   ("MAX_PROJECT_SNAPSHOT_BYTES", 1, 413),
                                   ("MAX_PROJECT_SNAPSHOT_SECONDS", -1, 503)):
        with monkeypatch.context() as patch:
            patch.setattr(artifact_routes, setting, limit)
            response = client.get(endpoint)
            assert response.status_code == status, response.text
            assert "nodes" not in response.json() and "edges" not in response.json()


def test_project_snapshot_checks_complete_response_size_and_serialization_deadline(context, monkeypatch):
    client, _, root, _ = context
    endpoint = f"{root}/artifacts/snapshot"
    full = client.get(endpoint).json()
    content = {key: value for key, value in full.items()
               if key not in {"revision", "node_count", "edge_count"}}
    content_size = len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > content_size
    with monkeypatch.context() as patch:
        patch.setattr(artifact_routes, "MAX_PROJECT_SNAPSHOT_BYTES", content_size)
        response = client.get(endpoint)
        assert response.status_code == 413
        assert "nodes" not in response.json() and "edges" not in response.json()

    clock = [0.0]

    def serialized(value, **kwargs):
        result = json.dumps(value, **kwargs)
        if isinstance(value, dict) and "revision" in value:
            clock[0] = 1.0
        return result

    with monkeypatch.context() as patch:
        patch.setattr(artifact_routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        patch.setattr(artifact_routes, "json", SimpleNamespace(dumps=serialized))
        patch.setattr(artifact_routes, "MAX_PROJECT_SNAPSHOT_SECONDS", 0.5)
        response = client.get(endpoint)
        assert response.status_code == 503
        assert "超时" in response.json()["detail"]
        assert "nodes" not in response.json() and "edges" not in response.json()
    assert client.get(endpoint).status_code == 200


def test_project_snapshot_rejects_when_process_slots_are_busy_and_recovers(context):
    client, _, root, _ = context
    endpoint = f"{root}/artifacts/snapshot"
    slots = artifact_routes._active_project_snapshots
    acquired = 0
    try:
        for _ in range(2):
            assert slots.acquire(blocking=False)
            acquired += 1
        busy = client.get(endpoint)
        assert busy.status_code == 503
        assert busy.headers["Retry-After"] == "5"
        assert "服务忙" in busy.json()["detail"]
        assert "nodes" not in busy.json() and "edges" not in busy.json()
    finally:
        for _ in range(acquired):
            slots.release()
    assert client.get(endpoint).status_code == 200


def test_project_snapshot_releases_slot_after_generation_exception(context, monkeypatch):
    client, _, root, _ = context
    endpoint = f"{root}/artifacts/snapshot"
    with monkeypatch.context() as patch:
        def fail_node(*args, **kwargs):
            raise RuntimeError("snapshot generation failed")

        patch.setattr(artifact_routes, "_node", fail_node)
        with pytest.raises(RuntimeError, match="snapshot generation failed"):
            client.get(endpoint)
    slots = artifact_routes._active_project_snapshots
    assert slots.acquire(blocking=False)
    try:
        assert slots.acquire(blocking=False)
        try:
            assert client.get(endpoint).status_code == 503
        finally:
            slots.release()
    finally:
        slots.release()
    assert client.get(endpoint).status_code == 200


def test_project_snapshot_keeps_one_sqlite_view_through_concurrent_commit(context, tmp_path, monkeypatch):
    client, _, root, _ = context
    engine = create_engine(f"sqlite:///{tmp_path / 'project-graph.db'}", connect_args={"timeout": 2})
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        with Session(engine) as writer:
            writer.add(Project(name="snapshot"))
            writer.flush()
            batch = ImportBatch(project_id=1, filename="cases.xlsx", headers=[], rows=[], warnings=[])
            writer.add(batch)
            writer.flush()
            writer.add(Case(project_id=1, import_id=batch.id, original_id="one", title="原始标题", original=[]))
            writer.commit()
            batch_id = batch.id

        original_node = artifact_routes._node
        wrote = False

        def concurrent_node(project_id, kind, value, snapshot_db):
            nonlocal wrote
            result = original_node(project_id, kind, value, snapshot_db)
            if kind == "import" and not wrote:
                wrote = True
                with Session(engine) as writer:
                    writer.query(Case).filter_by(project_id=1).first().title = "并发新标题"
                    writer.add(Case(project_id=1, import_id=batch_id, original_id="two",
                                    title="并发新增", original=[]))
                    writer.commit()
            return result

        monkeypatch.setattr(artifact_routes, "consistent_read", lambda _: consistent_read(engine))
        monkeypatch.setattr(artifact_routes, "_node", concurrent_node)
        response = client.get(f"{root}/artifacts/snapshot")
        assert response.status_code == 200, response.text
        old = response.json()
        assert wrote
        assert {node["title"] for node in old["nodes"] if node["type"] == "case"} == {"原始标题"}
        assert len([edge for edge in old["edges"] if edge["relation"] == "imported_as"]) == 1
        fresh = client.get(f"{root}/artifacts/snapshot").json()
        assert {node["title"] for node in fresh["nodes"] if node["type"] == "case"} == {
            "并发新标题", "并发新增"}
        assert fresh["revision"] != old["revision"]
    finally:
        engine.dispose()


def test_directory_catalog_and_relations_use_project_paths_and_stable_cursors(context):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Foreign directories"}, headers=headers).json()["id"]
    parent, child = "业务", "业务/登录"
    with SessionLocal() as db:
        db.add_all([GovernanceDirectory(project_id=project_id, path=parent, parent="", level=0),
                    GovernanceDirectory(project_id=project_id, path=child, parent=parent, level=1),
                    GovernanceDirectory(project_id=other_id, path="secret", parent="", level=0),
                    GovernanceChangeLog(project_id=project_id, entity="directory", entity_id=child,
                                        action="whitelist", before_json={}, after_json={"whitelisted": True},
                                        batch_key="directories", operator_id=1)])
        for index in range(43):
            db.add(GovernanceDirectory(project_id=project_id, path=f"folder-{index:02d}", parent="", level=0))
        db.get(Case, cases[0]["id"]).directory = child
        db.flush()
        change_id = db.query(GovernanceChangeLog.id).filter_by(project_id=project_id, entity="directory").scalar()
        db.commit()

    base = root + "/artifacts"
    first = client.get(f"{base}?kind=governance_directory")
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 40
    cursor = first.json()["next_before_id"]
    second = client.get(f"{base}?kind=governance_directory&before_id={quote(cursor)}")
    assert second.status_code == 200, second.text
    all_ids = {item["id"] for item in first.json()["items"] + second.json()["items"]}
    assert len(all_ids) == 45
    assert {_directory_id(parent), _directory_id(child)} <= all_ids
    assert _directory_id("secret") not in all_ids
    assert second.json()["next_before_id"] is None

    graph = client.get(f"{base}/governance_directory/{_directory_id(child)}/relations")
    assert graph.status_code == 200, graph.text
    assert {(edge["relation"], edge["to"]["type"]) for edge in graph.json()["edges"]} == {
        ("contains_directory", "governance_directory"), ("filed_in_directory", "case"),
        ("governed_as", "governance_change")}
    assert "filed_in_directory" in {edge["relation"] for edge in client.get(
        f"{base}/case/{cases[0]['id']}/relations").json()["edges"]}
    assert "governed_as" in {edge["relation"] for edge in client.get(
        f"{base}/governance_change/{change_id}/relations").json()["edges"]}
    assert client.get(f"{base}/governance_directory/{_directory_id('secret')}/relations").status_code == 404
    assert client.get(f"/api/v1/projects/{other_id}/artifacts/governance_directory/"
                      f"{_directory_id(child)}/relations").status_code == 404
    for invalid in ("dir_a", "dir_!", "dir_", "dir_44"):
        assert client.get(f"{base}/governance_directory/{invalid}/relations").status_code == 422


def test_verified_catalog_rejects_cross_page_changes_without_mixing_projects(context):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Other catalog"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        batch = cases[0]["import_id"]
        db.add_all(Case(project_id=project_id, import_id=batch, original_id=f"case-{index}",
                        title=f"用例 {index}", original=[]) for index in range(83))
        db.get(Case, cases[0]["id"]).title = "边界" * 90
        db.get(Case, cases[0]["id"]).version = 2
        db.commit()
    base = f"{root}/artifacts?kind=case"
    first = client.get(base + "&consistency=verified")
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 40
    revision = first.json()["revision"]
    cursor = first.json()["next_before_id"]
    next_page = f"{base}&consistency=verified&before_id={cursor}&revision={revision}"
    second = client.get(next_page)
    assert second.status_code == 200, second.text
    assert second.json()["revision"] == revision and len(second.json()["items"]) == 40
    assert not {item["id"] for item in first.json()["items"]}.intersection(
        item["id"] for item in second.json()["items"])
    cursor_after_second = second.json()["next_before_id"]
    third = client.get(f"{base}&consistency=verified&before_id={cursor_after_second}&revision={revision}")
    assert third.status_code == 200 and third.json()["next_before_id"] is None
    verified_items = first.json()["items"] + second.json()["items"] + third.json()["items"]
    live_items = []
    live_cursor = None
    while True:
        suffix = f"&before_id={live_cursor}" if live_cursor else ""
        page = client.get(base + suffix).json()
        live_items.extend(page["items"])
        live_cursor = page["next_before_id"]
        if live_cursor is None:
            break
    assert verified_items == live_items
    assert next(item for item in verified_items if item["id"] == str(cases[0]["id"]))["title"] == ("边界" * 90)[:160]

    with SessionLocal() as db:
        db.add(Case(project_id=other_id, import_id=batch, original_id="foreign", title="其他项目", original=[]))
        db.commit()
    assert client.get(next_page).status_code == 200
    with SessionLocal() as db:
        db.get(Case, cases[0]["id"]).title = "尚未加载的旧用例更新"
        db.commit()
    changed = client.get(next_page)
    assert changed.status_code == 409, changed.text
    refreshed = client.get(base + "&consistency=verified")
    assert refreshed.status_code == 200 and refreshed.json()["revision"] != revision
    assert client.get(base + f"&before_id={cursor}").status_code == 200
    assert client.get(base + f"&revision={revision}").status_code == 422
    assert client.get(base + f"&consistency=verified&before_id={cursor}").status_code == 422
    assert client.get(base + f"&consistency=verified&revision={revision}").status_code == 422
    assert client.get(base + f"&consistency=verified&before_id={cases[0]['id']}&revision={refreshed.json()['revision']}").status_code == 409


def test_verified_catalog_limit_does_not_silently_fall_back_to_live(context, monkeypatch):
    client, _, root, _ = context
    monkeypatch.setattr(artifact_routes, "MAX_VERIFIED_CATALOG_ITEMS", 2)
    base = f"{root}/artifacts?kind=case"
    verified = client.get(base + "&consistency=verified")
    assert verified.status_code == 413 and "实时目录" in verified.text
    live = client.get(base + "&consistency=live")
    assert live.status_code == 200 and len(live.json()["items"]) == 3
    assert "revision" not in live.json()


def test_directory_relations_page_all_changes_without_leaking_other_projects(context):
    client, headers, root, _ = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Other project"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(GovernanceDirectory(project_id=project_id, path="治理目录", parent="", level=0))
        db.add(GovernanceDirectory(project_id=other_id, path="治理目录", parent="", level=0))
        for index in range(105):
            db.add(GovernanceChangeLog(project_id=project_id, entity="directory", entity_id="治理目录",
                                       action="whitelist", before_json={}, after_json={"index": index},
                                       batch_key="history", operator_id=1))
        db.add(GovernanceChangeLog(project_id=other_id, entity="directory", entity_id="治理目录",
                                   action="whitelist", before_json={}, after_json={},
                                   batch_key="history", operator_id=1))
        db.commit()
    base = root + "/artifacts"
    directory = _directory_id("治理目录")
    first = client.get(f"{base}/governance_directory/{directory}/relations").json()
    second = client.get(f"{base}/governance_directory/{directory}/relations?offset=100").json()
    assert first["truncated"] is True and first["next_offset"] == 100
    assert len(first["edges"]) == 100 and len(second["edges"]) == 5
    assert second["next_offset"] is None
    assert len({edge["to"]["id"] for edge in first["edges"] + second["edges"]}) == 105


def test_directory_prefix_index_does_not_conflate_long_sibling_paths(context):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    prefix = "项目/" + "长" * 126
    first_path, second_path = prefix + "/甲", prefix + "/乙"
    with SessionLocal() as db:
        db.add_all([GovernanceDirectory(project_id=project_id, path=prefix, parent="", level=0),
                    GovernanceDirectory(project_id=project_id, path=first_path, parent=prefix, level=1),
                    GovernanceDirectory(project_id=project_id, path=second_path, parent=prefix, level=1)])
        db.get(Case, cases[0]["id"]).directory = first_path
        db.get(Case, cases[1]["id"]).directory = second_path
        db.commit()
    base = root + "/artifacts/governance_directory"
    parent = client.get(f"{base}/{_directory_id(prefix)}/relations")
    assert parent.status_code == 200, parent.text
    children = {edge["to"]["id"] for edge in parent.json()["edges"]}
    assert children == {_directory_id(first_path), _directory_id(second_path)}
    first = client.get(f"{base}/{_directory_id(first_path)}/relations")
    assert first.status_code == 200, first.text
    assert {edge["to"]["id"] for edge in first.json()["edges"] if edge["to"]["type"] == "case"} == {
        str(cases[0]["id"])}


def test_import_deep_pages_skip_only_scoped_complete_case_edges(context, monkeypatch):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Other import owner"}, headers=headers).json()["id"]
    batch_id = cases[0]["import_id"]
    with SessionLocal() as db:
        db.execute(insert(Case), [
            {"project_id": project_id, "import_id": batch_id, "original_id": f"more-{index}",
             "title": f"用例 {index}", "original": []} for index in range(205)
        ])
        db.add(Case(project_id=other_id, import_id=batch_id, original_id="foreign",
                    title="跨项目用例", original=[]))
        db.add_all(GovernanceMetricSnapshot(project_id=project_id, batch_id=batch_id,
                                            key=f"metric-{index}", value=index) for index in range(2))
        db.commit()
    url = f"{root}/artifacts/import/{batch_id}/relations"
    pages = [client.get(url), client.get(url + "?offset=100")]
    original_node = artifact_routes._node
    resolved_cases = 0

    def counted_node(project, kind, identifier, db):
        nonlocal resolved_cases
        if kind == "case":
            resolved_cases += 1
        return original_node(project, kind, identifier, db)

    monkeypatch.setattr(artifact_routes, "_node", counted_node)
    pages.extend([client.get(url + "?offset=200"), client.get(url + "?offset=210")])
    assert resolved_cases <= 12
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert [len(page.json()["edges"]) for page in pages] == [100, 100, 10, 0]
    all_edges = [edge for page in pages for edge in page.json()["edges"]]
    assert len({(edge["to"]["type"], edge["to"]["id"]) for edge in all_edges}) == 210
    assert {edge["to"]["type"] for edge in all_edges} == {"case", "governance_metric"}
    assert pages[-1].json()["truncated"] is False


def test_verified_import_pages_match_live_with_other_edges_and_detect_deep_change(context):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    other_id = client.post("/api/v1/projects", json={"name": "Unrelated import"}, headers=headers).json()["id"]
    batch_id = cases[0]["import_id"]
    with SessionLocal() as db:
        db.execute(insert(Case), [
            {"project_id": project_id, "import_id": batch_id, "original_id": str(index),
             "title": "边界" * 90 if index == 200 else f"用例 {index}", "original": []}
            for index in range(205)
        ])
        db.add(Case(project_id=other_id, import_id=batch_id, original_id="foreign",
                    title="其他项目", original=[]))
        db.add_all([
            GovernanceDuplicateGroup(project_id=project_id, batch_id=batch_id, gid="one",
                                     name_key="one", name="重复组", size=1),
            GovernanceMetricSnapshot(project_id=project_id, batch_id=batch_id, key="count", value=1),
            KnowledgeEntry(project_id=project_id, source_type="import", source_id=str(batch_id),
                           source_snapshot={"title": "import"}, title="历史知识", body="记录",
                           created_by=1, updated_by=1),
        ])
        db.commit()
    base = f"{root}/artifacts/import/{batch_id}/relations"
    revision = None
    for offset in (0, 100, 200):
        live = client.get(f"{base}?offset={offset}")
        verified_url = f"{base}?offset={offset}&consistency=verified"
        if revision:
            verified_url += f"&revision={revision}"
        verified = client.get(verified_url)
        assert live.status_code == verified.status_code == 200
        payload = verified.json()
        revision = payload.pop("revision")
        assert payload == live.json()
        assert len(payload["edges"]) == (100 if offset < 200 else 11)
    with SessionLocal() as db:
        tail = db.query(Case).filter_by(project_id=project_id, import_id=batch_id).order_by(Case.id.desc()).first()
        tail.title = "最后一页标题更新"
        db.commit()
    assert client.get(f"{base}?offset=100&consistency=verified&revision={revision}").status_code == 409


def test_verified_import_empty_and_limits_preserve_live_api(context, monkeypatch):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        empty = ImportBatch(project_id=project_id, filename="empty.xlsx", headers=[], rows=[],
                            warnings=[], status="done")
        db.add(empty)
        db.commit()
        empty_id = empty.id
    url = f"{root}/artifacts/import/{empty_id}/relations"
    live = client.get(url)
    verified = client.get(url + "?consistency=verified")
    assert live.status_code == verified.status_code == 200
    result = verified.json()
    assert len(result.pop("revision")) == 64 and result["edges"] == []
    assert result == live.json()

    nonempty = f"{root}/artifacts/import/{cases[0]['import_id']}/relations"
    monkeypatch.setattr(artifact_routes, "MAX_VERIFIED_EDGES", 2)
    assert client.get(nonempty + "?consistency=verified").status_code == 413
    assert client.get(nonempty).status_code == 200
    monkeypatch.setattr(artifact_routes, "MAX_VERIFIED_EDGES", 5000)
    monkeypatch.setattr(artifact_routes, "MAX_VERIFIED_BYTES", 32)
    assert client.get(nonempty + "?consistency=verified").status_code == 413


def test_import_above_verified_limit_still_has_live_deep_pages_with_adjacent_guard(context):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    batch_id = cases[0]["import_id"]
    with SessionLocal() as db:
        db.execute(insert(Case), [
            {"project_id": project_id, "import_id": batch_id, "original_id": f"overflow-{index}",
             "title": f"合成用例 {index}", "original": []}
            for index in range(5001 - len(cases))
        ])
        db.commit()
    base = f"{root}/artifacts/import/{batch_id}/relations"
    assert client.get(base + "?consistency=verified").status_code == 413
    first = client.get(base + "?consistency=live")
    assert first.status_code == 200 and len(first.json()["edges"]) == 100
    penultimate = client.get(base + "?consistency=live&offset=4900")
    assert penultimate.status_code == 200 and len(penultimate.json()["edges"]) == 100
    checkpoint = penultimate.json()["page_checkpoint"]
    last_url = f"{base}?consistency=live&offset=5000&previous_page={checkpoint}"
    last = client.get(last_url)
    assert last.status_code == 200 and len(last.json()["edges"]) == 1
    assert last.json()["next_offset"] is None
    with SessionLocal() as db:
        changed_id = int(penultimate.json()["edges"][0]["to"]["id"])
        db.get(Case, changed_id).title = "相邻页已修改"
        db.commit()
    assert client.get(last_url).status_code == 409
    refreshed = client.get(base + "?consistency=live&offset=4900").json()
    assert refreshed["page_checkpoint"] != checkpoint
    assert client.get(f"{base}?consistency=live&offset=5000&previous_page={refreshed['page_checkpoint']}").status_code == 200


def test_governance_and_suggestion_artifacts_are_scoped_and_traversable(context):
    client, headers, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    case_id, batch_id = cases[0]["id"], cases[0]["import_id"]
    with SessionLocal() as db:
        taxonomy = TaxonomyVersion(project_id=project_id, number=1, definitions={"domains": ["登录"]},
                                   status="published", created_by=1)
        group = GovernanceDuplicateGroup(project_id=project_id, batch_id=batch_id, gid="dupes",
                                         name_key="login", name="登录", size=1)
        change = GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case_id),
                                     action="edit", before_json={}, after_json={"level": "1"},
                                     batch_key="history", operator_id=1)
        db.add_all([taxonomy, group, change])
        db.flush()
        db.add_all([
            SuggestionJob(id="suggestions-1", project_id=project_id, created_by=1,
                          taxonomy_version_id=taxonomy.id, provider="local", case_ids=[case_id], total=1,
                          errors=[]),
            GovernanceDuplicateMember(group_id=group.id, case_id=case_id, cluster_fp="fp", decision="keep"),
            GovernanceMetricSnapshot(project_id=project_id, batch_id=batch_id, key="duplicates", value=1),
            GovernanceValidationRun(project_id=project_id, filename="new.csv", rows=[], result={}, created_by=1),
            GovernanceExportJob(project_id=project_id, kind="cases", format="xlsx", path="/private/export.xlsx",
                                rows=3, selfcheck={"readback": True}, created_by=1),
            GovernanceChangeLog(project_id=project_id, entity="case", entity_id=str(case_id),
                                action="undo", before_json={}, after_json={}, batch_key="history",
                                operator_id=1, revert_of=change.id),
        ])
        db.flush()
        suggestion = CaseSuggestion(project_id=project_id, case_id=case_id, case_version=1,
                                    taxonomy_version_id=taxonomy.id, job_id="suggestions-1", source="model",
                                    categories={"domain": "登录"}, rationale="规则", created_by=1)
        db.add(suggestion)
        db.flush()
        suggestion_id, group_id = suggestion.id, group.id
        db.commit()
    base = root + "/artifacts"
    for kind in ("taxonomy", "suggestion_job", "suggestion", "governance_change", "duplicate_group",
                 "duplicate_member", "governance_validation", "governance_metric", "governance_export_job"):
        response = client.get(f"{base}?kind={kind}")
        assert response.status_code == 200, response.text
        assert response.json()["items"] and all(item["type"] == kind for item in response.json()["items"])
    edges = client.get(f"{base}/suggestion/{suggestion_id}/relations").json()["edges"]
    assert {edge["relation"] for edge in edges} == {"suggested_for", "suggested_with", "produced_suggestion"}
    case_edges = client.get(f"{base}/case/{case_id}/relations").json()["edges"]
    assert {"suggested_for", "reviewed_as_duplicate", "governed_as"} <= {edge["relation"] for edge in case_edges}
    assert {"grouped_duplicates", "measured_as"} <= {
        edge["relation"] for edge in client.get(f"{base}/import/{batch_id}/relations").json()["edges"]}
    assert {"contains_duplicate", "grouped_duplicates"} == {
        edge["relation"] for edge in client.get(f"{base}/duplicate_group/{group_id}/relations").json()["edges"]}
    assert "reverts" in {edge["relation"] for edge in client.get(
        f"{base}/governance_change/{change.id}/relations").json()["edges"]}
    assert client.get(f"{base}/suggestion/invalid/relations").status_code == 422


def test_artifact_catalog_rejects_foreign_case_in_suggestion(context):
    client, headers, root, cases = context
    other_id = client.post("/api/v1/projects", json={"name": "Wrong suggestion owner"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        suggestion = CaseSuggestion(project_id=other_id, case_id=cases[0]["id"], case_version=1,
                                    source="rule", categories={}, rationale="wrong owner", created_by=1)
        db.add(suggestion)
        db.flush()
        suggestion_id = suggestion.id
        db.commit()
    assert client.get(f"/api/v1/projects/{other_id}/artifacts/suggestion/{suggestion_id}/relations").status_code == 404
    assert client.get(f"/api/v1/projects/{other_id}/artifacts?kind=suggestion").status_code == 409
    assert client.get(f"{root}/artifacts?kind=suggestion").json()["items"] == []


def test_suggestion_catalog_and_case_relations_page_without_dropping_late_ids(context):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    case_id = cases[0]["id"]
    with SessionLocal() as db:
        db.add_all(CaseSuggestion(project_id=project_id, case_id=case_id, case_version=1,
                                  source="rule", categories={}, rationale=f"candidate-{number}",
                                  created_by=1) for number in range(105))
        db.commit()
    base = root + "/artifacts"
    catalog = client.get(f"{base}?kind=suggestion").json()
    assert len(catalog["items"]) == 40 and catalog["next_before_id"] is not None
    seen = {item["id"] for item in catalog["items"]}
    while catalog["next_before_id"] is not None:
        catalog = client.get(f"{base}?kind=suggestion&before_id={catalog['next_before_id']}").json()
        seen.update(item["id"] for item in catalog["items"])
    assert len(seen) == 105
    first = client.get(f"{base}/case/{case_id}/relations").json()
    assert first["truncated"] is True and first["next_offset"] == 100
    last = client.get(f"{base}/case/{case_id}/relations?offset=100").json()
    assert last["truncated"] is False and len(last["edges"]) == 6
    assert len({(edge["from"]["type"], edge["from"]["id"], edge["to"]["type"], edge["to"]["id"])
                for edge in first["edges"] + last["edges"]}) == 106


def test_relation_page_checkpoint_rejects_changed_previous_page(context):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        db.add_all(CaseSuggestion(project_id=project_id, case_id=cases[0]["id"], case_version=1,
                                  source="rule", categories={}, rationale=f"candidate-{index}",
                                  created_by=1) for index in range(105))
        db.commit()
    base = f"{root}/artifacts/case/{cases[0]['id']}/relations"
    first = client.get(base).json()
    token = first["page_checkpoint"]
    assert len(token) == 64 and token == client.get(base).json()["page_checkpoint"]
    second = client.get(f"{base}?offset=100&previous_page={token}")
    assert second.status_code == 200, second.text
    assert len(second.json()["edges"]) == 6
    assert client.get(f"{base}?offset=100&previous_page=bad").status_code == 422
    assert client.get(f"{base}?offset=1&previous_page={token}").status_code == 422
    with SessionLocal() as db:
        first_suggestion_id = db.query(CaseSuggestion.id).filter_by(project_id=project_id).order_by(
            CaseSuggestion.id).limit(1).scalar()
        db.delete(db.get(CaseSuggestion, first_suggestion_id))
        db.commit()
    changed = client.get(f"{base}?offset=100&previous_page={token}")
    assert changed.status_code == 409 and "从头刷新" in changed.text
    renewed = client.get(base).json()
    assert renewed["page_checkpoint"] != token
    assert client.get(f"{base}?offset=100&previous_page={renewed['page_checkpoint']}").status_code == 200
    with SessionLocal() as db:
        db.get(Case, cases[0]["id"]).title = "已更新的用例标题"
        db.commit()
    assert client.get(f"{base}?offset=100&previous_page={renewed['page_checkpoint']}").status_code == 409


def test_verified_relation_cursor_detects_changes_outside_previous_page(context):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        db.add_all(CaseSuggestion(project_id=project_id, case_id=cases[0]["id"], case_version=1,
                                  source="rule", categories={}, rationale=f"candidate-{index}",
                                  created_by=1) for index in range(105))
        db.commit()
    base = f"{root}/artifacts/case/{cases[0]['id']}/relations"
    first_response = client.get(base + "?consistency=verified")
    assert first_response.status_code == 200, first_response.text
    first = first_response.json()
    assert len(first["revision"]) == 64 and first["next_offset"] == 100
    subsequent = client.get(f"{base}?consistency=verified&offset=100&revision={first['revision']}")
    assert subsequent.status_code == 200, subsequent.text
    assert subsequent.json()["revision"] == first["revision"]
    assert len(subsequent.json()["edges"]) == 6
    assert client.get(f"{base}?consistency=verified&offset=100").status_code == 422
    assert client.get(f"{base}?consistency=verified&offset=1&revision={first['revision']}").status_code == 422
    assert client.get(f"{base}?consistency=verified&offset=200&revision={first['revision']}").status_code == 409
    with SessionLocal() as db:
        latest = db.query(CaseSuggestion).filter_by(project_id=project_id).order_by(CaseSuggestion.id.desc()).first()
        latest.status = "rejected"  # The last page changes; the first page does not.
        db.commit()
    assert client.get(base).json()["page_checkpoint"] == first["page_checkpoint"]
    assert client.get(f"{base}?consistency=verified&offset=100&revision={first['revision']}").status_code == 409
    refreshed = client.get(base + "?consistency=verified").json()
    assert refreshed["revision"] != first["revision"]
    assert client.get(f"{base}?consistency=verified&revision={refreshed['revision']}").status_code == 422
    assert client.get(f"{base}?consistency=verified&offset=100&revision={refreshed['revision']}&previous_page={first['page_checkpoint']}").status_code == 422


def test_verified_relation_cursor_fails_closed_when_budget_exceeded(context, monkeypatch):
    client, _, root, cases = context
    project_id = int(root.rsplit("/", 1)[-1])
    with SessionLocal() as db:
        db.add_all(CaseSuggestion(project_id=project_id, case_id=cases[0]["id"], case_version=1,
                                  source="rule", categories={}, rationale=f"candidate-{index}",
                                  created_by=1) for index in range(105))
        db.commit()
    base = f"{root}/artifacts/case/{cases[0]['id']}/relations"
    monkeypatch.setattr(artifact_routes, "MAX_VERIFIED_EDGES", 100)
    result = client.get(base + "?consistency=verified")
    assert result.status_code == 413 and "一致性游标上限" in result.text
    assert "revision" not in client.get(base).json()


def test_artifact_references_follow_explicit_ids_and_frozen_knowledge(client):
    headers = login(client)
    first = client.post("/api/v1/projects", json={"name": "关联项目"}, headers=headers).json()["id"]
    second = client.post("/api/v1/projects", json={"name": "隔离项目"}, headers=headers).json()["id"]
    root = f"/api/v1/projects/{first}"
    doc = client.post(f"{root}/documents", files={"file": ("rules.md", b"# Rules")},
                      headers=headers).json()["id"]
    other = client.post(f"/api/v1/projects/{second}/documents",
                        files={"file": ("private.md", b"# private")}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add(AnalysisRun(id="analysis-link", project_id=first, user_id=1, source_ids=[doc],
                           status="ready", result={}, version=2))
        db.add(HarnessTask(id="task-link", project_id=first, user_id=1, task_type="test-cases",
                           skill_id="test-cases", result_schema="test-cases.v1", status="completed",
                           source_document_ids=[doc], analysis_run_id="analysis-link", idempotency_key="link"))
        db.add(CaseDraft(id="draft-link", project_id=first, analysis_id="analysis-link",
                         payload={"title": "待编写"}, status="draft"))
        db.commit()
    saved = client.post(f"{root}/knowledge", json={"source_type": "document", "source_id": doc,
                        "title": "人工结论", "body": "复核"}, headers=headers).json()
    document = client.get(f"{root}/artifacts/document/{doc}/relations")
    assert document.status_code == 200, document.text
    relations = document.json()
    assert relations["scope"] == "direct" and not relations["truncated"]
    assert {(edge["relation"], edge["to"]["id"]) for edge in relations["edges"]} == {
        ("analyzed_from", "analysis-link"), ("submitted_to", "task-link"),
        ("curated_as", str(saved["id"]))}
    analysis = client.get(f"{root}/artifacts/analysis/analysis-link/relations").json()
    assert {edge["relation"] for edge in analysis["edges"]} == {
        "analyzed_from", "generated_by", "drafted_from"}
    assert client.get(f"{root}/artifacts/knowledge/{saved['id']}/relations").json()["edges"][0]["from"]["id"] == doc
    assert client.get(f"{root}/artifacts/document/{other}/relations").status_code == 404
    assert client.get(f"/api/v1/projects/{second}/artifacts/knowledge/{saved['id']}/relations").status_code == 404
    assert client.get(f"{root}/artifacts/defect/invalid/relations").status_code == 422
    assert client.get(f"{root}/artifacts/document/{doc}/relations", headers={}).status_code == 200

    with SessionLocal() as db:
        db.delete(db.get(SourceDocument, doc))
        db.commit()
    archived_source = client.get(f"{root}/artifacts/knowledge/{saved['id']}/relations").json()
    assert archived_source["nodes"][-1]["missing"] is True
    assert archived_source["nodes"][-1]["title"] == "rules.md"
    assert client.get(f"{root}/artifacts/document/{doc}/relations").status_code == 404

    reader = client.post("/api/v1/users", json={"username": "graph-reader", "password": "reader-password-123",
                         "role": "reader"}, headers=headers).json()
    client.post(f"{root}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    login(client, "graph-reader", "reader-password-123")
    assert client.get(f"{root}/artifacts/knowledge/{saved['id']}/relations").status_code == 200
    assert client.get(f"/api/v1/projects/{second}/artifacts/document/{other}/relations").status_code == 403


def test_direct_reference_count_is_bounded_and_marked_truncated(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "关联上限"}, headers=headers).json()["id"]
    doc = client.post(f"/api/v1/projects/{project}/documents",
                      files={"file": ("bounded.md", b"# input")}, headers=headers).json()["id"]
    with SessionLocal() as db:
        for index in range(105):
            db.add(KnowledgeEntry(project_id=project, source_type="document", source_id=doc,
                                  source_snapshot={"title": "bounded.md", "version": 1},
                                  title=f"结论 {index}", body="人工结论", version=1,
                                  created_by=1, updated_by=1))
        db.add(AnalysisRun(id="analysis-after-knowledge", project_id=project, user_id=1,
                           source_ids=[doc], status="ready", result={}))
        db.commit()
    graph = client.get(f"/api/v1/projects/{project}/artifacts/document/{doc}/relations").json()
    assert graph["truncated"] is True
    assert len(graph["edges"]) == 100
    assert graph["next_offset"] == 100
    next_page = client.get(f"/api/v1/projects/{project}/artifacts/document/{doc}/relations?offset=100").json()
    assert len(next_page["edges"]) == 6
    assert next_page["next_offset"] is None and next_page["truncated"] is False
    assert next_page["edges"][-1]["to"]["id"] == "analysis-after-knowledge"
    assert not ({edge["to"]["id"] for edge in graph["edges"]} &
                {edge["to"]["id"] for edge in next_page["edges"]})
    assert client.get(f"/api/v1/projects/{project}/artifacts/document/{doc}/relations?offset=-1").status_code == 422


def test_document_references_beyond_legacy_scan_limit_remain_visible(client):
    headers = login(client)
    project = client.post("/api/v1/projects", json={"name": "大项目关联"}, headers=headers).json()["id"]
    doc = client.post(f"/api/v1/projects/{project}/documents",
                      files={"file": ("deep.md", b"# input")}, headers=headers).json()["id"]
    with SessionLocal() as db:
        db.add_all(AnalysisRun(id=f"empty-{index:04}", project_id=project, user_id=1,
                               source_ids=[], status="ready", result={}) for index in range(2001))
        db.add(AnalysisRun(id="last-source", project_id=project, user_id=1,
                           source_ids=[doc], status="ready", result={}))
        db.commit()
    root = f"/api/v1/projects/{project}/artifacts/document/{doc}/relations"
    graph = client.get(root).json()
    assert graph["truncated"] is False
    assert [(edge["relation"], edge["to"]["id"]) for edge in graph["edges"]] == [
        ("analyzed_from", "last-source")]


def test_report_defects_and_knowledge_retain_project_scoped_references(context):
    client, headers, root, cases = context
    case = review_yes(client, headers, root, cases[0])
    suite = create_suite(client, headers, root, ["登录"])
    suite = client.post(f"{root}/smoke-suites/{suite['id']}/items", json={
        "revision": suite["revision"], "case_id": case["id"], "chain_name": "登录", "estimated_seconds": 20},
        headers=headers).json()
    run = client.post(f"{root}/smoke-suites/{suite['id']}/runs", json={
        "suite_revision": suite["revision"]}, headers=headers).json()
    item = run["items"][0]
    client.put(f"{root}/execution-runs/{run['id']}/cases/{item['id']}", json={
        "version": 1, "result": "failed", "actual_seconds": 10, "note": "失败"}, headers=headers)
    assert client.post(f"{root}/execution-runs/{run['id']}/complete", headers=headers).status_code == 200
    defect = client.post(f"{root}/execution-defects", json={
        "run_id": run["id"], "run_case_id": item["id"], "title": "登录失败"}, headers=headers).json()
    knowledge = client.post(f"{root}/knowledge", json={
        "source_type": "execution_report", "source_id": str(run["id"]),
        "title": "失败原因", "body": "人工复核"}, headers=headers).json()
    graph = client.get(f"{root}/artifacts/execution_report/{run['id']}/relations").json()
    assert {edge["relation"] for edge in graph["edges"]} == {
        "curated_as", "records_defect", "completed_as", "contains_case"}
    assert any(node["id"] == str(defect["id"]) for node in graph["nodes"])
    frozen = client.get(f"{root}/artifacts/report_case/{run['id']}:{item['id']}/relations").json()
    assert frozen["center"]["original_id"] == case["original_id"]
    assert frozen["center"]["frozen"] is True
    assert {edge["relation"] for edge in frozen["edges"]} == {
        "contains_case", "frozen_as", "snapshot_of", "found_defect"}
    assert client.delete(f"{root}/cases/{case['id']}", headers=headers).status_code == 200
    assert client.get(f"{root}/artifacts/execution_report/{run['id']}/relations").status_code == 200
    other = client.post("/api/v1/projects", json={"name": "其他图谱"}, headers=headers).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/artifacts/report_case/{run['id']}:{item['id']}/relations").status_code == 404
    assert client.get(f"/api/v1/projects/{other}/artifacts/run_case/{item['id']}/relations").status_code == 404
    assert client.get(f"{root}/artifacts/report_case/{run['id']}:invalid/relations").status_code == 422
    frozen_after = client.get(f"{root}/artifacts/report_case/{run['id']}:{item['id']}/relations").json()
    assert frozen_after["center"]["original_id"] == case["original_id"]
    assert "snapshot_of" not in {edge["relation"] for edge in frozen_after["edges"]}
    assert client.get(f"{root}/artifacts/knowledge/{knowledge['id']}/relations").json()["edges"][0]["relation"] == "curated_as"


def test_catalog_paginates_and_does_not_expose_other_project(client):
    headers = login(client)
    first = client.post("/api/v1/projects", json={"name": "浏览项目"}, headers=headers).json()["id"]
    second = client.post("/api/v1/projects", json={"name": "别的项目"}, headers=headers).json()["id"]
    with SessionLocal() as db:
        for number in range(45):
            db.add(AnalysisRun(id=f"analysis-{number:03}", project_id=first, user_id=1,
                               source_ids=[], status="ready", result={}))
        db.add(AnalysisRun(id="analysis-foreign", project_id=second, user_id=1,
                           source_ids=[], status="ready", result={}))
        db.commit()
    root = f"/api/v1/projects/{first}/artifacts"
    first_page = client.get(f"{root}?kind=analysis").json()
    assert len(first_page["items"]) == 40
    assert first_page["next_before_id"] is not None
    second_page = client.get(f"{root}?kind=analysis&before_id={first_page['next_before_id']}").json()
    assert len(second_page["items"]) == 5
    assert second_page["next_before_id"] is None
    assert len({item["id"] for item in first_page["items"] + second_page["items"]}) == 45
    assert "analysis-foreign" not in {item["id"] for item in first_page["items"] + second_page["items"]}
    assert client.get(f"{root}?kind=report_case").status_code == 422
    assert client.get(f"{root}?kind=analysis&before_id={'x' * 129}").status_code == 422

    reader = client.post("/api/v1/users", json={"username": "artifact-reader", "password": "reader-password-123",
                         "role": "reader"}, headers=headers).json()
    client.post(f"/api/v1/projects/{first}/members", json={"user_id": reader["id"]}, headers=headers)
    client.post("/api/v1/auth/logout", headers=headers)
    login(client, "artifact-reader", "reader-password-123")
    assert client.get(f"{root}?kind=analysis").status_code == 200
    assert client.get(f"/api/v1/projects/{second}/artifacts?kind=analysis").status_code == 403
