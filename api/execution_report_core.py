"""Deterministic report payloads built only from execution-time snapshots."""

import hashlib
import json
from collections.abc import Mapping

RESULTS = ("not_run", "passed", "failed", "blocked", "skipped")
BUDGET_SECONDS = 30 * 60


def _value(row, key):
    return row[key] if isinstance(row, Mapping) else getattr(row, key)


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def report_integrity_ok(snapshot) -> bool:
    report = snapshot.payload
    if not isinstance(report, dict):
        return False
    try:
        digest = hashlib.sha256(canonical_json({key: value for key, value in report.items()
                                                if key != "integrity"})).hexdigest()
        summary = report["summary"]
        results = summary["results"]
        return (digest == snapshot.payload_sha256 == report["integrity"]["payload_sha256"]
                and report["source"]["project_id"] == snapshot.project_id
                and report["source"]["run_id"] == snapshot.run_id
                and all(results[key] == getattr(snapshot, f"{key}_count") for key in RESULTS)
                and summary["case_count"] == snapshot.case_executions == sum(results[key] for key in RESULTS)
                and summary["case_count"] == len(report["cases"])
                and summary["estimated_seconds"] == snapshot.estimated_seconds
                and summary["actual_seconds"] == snapshot.actual_seconds)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def build_execution_report(run, rows) -> dict:
    """Never inspect the current Case or SmokeSuite while building a report."""
    rows = list(rows)
    if _value(run, "status") != "completed" or not _value(run, "completed_at") or not rows:
        raise ValueError("Completed run with cases required")
    if any(_value(row, "result") not in RESULTS[1:] for row in rows):
        raise ValueError("Completed run contains an unfinished or unknown result")
    counts = {key: sum(_value(row, "result") == key for row in rows) for key in RESULTS}
    chains = [{"name": name, "case_count": sum(_value(row, "chain_name") == name for row in rows),
               "results": {key: sum(_value(row, "chain_name") == name and _value(row, "result") == key for row in rows)
                           for key in RESULTS}} for name in _value(run, "required_chains")]
    report = {
        "schema_version": "execution-report.v1",
        "source": {"project_id": _value(run, "project_id"), "run_id": _value(run, "id"),
                   "suite_id": _value(run, "suite_id"), "suite_revision": _value(run, "suite_revision"),
                   "title": _value(run, "title"), "target_version": _value(run, "target_version"),
                   "environment": _value(run, "environment"), "started_by_id": _value(run, "started_by"),
                   "started_at": _value(run, "started_at"), "completed_by_id": _value(run, "completed_by"),
                   "completed_at": _value(run, "completed_at")},
        "scope": "开始执行时冻结用例标识、标题、等级、冒烟值、链路和预计时长；结束执行时冻结结果、实际时长与说明。不包含用例步骤、预期结果或缺陷关联",
        "summary": {"case_count": len(rows), "results": counts,
                    "estimated_seconds": sum(_value(row, "estimated_seconds") for row in rows),
                    "actual_seconds": sum(_value(row, "actual_seconds") or 0 for row in rows),
                    "budget_seconds": BUDGET_SECONDS,
                    "uncovered_chains": [chain["name"] for chain in chains if chain["case_count"] == 0]},
        "chains": chains,
        "cases": [{"run_case_id": _value(row, "id"), "case_id": _value(row, "case_id"),
                   "case_version": _value(row, "case_version"), "original_id": _value(row, "original_id"),
                   "title": _value(row, "title"), "level": _value(row, "level"), "smoke": _value(row, "smoke"),
                   "chain_name": _value(row, "chain_name"), "estimated_seconds": _value(row, "estimated_seconds"),
                   "result": _value(row, "result"), "actual_seconds": _value(row, "actual_seconds"),
                   "note": _value(row, "note"), "executed_by_id": _value(row, "executed_by"),
                   "executed_at": _value(row, "executed_at"), "result_version": _value(row, "version")}
                  for row in rows],
    }
    report["integrity"] = {"algorithm": "sha256", "canonicalization": "utf8-json-sort-keys-compact",
                           "payload_sha256": hashlib.sha256(canonical_json(report)).hexdigest()}
    return report
