#!/usr/bin/env python3
"""Score a human-reviewed Harness analysis/case run without printing source text."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

MAX_RESULT_FILE_BYTES = 128 * 1024 * 1024
MAX_REVIEW_FILE_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
REPORT_DIMENSIONS = (
    "requirement_completeness", "module_coverage", "acceptance_testability",
    "rules_and_boundaries", "method_planning", "risk_categories",
    "traceability", "source_grounding", "multi_document_coverage",
    "multi_document_deduplication", "usability_review",
)


class ReviewError(ValueError):
    """An invalid review or result; callers must not print the original input."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError("Duplicate JSON key")
        result[key] = value
    return result


def _read_bounded(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ReviewError("Input must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        metadata = os.fstat(source.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_ctime_ns)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit
                or identity(before) != identity(metadata)):
            raise ReviewError("Input must be a bounded regular file")
        raw = source.read(limit + 1)
        if len(raw) > limit:
            raise ReviewError("Input exceeded the bounded review size")
        after = os.fstat(source.fileno())
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode) or identity(current) != identity(metadata)
                or identity(after) != identity(metadata) or after.st_size != metadata.st_size
                or after.st_mtime_ns != metadata.st_mtime_ns or len(raw) != metadata.st_size):
            raise ReviewError("Input changed while the quality review was reading it")
        return raw


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise ReviewError("Expected a JSON object")
    return value


def _ids(items: object, *, allow_empty: bool = False) -> set[str]:
    if not isinstance(items, list) or (not allow_empty and not items):
        raise ReviewError("Expected a non-empty ID list")
    values = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > 128:
            raise ReviewError("Invalid ID")
        values.append(item)
    if len(values) != len(set(values)):
        raise ReviewError("Duplicate ID")
    return set(values)


def _result(value: object, schema: str) -> dict:
    wrapped = _object(value)
    raw = _object(wrapped.get("result"))
    if (wrapped.get("status") != "completed" or wrapped.get("result_schema") != schema
            or wrapped.get("task_id") != raw.get("task_id")):
        raise ReviewError("Only a completed, correctly bound task result can be reviewed")
    if raw.get("schema") != schema or not isinstance(raw.get("data"), dict):
        raise ReviewError("Result schema does not match the requested run")
    if (type(wrapped.get("project_id")) is not int or wrapped["project_id"] < 1
            or not isinstance(wrapped.get("analysis_run_id"), str) or not wrapped["analysis_run_id"]
            or _ids(wrapped.get("source_document_ids")) != _ids(raw.get("source_document_ids"))):
        raise ReviewError("Task result is missing its authorized project, analysis or source binding")
    if schema == "test-cases.v1" and wrapped["analysis_run_id"] != raw.get("analysis_run_id"):
        raise ReviewError("Case result does not match its authorized analysis run")
    return raw


def _review_rows(rows: object, observed: set[str], *, allow_empty: bool = False) -> list[dict]:
    if not isinstance(rows, list) or (not allow_empty and not rows):
        raise ReviewError("Review rows are missing")
    gold_ids: set[str] = set()
    checked = []
    for raw in rows:
        row = _object(raw)
        gold_id = row.get("id")
        if not isinstance(gold_id, str) or not gold_id or len(gold_id) > 128 or gold_id in gold_ids:
            raise ReviewError("Gold IDs must be unique and non-empty")
        gold_ids.add(gold_id)
        verdict = row.get("verdict")
        if verdict not in {"correct", "partial", "missing"}:
            raise ReviewError("Each gold item needs an explicit review verdict")
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240:
            raise ReviewError("Each gold item needs a bounded reviewer evidence locator")
        matches = _ids(row.get("observed_ids"), allow_empty=True)
        if not matches.issubset(observed) or (verdict == "missing") != (not matches):
            raise ReviewError("Reviewed IDs or missing verdict do not match the result")
        if not isinstance(row.get("critical"), bool):
            raise ReviewError("Each gold item must declare its criticality")
        checked.append(row)
    return checked


def _ratio(rows: list[dict]) -> float:
    return sum(row["verdict"] == "correct" for row in rows) / len(rows) if rows else 1.0


def _additional(items: object, observed: set[str]) -> set[str]:
    if not isinstance(items, list):
        raise ReviewError("Additional model outputs need an explicit review list")
    ids: set[str] = set()
    for value in items:
        item = _object(value)
        identifier = item.get("id")
        evidence = item.get("evidence")
        if (not isinstance(identifier, str) or identifier not in observed or identifier in ids
                or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240):
            raise ReviewError("Additional output needs a valid ID and evidence locator")
        ids.add(identifier)
    return ids


def _complete_review(rows: list[dict], additional: set[str], rejected: set[str], observed: set[str]) -> None:
    matched = {identifier for row in rows for identifier in row["observed_ids"]}
    if matched & additional or matched & rejected or additional & rejected or matched | additional | rejected != observed:
        raise ReviewError("Every model output must receive one consistent reviewer classification")


def _review_report(analysis: dict, review: dict) -> tuple[int, int]:
    report = analysis.get("report_markdown")
    if not isinstance(report, str) or not report.lstrip().startswith("# "):
        raise ReviewError("The completed analysis has no original Markdown report")
    encoded = report.encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ReviewError("The original Markdown report exceeds its size limit")
    checklist = _object(review.get("report_review"))
    if checklist.get("sha256") != hashlib.sha256(encoded).hexdigest():
        raise ReviewError("The human review is not bound to this original report")
    dimensions = checklist.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != len(REPORT_DIMENSIONS):
        raise ReviewError("Every report quality dimension requires a human verdict")
    reviewed = {}
    for raw in dimensions:
        item = _object(raw)
        name = item.get("name")
        if (not isinstance(name, str) or name not in REPORT_DIMENSIONS or name in reviewed
                or not isinstance(item.get("verdict"), str) or item["verdict"] not in {"pass", "fail"}):
            raise ReviewError("Report quality verdict is invalid or repeated")
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240:
            raise ReviewError("Every report quality verdict needs a bounded evidence locator")
        reviewed[name] = item["verdict"]
    if set(reviewed) != set(REPORT_DIMENSIONS):
        raise ReviewError("The report quality checklist is incomplete")
    sources = checklist.get("source_coverage")
    if not isinstance(sources, list) or len(sources) != len(analysis["source_document_ids"]):
        raise ReviewError("Every source document needs report coverage review")
    covered = {}
    for raw in sources:
        item = _object(raw)
        source_id = item.get("document_id")
        if (not isinstance(source_id, str) or source_id in covered
                or not isinstance(item.get("verdict"), str) or item["verdict"] not in {"covered", "missing"}):
            raise ReviewError("Report source review is invalid or repeated")
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240:
            raise ReviewError("Every source coverage verdict needs a bounded evidence locator")
        covered[source_id] = item["verdict"]
    if set(covered) != set(analysis["source_document_ids"]):
        raise ReviewError("Report source review does not match the task documents")
    return sum(verdict != "pass" for verdict in reviewed.values()), sum(
        verdict != "covered" for verdict in covered.values())


def _check_acceptance_traceability(analysis_data: dict, cases_data: dict,
                                   requirements: list[dict], criteria: list[dict]) -> None:
    reviewed_requirements = {row["id"]: set(row["observed_ids"]) for row in requirements}
    model_criteria: dict[str, set[str]] = {}
    for raw in analysis_data.get("requirements", []):
        requirement = _object(raw)
        requirement_id = requirement["id"]
        model_criteria[requirement_id] = _ids(
            [_object(item).get("id") for item in requirement.get("acceptance_criteria", [])], allow_empty=True,
        )
    model_cases = {_object(item)["id"]: _object(item) for item in cases_data.get("cases", [])}
    for case in model_cases.values():
        traces = case.get("traceability")
        if not isinstance(traces, list) or not traces or any(
            not isinstance(trace, dict) or not isinstance(trace.get("requirement_id"), str)
            or trace["requirement_id"] not in model_criteria
            or (trace.get("acceptance_criterion_id") is not None
                and (not isinstance(trace["acceptance_criterion_id"], str)
                     or trace["acceptance_criterion_id"] not in model_criteria[trace["requirement_id"]]))
            for trace in traces
        ):
            raise ReviewError("Model case traceability is not bound to the reviewed analysis")
    for row in criteria:
        gold_requirement_id = row.get("requirement_gold_id")
        if not isinstance(gold_requirement_id, str) or gold_requirement_id not in reviewed_requirements:
            raise ReviewError("Gold acceptance criterion must reference a reviewed Gold requirement")
        expected_requirements = reviewed_requirements[gold_requirement_id]
        observed_criteria = _ids(row.get("observed_criterion_ids"), allow_empty=True)
        available_criteria = set().union(*(model_criteria.get(identifier, set()) for identifier in expected_requirements))
        if not observed_criteria.issubset(available_criteria):
            raise ReviewError("Reviewed criterion does not belong to its observed requirement")
        if row["verdict"] == "correct" and not observed_criteria:
            raise ReviewError("A correct acceptance criterion needs a matching model criterion")
        for case_id in row["observed_ids"]:
            case = model_cases[case_id]
            traces = case.get("traceability")
            if not isinstance(traces, list) or not any(
                isinstance(trace, dict) and trace.get("requirement_id") in expected_requirements
                and (row["verdict"] == "partial" or (
                    trace.get("acceptance_criterion_id") in observed_criteria
                    and trace["acceptance_criterion_id"] in model_criteria[trace["requirement_id"]]
                ))
                for trace in traces
            ):
                raise ReviewError("Reviewed case does not trace to its Gold acceptance criterion")
        if row["verdict"] == "correct":
            covered_criteria = {
                trace["acceptance_criterion_id"]
                for case_id in row["observed_ids"]
                for trace in model_cases[case_id]["traceability"]
                if trace["requirement_id"] in expected_requirements
                and trace.get("acceptance_criterion_id") in model_criteria[trace["requirement_id"]]
            }
            if not observed_criteria.issubset(covered_criteria):
                raise ReviewError("Every reviewed model criterion needs a corresponding case trace")


def evaluate(analysis_value: object, cases_value: object, review_value: object) -> dict:
    analysis = _result(analysis_value, "requirement-analysis.v1")
    cases = _result(cases_value, "test-cases.v1")
    analysis_task, cases_task = _object(analysis_value), _object(cases_value)
    review = _object(review_value)
    if review.get("schema") != "harness-quality-review.v3":
        raise ReviewError("Unknown review schema")
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 120:
        raise ReviewError("A reviewer is required")
    execution = _object(review.get("execution"))
    if execution.get("platform") not in {"darwin", "win32", "linux"}:
        raise ReviewError("Review platform is outside the supported release matrix")
    for name in ("harness_sha", "extension_sha"):
        value = execution.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ReviewError("Both reviewed source commits must be complete Git SHA values")
    if execution.get("profile") not in {"test-atelier", "test-atelier-acp"}:
        raise ReviewError("Review must identify the installed Harness Profile")
    model = execution.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 120:
        raise ReviewError("Review must identify the configured model")
    if (review.get("analysis_task_id") != analysis.get("task_id")
            or review.get("cases_task_id") != cases.get("task_id")
            or analysis_task["project_id"] != cases_task["project_id"]
            or analysis_task["analysis_run_id"] != cases_task["analysis_run_id"]
            or cases.get("analysis_run_id") != review.get("analysis_run_id")
            or analysis_task["analysis_run_id"] != review.get("analysis_run_id")):
        raise ReviewError("Review does not bind the two original task results")
    if _ids(analysis.get("source_document_ids")) != _ids(cases.get("source_document_ids")):
        raise ReviewError("Analysis and cases have different source documents")
    failed_report_dimensions, uncovered_report_sources = _review_report(analysis, review)
    analysis_data, cases_data = analysis["data"], cases["data"]
    requirements = _ids([_object(item).get("id") for item in analysis_data.get("requirements", [])], allow_empty=True)
    ambiguities = _ids([_object(item).get("id") for item in analysis_data.get("ambiguities", [])], allow_empty=True)
    case_ids = _ids([_object(item).get("id") for item in cases_data.get("cases", [])], allow_empty=True)
    gold = _object(review.get("gold"))
    reviewed_requirements = _review_rows(gold.get("requirements"), requirements)
    reviewed_ambiguities = _review_rows(gold.get("blocking_ambiguities"), ambiguities, allow_empty=True)
    reviewed_cases = _review_rows(gold.get("acceptance_criteria"), case_ids)
    _check_acceptance_traceability(analysis_data, cases_data, reviewed_requirements, reviewed_cases)
    if any(not row["critical"] for row in reviewed_ambiguities):
        raise ReviewError("Blocking ambiguity review entries must be critical")
    unsupported = _ids(review.get("unsupported_requirement_ids"), allow_empty=True)
    unsupported_ambiguities = _ids(review.get("unsupported_ambiguity_ids"), allow_empty=True)
    unusable = _ids(review.get("unusable_case_ids"), allow_empty=True)
    if not unsupported.issubset(requirements) or not unsupported_ambiguities.issubset(ambiguities) or not unusable.issubset(case_ids):
        raise ReviewError("Unsupported or unusable IDs are absent from the model result")
    _complete_review(reviewed_requirements,
                     _additional(review.get("additional_supported_requirements"), requirements), unsupported, requirements)
    _complete_review(reviewed_ambiguities,
                     _additional(review.get("additional_ambiguities"), ambiguities), unsupported_ambiguities, ambiguities)
    _complete_review(reviewed_cases,
                     _additional(review.get("additional_usable_cases"), case_ids), unusable, case_ids)
    observed_ambiguities = {item["id"]: item for item in analysis_data.get("ambiguities", [])}
    for row in reviewed_ambiguities:
        if row["verdict"] == "correct" and not all(
            observed_ambiguities[identifier].get("severity") == "blocking"
            and observed_ambiguities[identifier].get("resolution") is None
            for identifier in row["observed_ids"]
        ):
            raise ReviewError("A correct blocking ambiguity must remain blocking and unresolved")
    limits = _object(review.get("limits"))
    for key in ("requirements_min", "acceptance_min"):
        if type(limits.get(key)) not in (int, float) or not 0 < limits[key] <= 1:
            raise ReviewError("Review thresholds must be explicit positive ratios at most one")
    requirement_ratio = _ratio(reviewed_requirements)
    acceptance_ratio = _ratio(reviewed_cases)
    missing_critical = sum(row["critical"] and row["verdict"] != "correct" for row in reviewed_requirements)
    missing_blocking = sum(row["verdict"] != "correct" for row in reviewed_ambiguities)
    failures = []
    if requirement_ratio < limits["requirements_min"]:
        failures.append("requirement_recall_below_threshold")
    if acceptance_ratio < limits["acceptance_min"]:
        failures.append("acceptance_coverage_below_threshold")
    if missing_critical:
        failures.append("critical_requirement_not_correct")
    if missing_blocking:
        failures.append("blocking_ambiguity_not_correct")
    if unsupported:
        failures.append("unsupported_requirements")
    if unsupported_ambiguities:
        failures.append("unsupported_ambiguities")
    if unusable:
        failures.append("unusable_cases")
    if failed_report_dimensions:
        failures.append("report_quality_review_failed")
    if uncovered_report_sources:
        failures.append("report_source_coverage_missing")
    return {
        "status": "pass" if not failures else "fail", "failures": failures,
        "requirements": {"reviewed": len(reviewed_requirements), "correct_ratio": round(requirement_ratio, 4),
                         "critical_not_correct": missing_critical},
        "blocking_ambiguities": {"reviewed": len(reviewed_ambiguities), "not_correct": missing_blocking},
        "acceptance_criteria": {"reviewed": len(reviewed_cases), "correct_ratio": round(acceptance_ratio, 4)},
        "unsupported_requirement_count": len(unsupported),
        "unsupported_ambiguity_count": len(unsupported_ambiguities),
        "unusable_case_count": len(unusable),
        "report": {"quality_dimensions_failed": failed_report_dimensions,
                   "sources_uncovered": uncovered_report_sources},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        content = [_read_bounded(args.analysis, MAX_RESULT_FILE_BYTES),
                   _read_bounded(args.cases, MAX_RESULT_FILE_BYTES),
                   _read_bounded(args.review, MAX_REVIEW_FILE_BYTES)]
        report = evaluate(*(json.loads(raw, object_pairs_hook=_unique_pairs) for raw in content))
        report["input_sha256"] = [hashlib.sha256(raw).hexdigest() for raw in content]
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        print("Harness quality review input is invalid; no source material was printed", file=sys.stderr)
        return 78
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
