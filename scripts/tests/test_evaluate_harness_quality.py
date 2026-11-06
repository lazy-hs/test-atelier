import json
import os
import hashlib
from copy import deepcopy

import pytest

from scripts.evaluate_harness_quality import (MAX_RESULT_FILE_BYTES, MAX_REVIEW_FILE_BYTES, REPORT_DIMENSIONS,
                                              ReviewError, _read_bounded, evaluate, main)


def test_review_input_rejects_a_replaced_file_even_when_the_new_file_is_regular(tmp_path, monkeypatch):
    source = tmp_path / "review.json"
    replacement = tmp_path / "replacement.json"
    source.write_bytes(b"original")
    replacement.write_bytes(b"switched")
    actual_open = os.open
    changed = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal changed
        if not changed and str(path) == str(source):
            changed = True
            source.unlink()
            replacement.rename(source)
        return actual_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", swap_before_open)
        with pytest.raises(ReviewError, match="bounded regular file"):
            _read_bounded(source, 16)


def test_review_input_rejects_a_same_size_in_place_change_during_read(tmp_path, monkeypatch):
    source = tmp_path / "review.json"
    source.write_bytes(b"original")
    actual_fstat = os.fstat
    changed = False

    def change_after_first_stat(fd):
        nonlocal changed
        metadata = actual_fstat(fd)
        if not changed:
            changed = True
            source.write_bytes(b"switched")
            os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
        return metadata

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", change_after_first_stat)
        with pytest.raises(ReviewError, match="changed while"):
            _read_bounded(source, 16)


def sample():
    analysis = {
        "schema": "requirement-analysis.v1", "task_id": "analysis-task", "source_document_ids": ["doc-1"],
        "report_markdown": "# Analysis\n\nSynthetic report body\n",
        "data": {
            "requirements": [{"id": "REQ-1", "description": "private original material",
                              "acceptance_criteria": [{"id": "AC-1"}]}],
            "ambiguities": [{"id": "AMB-1", "severity": "blocking", "resolution": None}],
        },
    }
    cases = {
        "schema": "test-cases.v1", "task_id": "cases-task", "analysis_run_id": "run-1",
        "source_document_ids": ["doc-1"], "data": {"cases": [{"id": "TC-1", "title": "private case",
            "traceability": [{"requirement_id": "REQ-1", "acceptance_criterion_id": "AC-1"}]}]},
    }
    review = {
        "schema": "harness-quality-review.v3", "reviewer": "qa-reviewer",
        "execution": {"platform": "darwin", "harness_sha": "a" * 40, "extension_sha": "b" * 40,
                      "profile": "test-atelier", "model": "synthetic-test-model"},
        "analysis_task_id": "analysis-task", "cases_task_id": "cases-task", "analysis_run_id": "run-1",
        "gold": {
            "requirements": [{"id": "G-REQ-1", "critical": True, "observed_ids": ["REQ-1"],
                              "verdict": "correct", "evidence": "spec:section-1"}],
            "blocking_ambiguities": [{"id": "G-AMB-1", "critical": True, "observed_ids": ["AMB-1"],
                                      "verdict": "correct", "evidence": "spec:section-2"}],
            "acceptance_criteria": [{"id": "G-AC-1", "critical": True, "observed_ids": ["TC-1"],
                                     "requirement_gold_id": "G-REQ-1", "observed_criterion_ids": ["AC-1"],
                                     "verdict": "correct", "evidence": "spec:section-3"}],
        },
        "additional_supported_requirements": [], "additional_ambiguities": [], "additional_usable_cases": [],
        "unsupported_requirement_ids": [], "unsupported_ambiguity_ids": [], "unusable_case_ids": [],
        "limits": {"requirements_min": .9, "acceptance_min": .9},
        "report_review": {
            "sha256": hashlib.sha256(analysis["report_markdown"].encode()).hexdigest(),
            "dimensions": [{"name": name, "verdict": "pass", "evidence": "report:section-1"}
                           for name in REPORT_DIMENSIONS],
            "source_coverage": [{"document_id": "doc-1", "verdict": "covered",
                                 "evidence": "report:source-coverage"}],
        },
    }
    return analysis, cases, review


def api_result(result):
    return {"result": result, "status": "completed", "result_schema": result["schema"],
            "task_id": result["task_id"], "project_id": 1, "analysis_run_id": "run-1",
            "source_document_ids": result["source_document_ids"]}


def test_complete_human_review_passes_only_completed_api_results():
    analysis, cases, review = sample()
    report = evaluate(api_result(analysis), api_result(cases), review)
    assert report["status"] == "pass" and report["failures"] == []
    assert report["requirements"]["correct_ratio"] == 1
    assert report["acceptance_criteria"]["reviewed"] == 1
    assert report["report"] == {"quality_dimensions_failed": 0, "sources_uncovered": 0}


@pytest.mark.parametrize("change", [
    lambda a, c, r: a.pop("report_markdown"),
    lambda a, c, r: a.update(report_markdown="# Changed report"),
    lambda a, c, r: a.update(report_markdown="Not Markdown"),
    lambda a, c, r: a.update(report_markdown="# Report\n" + "中" * 350_000),
    lambda a, c, r: r.update(schema="harness-quality-review.v2"),
    lambda a, c, r: r["report_review"]["dimensions"].pop(),
    lambda a, c, r: r["report_review"]["dimensions"][0].update(name="unapproved_dimension"),
    lambda a, c, r: r["report_review"]["dimensions"][0].update(verdict=[]),
    lambda a, c, r: r["report_review"]["dimensions"][0].update(evidence=""),
    lambda a, c, r: r["report_review"]["source_coverage"][0].update(document_id="foreign-document"),
])
def test_report_release_review_rejects_missing_mismatched_or_unreviewed_original(change):
    analysis, cases, review = sample()
    change(analysis, cases, review)
    with pytest.raises(ReviewError):
        evaluate(api_result(analysis), api_result(cases), review)


def test_report_release_review_fails_on_report_quality_or_source_coverage():
    analysis, cases, review = sample()
    review["report_review"]["dimensions"][0]["verdict"] = "fail"
    review["report_review"]["source_coverage"][0]["verdict"] = "missing"
    result = evaluate(api_result(analysis), api_result(cases), review)
    assert result["status"] == "fail"
    assert set(result["failures"]) == {"report_quality_review_failed", "report_source_coverage_missing"}
    assert result["report"] == {"quality_dimensions_failed": 1, "sources_uncovered": 1}


def test_report_review_accounts_for_each_document_in_one_consolidated_report():
    analysis, cases, review = sample()
    analysis["source_document_ids"].append("doc-2")
    cases["source_document_ids"].append("doc-2")
    with pytest.raises(ReviewError, match="Every source document"):
        evaluate(api_result(analysis), api_result(cases), review)
    review["report_review"]["source_coverage"].append({
        "document_id": "doc-2", "verdict": "missing", "evidence": "report:missing-section",
    })
    assert evaluate(api_result(analysis), api_result(cases), review)["failures"] == ["report_source_coverage_missing"]
    review["report_review"]["source_coverage"][1]["verdict"] = "covered"
    assert evaluate(api_result(analysis), api_result(cases), review)["status"] == "pass"


@pytest.mark.parametrize("change", [
    lambda a, c, r: r["gold"]["acceptance_criteria"][0].update(requirement_gold_id="unknown"),
    lambda a, c, r: r["gold"]["acceptance_criteria"][0].update(observed_criterion_ids=[]),
    lambda a, c, r: r["gold"]["acceptance_criteria"][0].update(observed_criterion_ids=["AC-unknown"]),
    lambda a, c, r: c["data"]["cases"][0].update(traceability=[]),
    lambda a, c, r: c["data"]["cases"][0].update(traceability=[
        {"requirement_id": "REQ-other", "acceptance_criterion_id": "AC-1"}]),
    lambda a, c, r: c["data"]["cases"][0].update(traceability=[
        {"requirement_id": "REQ-1", "acceptance_criterion_id": None}]),
    lambda a, c, r: r.update(schema="harness-quality-review.v1"),
])
def test_review_cannot_claim_acceptance_coverage_without_an_actual_trace(change):
    analysis, cases, review = sample()
    change(analysis, cases, review)
    with pytest.raises(ReviewError):
        evaluate(api_result(analysis), api_result(cases), review)


def test_criterion_from_another_observed_requirement_does_not_validate_the_wrong_pair():
    analysis, cases, review = sample()
    analysis["data"]["requirements"].append({"id": "REQ-2", "acceptance_criteria": [{"id": "AC-2"}]})
    review["additional_supported_requirements"] = [{"id": "REQ-2", "evidence": "spec:section-4"}]
    cases["data"]["cases"][0]["traceability"] = [
        {"requirement_id": "REQ-2", "acceptance_criterion_id": "AC-2"}]
    review["gold"]["acceptance_criteria"][0]["observed_criterion_ids"] = ["AC-2"]
    with pytest.raises(ReviewError, match="criterion does not belong"):
        evaluate(api_result(analysis), api_result(cases), review)


def test_every_observed_model_criterion_must_have_a_case_trace_for_correct_gold():
    analysis, cases, review = sample()
    analysis["data"]["requirements"][0]["acceptance_criteria"].append({"id": "AC-2"})
    review["gold"]["acceptance_criteria"][0]["observed_criterion_ids"] = ["AC-1", "AC-2"]
    with pytest.raises(ReviewError, match="Every reviewed model criterion"):
        evaluate(api_result(analysis), api_result(cases), review)
    cases["data"]["cases"][0]["traceability"].append({
        "requirement_id": "REQ-1", "acceptance_criterion_id": "AC-2",
    })
    assert evaluate(api_result(analysis), api_result(cases), review)["status"] == "pass"


def test_every_ambiguity_mapped_to_correct_blocking_gold_remains_unresolved():
    analysis, cases, review = sample()
    analysis["data"]["ambiguities"].append({"id": "AMB-2", "severity": "blocking", "resolution": "assumed"})
    review["gold"]["blocking_ambiguities"][0]["observed_ids"] = ["AMB-1", "AMB-2"]
    with pytest.raises(ReviewError, match="blocking and unresolved"):
        evaluate(api_result(analysis), api_result(cases), review)
    analysis["data"]["ambiguities"][1]["resolution"] = None
    assert evaluate(api_result(analysis), api_result(cases), review)["status"] == "pass"


def test_partially_related_case_can_be_reviewed_without_claiming_full_criterion_coverage():
    analysis, cases, review = sample()
    cases["data"]["cases"][0]["traceability"] = [
        {"requirement_id": "REQ-1", "acceptance_criterion_id": None}]
    review["gold"]["acceptance_criteria"][0].update(
        verdict="partial", observed_criterion_ids=[])
    report = evaluate(api_result(analysis), api_result(cases), review)
    assert report["status"] == "fail"
    assert report["acceptance_criteria"]["correct_ratio"] == 0


def test_critical_omission_and_hallucinated_outputs_fail_gate():
    analysis, cases, review = sample()
    review["gold"]["requirements"][0].update(observed_ids=[], verdict="missing")
    review["unsupported_requirement_ids"] = ["REQ-1"]
    review["gold"]["blocking_ambiguities"][0].update(observed_ids=[], verdict="missing")
    review["additional_ambiguities"] = [{"id": "AMB-1", "evidence": "spec:section-2"}]
    review["gold"]["acceptance_criteria"][0].update(
        observed_ids=[], observed_criterion_ids=[], verdict="missing")
    review["unusable_case_ids"] = ["TC-1"]
    result = evaluate(api_result(analysis), api_result(cases), review)
    assert result["status"] == "fail"
    assert set(result["failures"]) == {
        "requirement_recall_below_threshold", "acceptance_coverage_below_threshold",
        "critical_requirement_not_correct", "blocking_ambiguity_not_correct",
        "unsupported_requirements", "unusable_cases",
    }


@pytest.mark.parametrize("change", [
    lambda a, c, r: c.update(analysis_run_id="another-run"),
    lambda a, c, r: c.update(source_document_ids=["another-document"]),
    lambda a, c, r: r["gold"]["requirements"][0].update(observed_ids=["invented-id"]),
    lambda a, c, r: r["gold"]["blocking_ambiguities"][0].update(verdict="correct", observed_ids=[]),
    lambda a, c, r: a["data"]["ambiguities"][0].update(severity="non_blocking"),
    lambda a, c, r: a["data"]["requirements"].append({"id": "REQ-unreviewed"}),
    lambda a, c, r: r["additional_usable_cases"].append({"id": "TC-1", "evidence": "conflicts with gold"}),
    lambda a, c, r: r["limits"].update(requirements_min=-1),
    lambda a, c, r: r["limits"].update(requirements_min=0),
    lambda a, c, r: r["execution"].update(platform="freebsd"),
    lambda a, c, r: r["execution"].update(harness_sha="dirty-checkout"),
    lambda a, c, r: r["execution"].update(profile="unsafe-profile"),
    lambda a, c, r: r.update(cases_task_id="another-task"),
])
def test_unbound_unreviewed_or_conflicting_quality_inputs_fail_closed(change):
    analysis, cases, review = sample()
    change(analysis, cases, review)
    with pytest.raises(ReviewError):
        evaluate(api_result(analysis), api_result(cases), review)


def test_usable_extra_results_require_explicit_evidence():
    analysis, cases, review = sample()
    analysis["data"]["requirements"].append({"id": "REQ-extra"})
    cases["data"]["cases"].append({"id": "TC-extra", "traceability": [
        {"requirement_id": "REQ-extra", "acceptance_criterion_id": None}]})
    review["additional_supported_requirements"] = [{"id": "REQ-extra", "evidence": "spec:section-4"}]
    review["additional_usable_cases"] = [{"id": "TC-extra", "evidence": "spec:section-4"}]
    assert evaluate(api_result(analysis), api_result(cases), review)["status"] == "pass"
    cases["data"]["cases"][-1]["traceability"][0]["requirement_id"] = "another-requirement"
    with pytest.raises(ReviewError, match="traceability"):
        evaluate(api_result(analysis), api_result(cases), review)


@pytest.mark.parametrize("bare_index", [0, 1])
def test_bare_result_cannot_be_used_as_completed_task_evidence(bare_index):
    analysis, cases, review = sample()
    results = [api_result(analysis), api_result(cases)]
    results[bare_index] = [analysis, cases][bare_index]
    with pytest.raises(ReviewError):
        evaluate(*results, review)


@pytest.mark.parametrize("status,task_id,result_schema", [
    ("failed", "analysis-task", "requirement-analysis.v1"),
    ("completed", "stale-task", "requirement-analysis.v1"),
    ("completed", "analysis-task", "test-cases.v1"),
])
def test_projected_api_result_must_be_completed_and_identity_bound(status, task_id, result_schema):
    analysis, cases, review = sample()
    wrapped = {**api_result(analysis), "status": status, "task_id": task_id, "result_schema": result_schema}
    with pytest.raises(ReviewError):
        evaluate(wrapped, api_result(cases), review)


@pytest.mark.parametrize("side,field,value", [
    (0, "analysis_run_id", "different-analysis"),
    (1, "analysis_run_id", "different-analysis"),
    (1, "project_id", 2),
    (0, "project_id", True),
    (0, "source_document_ids", ["other-doc"]),
    (1, "source_document_ids", ["other-doc"]),
])
def test_quality_review_rejects_mismatched_authoritative_task_binding(side, field, value):
    analysis, cases, review = sample()
    responses = [api_result(analysis), api_result(cases)]
    responses[side][field] = value
    with pytest.raises(ReviewError):
        evaluate(*responses, review)


def test_cases_from_another_analysis_cannot_pass_using_the_same_source_documents():
    analysis, cases, review = sample()
    cases["analysis_run_id"] = "run-2"
    review["analysis_run_id"] = "run-2"
    case_response = api_result(cases)
    case_response["analysis_run_id"] = "run-2"
    with pytest.raises(ReviewError, match="two original task results"):
        evaluate(api_result(analysis), case_response, review)


def test_cli_prints_only_aggregate_and_hashes_not_sensitive_source(tmp_path, capsys):
    analysis, cases, review = sample()
    paths = [tmp_path / name for name in ("analysis.json", "cases.json", "review.json")]
    for path, value in zip(paths, (api_result(analysis), api_result(cases), review)):
        path.write_text(json.dumps(value), encoding="utf-8")
    args = ["--analysis", str(paths[0]), "--cases", str(paths[1]), "--review", str(paths[2])]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "pass"
    assert "private original material" not in output and "private case" not in output
    assert "Synthetic report body" not in output
    bad = deepcopy(review)
    bad["gold"]["requirements"][0]["observed_ids"] = ["private original material"]
    paths[2].write_text(json.dumps(bad), encoding="utf-8")
    assert main(args) == 78
    captured = capsys.readouterr()
    assert "private original material" not in captured.out + captured.err
    paths[2].write_text('{"schema":"harness-quality-review.v3","schema":"private original material"}', encoding="utf-8")
    assert main(args) == 78
    assert "private original material" not in capsys.readouterr().err


def test_cli_rejects_stale_report_hash_without_echoing_report(tmp_path, capsys):
    analysis, cases, review = sample()
    analysis["report_markdown"] = "# Secret report\n\nprivate-unreviewed-report"
    paths = [tmp_path / name for name in ("analysis.json", "cases.json", "review.json")]
    for path, value in zip(paths, (api_result(analysis), api_result(cases), review)):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["--analysis", str(paths[0]), "--cases", str(paths[1]), "--review", str(paths[2])]) == 78
    captured = capsys.readouterr()
    assert "private-unreviewed-report" not in captured.out + captured.err


def test_large_completed_result_can_be_reviewed_without_echoing_material(tmp_path, capsys):
    analysis, cases, review = sample()
    analysis["data"]["requirements"][0]["description"] = "private-large-material-" + "x" * (17 * 1024 * 1024)
    paths = [tmp_path / name for name in ("analysis.json", "cases.json", "review.json")]
    for path, value in zip(paths, (api_result(analysis), api_result(cases), review)):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["--analysis", str(paths[0]), "--cases", str(paths[1]), "--review", str(paths[2])]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "pass"
    assert "private-large-material" not in output.out + output.err


@pytest.mark.parametrize("position,limit", [(0, MAX_RESULT_FILE_BYTES), (2, MAX_REVIEW_FILE_BYTES)])
def test_oversized_input_is_rejected_before_reading_or_echoing(tmp_path, capsys, position, limit):
    paths = [tmp_path / name for name in ("analysis.json", "cases.json", "review.json")]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    with paths[position].open("r+b") as target:
        target.truncate(limit + 1)
    assert main(["--analysis", str(paths[0]), "--cases", str(paths[1]), "--review", str(paths[2])]) == 78
    assert "Input" not in capsys.readouterr().err


def test_cli_rejects_symlinked_result(tmp_path, capsys):
    analysis, cases, review = sample()
    original = tmp_path / "source.json"
    original.write_text(json.dumps(api_result(analysis)), encoding="utf-8")
    link = tmp_path / "analysis.json"
    try:
        link.symlink_to(original)
    except OSError:
        pytest.skip("This platform cannot create a file symlink")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(api_result(cases)), encoding="utf-8")
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(review), encoding="utf-8")
    assert main(["--analysis", str(link), "--cases", str(cases_file), "--review", str(review_file)]) == 78
    assert "source.json" not in capsys.readouterr().err


def test_cli_rejects_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    fifo = tmp_path / "analysis.json"
    os.mkfifo(fifo)
    assert main(["--analysis", str(fifo), "--cases", str(fifo), "--review", str(fifo)]) == 78
