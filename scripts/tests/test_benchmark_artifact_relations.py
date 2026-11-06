"""The synthetic benchmark must stay isolated and exercise deep pagination."""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_artifact_relations_runs_against_disposable_sqlite():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_artifact_relations.py")],
                            cwd=ROOT, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert "synthetic SQLite" in payload["dataset"]
    assert payload["measurements"]["directory_catalog"]["count"] == 40
    assert payload["measurements"]["import_relations_deep"]["count"] == 49
    for mode in ("live", "verified"):
        for page in ("first", "deep"):
            assert payload["measurements"][f"case_catalog_{mode}_{page}"]["count"] == 40


def test_project_snapshot_covers_the_synthetic_project_without_partial_edges():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_artifact_relations.py"),
                             "--snapshot-probe"], cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-1000:]
    snapshot = json.loads(result.stdout.splitlines()[-1])["measurements"]["project_snapshot_candidate"]
    assert snapshot["status"] == 200
    assert snapshot["node_count"] == 3785
    assert snapshot["edge_count"] == 7232
    assert snapshot["bytes"] <= 16 * 1024 * 1024
