#!/usr/bin/env python3
"""Measure project artifact reads with a disposable, synthetic SQLite project."""

import json
import argparse
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-probe", action="store_true",
                        help="measure a larger candidate project snapshot on synthetic data")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="test-atelier-artifact-benchmark-") as directory:
        root = Path(directory)
        password = secrets.token_urlsafe(24)
        os.environ.update({
            "DATABASE_URL": f"sqlite:///{root / 'benchmark.db'}",
            "AI_STUDIO_DATABASE_PASSWORD_FILE": "",
            "POSTGRES_PASSWORD": "",
            "AI_STUDIO_DATA_DIR": str(root / "data"),
            "AI_STUDIO_CONFIG_FILE": str(root / "data" / "config" / "settings.json"),
            "AI_STUDIO_SESSION_SECRET": secrets.token_urlsafe(48),
            "AI_STUDIO_SESSION_SECRET_FILE": "",
            "AI_STUDIO_ADMIN_PASSWORD": password,
            "AI_STUDIO_ADMIN_PASSWORD_FILE": "",
            "AI_STUDIO_ENV": "development",
            "AI_STUDIO_REQUIRE_MIGRATIONS": "0",
            "TEST_ATELIER_HARNESS_URL": "",
        })
        from fastapi.testclient import TestClient
        from sqlalchemy import insert, select

        from api.db import (Case, GovernanceChangeLog, GovernanceDirectory,
                            ImportBatch, SessionLocal)
        from api.main import app

        with TestClient(app) as client:
            csrf = client.get("/api/v1/auth/csrf").json()["token"]
            logged = client.post("/api/v1/auth/login", json={"username": "admin", "password": password},
                                 headers={"x-csrf-token": csrf})
            logged.raise_for_status()
            headers = {"x-csrf-token": logged.json()["csrf"]}
            project = client.post("/api/v1/projects", json={"name": "synthetic-directory-benchmark"},
                                  headers=headers)
            project.raise_for_status()
            project_id = project.json()["id"]
            paths = [f"业务/能力-{index:03d}" for index in range(229)]
            with SessionLocal() as db:
                batch = ImportBatch(project_id=project_id, filename="synthetic.xlsx", headers=[],
                                    rows=[], warnings=[], status="done")
                db.add(batch)
                db.flush()
                db.execute(insert(GovernanceDirectory), [
                    {"project_id": project_id, "path": "业务", "parent": "", "level": 0},
                    *({"project_id": project_id, "path": path, "parent": "业务", "level": 1}
                      for path in paths),
                ])
                db.execute(insert(Case), [
                    {"project_id": project_id, "import_id": batch.id, "original_id": str(index),
                     "title": f"合成用例 {index}", "directory": paths[0 if index < 120 else index % len(paths)],
                     "original": []}
                    for index in range(3449)
                ])
                db.execute(insert(GovernanceChangeLog), [
                    {"project_id": project_id, "entity": "directory", "entity_id": paths[0],
                     "action": "whitelist", "before_json": {}, "after_json": {"index": index},
                     "batch_key": "synthetic", "operator_id": logged.json()["id"]}
                    for index in range(105)
                ])
                deep_cursor = db.scalar(select(Case.id).where(Case.project_id == project_id)
                                        .order_by(Case.id.desc()).offset(3399).limit(1))
                batch_id = batch.id
                db.commit()

            from api.artifact_routes import _directory_id
            base = f"/api/v1/projects/{project_id}/artifacts"
            requests = {
                "directory_catalog": (f"{base}?kind=governance_directory", 40),
                "directory_relations_first": (f"{base}/governance_directory/"
                                              f"{_directory_id(paths[0])}/relations", 100),
                "directory_relations_next": (f"{base}/governance_directory/"
                                             f"{_directory_id(paths[0])}/relations?offset=100", 100),
                "import_relations_first": (f"{base}/import/{batch_id}/relations", 100),
                "import_relations_deep": (f"{base}/import/{batch_id}/relations?offset=3400", 49),
            }
            measured = {}
            for name, (url, minimum) in requests.items():
                started = time.perf_counter()
                response = client.get(url)
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                response.raise_for_status()
                result = response.json()
                count = len(result.get("items", result.get("edges", [])))
                if count < minimum:
                    raise RuntimeError(f"{name} returned {count} artifacts, expected at least {minimum}")
                measured[name] = {"milliseconds": duration_ms, "count": count}
            verified_url = f"{base}/import/{batch_id}/relations?consistency=verified"
            started = time.perf_counter()
            first = client.get(verified_url)
            first.raise_for_status()
            measured["import_verified_first"] = {
                "milliseconds": round((time.perf_counter() - started) * 1000, 2),
                "count": len(first.json()["edges"]),
            }
            started = time.perf_counter()
            last = client.get(f"{verified_url}&offset=3400&revision={first.json()['revision']}")
            last.raise_for_status()
            measured["import_verified_deep"] = {
                "milliseconds": round((time.perf_counter() - started) * 1000, 2),
                "count": len(last.json()["edges"]),
            }
            catalog_url = f"{base}?kind=case"
            for name, url in (("case_catalog_live_first", catalog_url),
                              ("case_catalog_live_deep", f"{catalog_url}&before_id={deep_cursor}"),
                              ("case_catalog_verified_first", f"{catalog_url}&consistency=verified")):
                started = time.perf_counter()
                response = client.get(url)
                response.raise_for_status()
                measured[name] = {"milliseconds": round((time.perf_counter() - started) * 1000, 2),
                                  "count": len(response.json()["items"])}
                if len(response.json()["items"]) != 40:
                    raise RuntimeError(f"{name} did not return a full page")
                if name == "case_catalog_verified_first":
                    catalog_revision = response.json()["revision"]
            started = time.perf_counter()
            deep = client.get(f"{catalog_url}&consistency=verified&before_id={deep_cursor}"
                              f"&revision={catalog_revision}")
            deep.raise_for_status()
            measured["case_catalog_verified_deep"] = {
                "milliseconds": round((time.perf_counter() - started) * 1000, 2),
                "count": len(deep.json()["items"]),
            }
            if len(deep.json()["items"]) != 40:
                raise RuntimeError("case_catalog_verified_deep did not return a full page")
            if args.snapshot_probe:
                started = time.perf_counter()
                response = client.get(f"/api/v1/projects/{project_id}/artifacts/snapshot")
                measured["project_snapshot_candidate"] = {
                    "milliseconds": round((time.perf_counter() - started) * 1000, 2),
                    "status": response.status_code,
                    "node_count": response.json().get("node_count"),
                    "edge_count": response.json().get("edge_count"),
                    "bytes": len(response.content),
                }
            print(json.dumps({"dataset": "synthetic SQLite, 3449 cases, 230 directories, 105 changes",
                              "measurements": measured}, ensure_ascii=False))


if __name__ == "__main__":
    main()
