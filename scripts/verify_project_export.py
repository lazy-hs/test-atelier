"""Offline integrity and boundary checks for a Test Atelier evidence ZIP."""

import argparse
import hashlib
import json
import re
import stat
import zipfile
from collections import Counter
from pathlib import Path

MAX_FILES = 10000
MAX_BYTES = 512 * 1024 * 1024
MAX_LINKS = 50000
SAFE_NAME = re.compile(r"(?:[a-zA-Z0-9_-]+/)?[a-zA-Z0-9_-]+\.(?:json|bin|md)\Z")
KINDS = frozenset({"import", "document", "analysis", "analysis_report", "harness_task", "draft", "case",
                   "taxonomy", "suggestion_job", "suggestion", "governance_change",
                   "duplicate_group", "duplicate_member", "governance_validation", "governance_metric",
                   "governance_directory", "governance_export_job",
                   "conversion", "conversion_input", "smoke_suite", "smoke_item", "execution_run", "run_case",
                   "execution_report", "report_case", "defect", "knowledge"})


def _reference_file(kind: str, identifier: str) -> str:
    if kind == "report_case":
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", identifier):
            raise ValueError("invalid frozen case reference")
        return f"report_case/{identifier.replace(':', '-')}.json"
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identifier):
        raise ValueError("invalid evidence reference identifier")
    return f"{kind}/{identifier}.{'md' if kind == 'analysis_report' else 'json'}"


def _expected_references(archive: zipfile.ZipFile, by_path: dict) -> Counter:
    expected = Counter()
    directories = {}
    for path in by_path:
        if path.startswith("governance_directory/"):
            record = json.loads(archive.read(path))
            directories[record["path"]] = str(record["id"])

    count = 0
    def add(source_kind, source_id, target_kind, target_id, relation):
        nonlocal count
        expected[(source_kind, str(source_id), target_kind, str(target_id), relation)] += 1
        count += 1
        if count > MAX_LINKS:
            raise ValueError("too many expected project references")

    for path in by_path:
        kind = path.split("/", 1)[0]
        if kind not in {"analysis", "harness_task", "draft", "case", "suggestion_job", "suggestion",
                        "governance_change", "duplicate_group", "duplicate_member", "governance_metric",
                        "governance_directory", "conversion_input", "smoke_item", "execution_run", "run_case",
                        "execution_report", "defect", "knowledge"}:
            continue
        row = json.loads(archive.read(path))
        identifier = row["source"]["run_id"] if kind == "execution_report" else row["id"]
        if kind == "analysis":
            for source in row["source_ids"] or []:
                add("document", source, kind, identifier, "analyzed_from")
        elif kind == "harness_task":
            for source in row["source_document_ids"] or []:
                add("document", source, kind, identifier, "submitted_to")
            if row["analysis_run_id"]:
                add("analysis", row["analysis_run_id"], kind, identifier, "generated_by")
            if (row["task_type"] == "requirement-analysis" and row["status"] == "completed"
                    and isinstance(row["result"], dict) and row["result"].get("report_markdown") is not None):
                add("harness_task", identifier, "analysis_report", identifier, "contains_report")
                add("analysis_report", identifier, "analysis", row["analysis_run_id"], "reviewed_as")
        elif kind == "draft":
            add("analysis", row["analysis_id"], kind, identifier, "drafted_from")
            if row["case_id"] is not None:
                add(kind, identifier, "case", row["case_id"], "published_as")
        elif kind == "case":
            add("import", row["import_id"], kind, identifier, "imported_as")
        elif kind == "suggestion_job":
            if row["taxonomy_version_id"] is not None:
                add("taxonomy", row["taxonomy_version_id"], kind, identifier, "used_for_suggestions")
        elif kind == "suggestion":
            add("case", row["case_id"], kind, identifier, "suggested_for")
            if row["taxonomy_version_id"] is not None:
                add("taxonomy", row["taxonomy_version_id"], kind, identifier, "suggested_with")
            if row["job_id"] is not None:
                add("suggestion_job", row["job_id"], kind, identifier, "produced_suggestion")
        elif kind == "governance_change":
            entity_id = row["entity_id"]
            if (row["entity"] in {"case", "duplicate-member"} and isinstance(entity_id, str)
                    and entity_id.isascii() and entity_id.isdecimal()):
                add("case", int(entity_id), kind, identifier, "governed_as")
            elif row["entity"] == "directory" and entity_id in directories:
                add("governance_directory", directories[entity_id], kind, identifier, "governed_as")
        elif kind == "duplicate_group":
            add("import", row["batch_id"], kind, identifier, "grouped_duplicates")
        elif kind == "duplicate_member":
            add("duplicate_group", row["group_id"], kind, identifier, "contains_duplicate")
            add("case", row["case_id"], kind, identifier, "reviewed_as_duplicate")
        elif kind == "governance_metric":
            add("import", row["batch_id"], kind, identifier, "measured_as")
        elif kind == "governance_directory" and row["parent"] in directories:
            add(kind, directories[row["parent"]], kind, identifier, "contains_directory")
        elif kind == "conversion_input":
            add(kind, identifier, "conversion", identifier, "converted_into")
        elif kind == "smoke_item":
            add("smoke_suite", row["suite_id"], kind, identifier, "contains_case")
            add("case", row["case_id"], kind, identifier, "selected_for_smoke")
        elif kind == "execution_run":
            add("smoke_suite", row["suite_id"], kind, identifier, "executed_as")
        elif kind == "run_case":
            add("execution_run", row["run_id"], kind, identifier, "contains_case")
            add("case", row["case_id"], kind, identifier, "executed_as")
        elif kind == "execution_report":
            add("execution_run", identifier, kind, identifier, "completed_as")
            for frozen in row["cases"]:
                logical_id = f"{identifier}:{frozen['run_case_id']}"
                add(kind, identifier, "report_case", logical_id, "contains_case")
                add("run_case", frozen["run_case_id"], "report_case", logical_id, "frozen_as")
                add("case", frozen["case_id"], "report_case", logical_id, "snapshot_of")
        elif kind == "defect":
            add("execution_report", row["run_id"], kind, identifier, "records_defect")
            add("report_case", f"{row['run_id']}:{row['run_case_id']}", kind, identifier, "found_defect")
        elif kind == "knowledge":
            add(row["source_type"], row["source_id"], kind, identifier, "curated_as")
    return expected


def verify(path: Path, max_bytes: int = MAX_BYTES) -> dict:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) > MAX_FILES or len(set(names)) != len(names) or "manifest.json" not in names:
            raise ValueError("invalid evidence archive entries")
        total = 0
        for info in infos:
            total += info.file_size
            pieces = info.filename.split("/")
            expected = (info.filename == "manifest.json" or info.filename == "indexes/references.json"
                        or (len(pieces) == 2 and pieces[0] in KINDS and pieces[1].endswith(".json"))
                        or (len(pieces) == 2 and pieces[0] in ("document-files", "conversion-files", "conversion-input-files",
                                                                  "governance-import-files", "governance-export-files")
                            and pieces[1].endswith(".bin"))
                        or (len(pieces) == 2 and pieces[0] == "analysis_report" and pieces[1].endswith(".md")
                            and info.file_size <= 1024 * 1024))
            if (not SAFE_NAME.fullmatch(info.filename) or info.is_dir()
                    or not expected
                    or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
                    or info.file_size > 128 * 1024 * 1024 or total > max_bytes):
                raise ValueError("unsafe or oversized evidence archive entry")
        if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
            raise ValueError("oversized evidence manifest")
        manifest = json.loads(archive.read("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("schema_version") not in {"project-evidence.v1", "project-evidence.v2"}
                or type(manifest.get("project_id")) is not int or manifest["project_id"] < 1
                or not isinstance(manifest.get("files"), list)):
            raise ValueError("invalid evidence manifest")
        entries = manifest["files"]
        paths = [item.get("path") for item in entries if isinstance(item, dict)]
        if (len(paths) != len(entries) or any(not isinstance(value, str) for value in paths)
                or len(paths) != len(set(paths))
                or set(paths) != set(names) - {"manifest.json"}):
            raise ValueError("evidence manifest does not cover archive entries")
        by_path = {item["path"]: item for item in entries}
        counts = {kind: sum(value.startswith(kind + "/") for value in paths) for kind in KINDS}
        recorded_counts = manifest.get("counts")
        if (not isinstance(recorded_counts, dict) or not recorded_counts
                or set(recorded_counts) - KINDS
                or any(type(value) is not int or value < 0 or value != counts[kind]
                       for kind, value in recorded_counts.items())
                or any(value and kind not in recorded_counts for kind, value in counts.items())):
            raise ValueError("evidence manifest counts mismatch")
        if manifest["schema_version"] == "project-evidence.v2" and "analysis_report" not in recorded_counts:
            raise ValueError("v2 evidence manifest is missing the report delivery contract")
        frozen_cases = {}
        for item in entries:
            info = archive.getinfo(item["path"])
            if (type(item.get("size")) is not int or item["size"] != info.file_size
                    or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
                raise ValueError("evidence entry metadata mismatch")
            digest = hashlib.sha256()
            with archive.open(info) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("evidence entry checksum mismatch")
            if any(item["path"].startswith(kind + "/") for kind in KINDS - {"execution_report", "report_case"}):
                if not item["path"].startswith("analysis_report/"):
                    payload = json.loads(archive.read(info))
                    if (not isinstance(payload, dict) or payload.get("project_id") != manifest["project_id"]
                            or str(payload.get("id")) != info.filename.split("/")[-1][:-5]):
                        raise ValueError("project artifact identity mismatch")
            if item["path"].startswith("execution_report/"):
                report = json.loads(archive.read(info))
                expected = report.get("integrity", {}).get("payload_sha256")
                actual = hashlib.sha256(json.dumps({k: v for k, v in report.items() if k != "integrity"},
                                            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                if (expected != actual or report.get("source", {}).get("project_id") != manifest["project_id"]
                        or str(report.get("source", {}).get("run_id")) != info.filename.split("/")[-1][:-5]):
                    raise ValueError("execution report does not match its source")
                if "report_case" in recorded_counts:
                    run_id = report["source"]["run_id"]
                    if type(run_id) is not int or not isinstance(report.get("cases"), list):
                        raise ValueError("invalid frozen report cases")
                    for frozen in report["cases"]:
                        case_id = frozen.get("run_case_id") if isinstance(frozen, dict) else None
                        if type(case_id) is not int or case_id < 1 or type(frozen.get("case_id")) is not int or frozen["case_id"] < 1:
                            raise ValueError("invalid frozen report case identity")
                        frozen_path = f"report_case/{run_id}-{case_id}.json"
                        if frozen_path in frozen_cases:
                            raise ValueError("duplicate frozen report case identity")
                        frozen_cases[frozen_path] = (run_id, case_id, expected, frozen)
            if item["path"].startswith("conversion_input/"):
                source = json.loads(archive.read(info))
                identifier = source.get("id")
                binary = f"conversion-input-files/{identifier}.bin"
                binary_entry = by_path.get(binary)
                result_path = f"conversion/{identifier}.json"
                result = json.loads(archive.read(result_path)) if result_path in by_path else {}
                metadata = result.get("preview", {}).get("_source") if isinstance(result.get("preview"), dict) else None
                if (binary_entry is None or source.get("conversion_id") != identifier
                        or source.get("sha256") != binary_entry.get("sha256")
                        or source.get("size") != binary_entry.get("size")
                        or not isinstance(metadata, dict) or metadata.get("schema_version") != "conversion-upload.v1"
                        or metadata.get("sha256") != source.get("sha256")
                        or metadata.get("size") != source.get("size")):
                    raise ValueError("conversion input does not match its source")
            if item["path"].startswith("document/"):
                document = json.loads(archive.read(info))
                binary = by_path.get(f"document-files/{document['id']}.bin")
                integrity = document.get("creation_integrity")
                if binary is None:
                    raise ValueError("document original file is missing")
                if integrity == "verified":
                    if (not isinstance(document.get("source_sha256"), str)
                            or not re.fullmatch(r"[a-f0-9]{64}", document["source_sha256"])
                            or type(document.get("source_size")) is not int or document["source_size"] < 0
                            or document["source_sha256"] != binary["sha256"]
                            or document["source_size"] != binary["size"]):
                        raise ValueError("document original file does not match upload identity")
                elif integrity == "unverified":
                    if document.get("source_sha256") is not None or document.get("source_size") is not None:
                        raise ValueError("legacy document cannot claim upload identity")
                elif integrity is not None or "source_sha256" in document or "source_size" in document:
                    raise ValueError("invalid document upload integrity metadata")
            if item["path"].startswith("document-files/"):
                identifier = item["path"].split("/")[-1][:-4]
                if f"document/{identifier}.json" not in by_path:
                    raise ValueError("unreferenced document original file")
            if item["path"].startswith("conversion-input-files/"):
                identifier = item["path"].split("/")[-1][:-4]
                if f"conversion_input/{identifier}.json" not in by_path:
                    raise ValueError("unreferenced conversion input")
            if item["path"].startswith("conversion/"):
                conversion = json.loads(archive.read(info))
                binary = by_path.get(f"conversion-files/{conversion['id']}.bin")
                integrity = conversion.get("creation_integrity")
                if binary is None or conversion.get("file_size") != binary["size"]:
                    raise ValueError("conversion output file is missing or has wrong size")
                if integrity == "verified":
                    if (not isinstance(conversion.get("output_sha256"), str)
                            or not re.fullmatch(r"[a-f0-9]{64}", conversion["output_sha256"])
                            or conversion["output_sha256"] != binary["sha256"]):
                        raise ValueError("conversion output does not match creation identity")
                elif integrity == "unverified":
                    if conversion.get("output_sha256") is not None:
                        raise ValueError("legacy conversion cannot claim creation identity")
                elif integrity is not None or "output_sha256" in conversion:
                    raise ValueError("invalid conversion output integrity metadata")
                preview = conversion.get("preview")
                if (isinstance(preview, dict) and preview.get("_source") is not None
                        and f"conversion_input/{conversion['id']}.json" not in by_path):
                    raise ValueError("conversion input metadata has no archived source")
            if item["path"].startswith("conversion-files/"):
                identifier = item["path"].split("/")[-1][:-4]
                if f"conversion/{identifier}.json" not in by_path:
                    raise ValueError("unreferenced conversion output")
            if item["path"].startswith("suggestion_job/"):
                job = json.loads(archive.read(info))
                taxonomy_id = job.get("taxonomy_version_id")
                if taxonomy_id is not None and f"taxonomy/{taxonomy_id}.json" not in by_path:
                    raise ValueError("suggestion job has no archived taxonomy")
            if item["path"].startswith("suggestion/"):
                suggestion = json.loads(archive.read(info))
                sources = [("case", suggestion.get("case_id")),
                           ("taxonomy", suggestion.get("taxonomy_version_id")),
                           ("suggestion_job", suggestion.get("job_id"))]
                if (type(suggestion.get("case_id")) is not int or suggestion["case_id"] < 1
                        or any(f"{kind}/{identifier}.json" not in by_path
                               for kind, identifier in sources if identifier is not None)):
                    raise ValueError("suggestion has incomplete archived provenance")
            if item["path"].startswith(("duplicate_group/", "governance_metric/")):
                payload = json.loads(archive.read(info))
                if f"import/{payload.get('batch_id')}.json" not in by_path:
                    raise ValueError("governance artifact has no archived import")
            if item["path"].startswith("duplicate_member/"):
                member = json.loads(archive.read(info))
                if (f"duplicate_group/{member.get('group_id')}.json" not in by_path
                        or f"case/{member.get('case_id')}.json" not in by_path):
                    raise ValueError("duplicate member has incomplete archived provenance")
            if item["path"].startswith("governance_directory/"):
                directory = json.loads(archive.read(info))
                path_value = directory.get("path")
                if (not isinstance(path_value, str) or not isinstance(directory.get("parent"), str)
                        or hashlib.sha256(path_value.encode("utf-8")).hexdigest() != directory.get("id")):
                    raise ValueError("governance directory identity mismatch")
            if item["path"].startswith("import/"):
                imported = json.loads(archive.read(info))
                binary = by_path.get(f"governance-import-files/{imported['id']}.bin")
                saved = imported.get("source_saved", False)
                if (type(saved) is not bool or saved != bool(binary)
                        or binary is not None and (imported.get("kind") != "governance"
                                                   or imported.get("sha256") != binary["sha256"])):
                    raise ValueError("governance import source does not match its snapshot")
            if item["path"].startswith("governance_export_job/"):
                job = json.loads(archive.read(info))
                binary = by_path.get(f"governance-export-files/{job['id']}.bin")
                selfcheck = job.get("selfcheck") or {}
                if not isinstance(selfcheck, dict):
                    raise ValueError("governance export selfcheck is invalid")
                has_creation_digest = "created_sha256" in selfcheck or "created_size" in selfcheck
                integrity = job.get("creation_integrity")
                invalid_integrity = (integrity not in (None, "verified", "unverified")
                                     or (integrity is not None
                                         and (integrity == "verified") != has_creation_digest))
                if ("path" in job or
                        (binary is None and ("file_sha256" in job or "file_size" in job
                                             or has_creation_digest or integrity is not None)) or
                        (binary is not None and (job.get("file_sha256") != binary["sha256"]
                                                 or job.get("file_size") != binary["size"])) or
                        invalid_integrity):
                    raise ValueError("governance export file does not match its record")
                if has_creation_digest and (binary is None
                        or not isinstance(selfcheck.get("created_sha256"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", selfcheck["created_sha256"])
                        or type(selfcheck.get("created_size")) is not int
                        or selfcheck["created_size"] < 0
                        or selfcheck["created_sha256"] != binary["sha256"]
                        or selfcheck["created_size"] != binary["size"]):
                    raise ValueError("governance export creation checksum mismatch")
            for directory, record_kind in (("governance-import-files/", "import"),
                                           ("governance-export-files/", "governance_export_job")):
                if item["path"].startswith(directory):
                    identifier = item["path"].split("/")[-1][:-4]
                    if f"{record_kind}/{identifier}.json" not in by_path:
                        raise ValueError("unreferenced governance file")
        reference_index = json.loads(archive.read("indexes/references.json"))
        if (not isinstance(reference_index, dict) or reference_index.get("schema_version") not in {
                "project-references.v1", "project-references.v2"}
                or not isinstance(reference_index.get("links"), list)
                or len(reference_index["links"]) > MAX_LINKS):
            raise ValueError("invalid project reference index")
        for link in reference_index["links"]:
            if not isinstance(link, dict) or not isinstance(link.get("relation"), str) or not link["relation"]:
                raise ValueError("invalid project reference")
            for end in ("from", "to"):
                node = link.get(end)
                if (not isinstance(node, dict) or not isinstance(node.get("type"), str)
                        or node["type"] not in KINDS
                        or not isinstance(node.get("id"), str)
                        or type(node.get("included")) is not bool):
                    raise ValueError("invalid project reference")
                if node["included"] != (_reference_file(node["type"], node["id"]) in by_path):
                    raise ValueError("project reference inclusion does not match archive")
        if "analysis_report" in recorded_counts:
            expected_report_links = set()
            for path in by_path:
                if not path.startswith("harness_task/"):
                    continue
                task = json.loads(archive.read(path))
                raw = task.get("result")
                report = raw.get("report_markdown") if isinstance(raw, dict) else None
                if task.get("task_type") != "requirement-analysis" or task.get("status") != "completed" or report is None:
                    continue
                task_id = str(task["id"])
                name = f"analysis_report/{task_id}.md"
                analysis_id = task.get("analysis_run_id")
                if name not in by_path or not isinstance(analysis_id, str) or f"analysis/{analysis_id}.json" not in by_path:
                    raise ValueError("analysis report is missing its archived source")
                analysis = json.loads(archive.read(f"analysis/{analysis_id}.json"))
                original = analysis.get("result", {}).get("harness_result") if isinstance(analysis.get("result"), dict) else None
                try:
                    report_bytes = report.encode("utf-8") if isinstance(report, str) else b""
                    report_text = archive.read(name).decode("utf-8")
                except UnicodeError as exc:
                    raise ValueError("analysis report is not UTF-8") from exc
                if (not report_bytes or len(report_bytes) > 1024 * 1024
                        or not report_text.lstrip().startswith("# ") or report_text != report
                        or analysis.get("provider") != "harness" or analysis.get("status") != "ready"
                        or not isinstance(original, dict) or original.get("task_id") != task_id
                        or original.get("report_markdown") != report):
                    raise ValueError("analysis report does not match its task and analysis snapshot")
                expected_report_links.update({
                    ("harness_task", task_id, "analysis_report", task_id, "contains_report"),
                    ("analysis_report", task_id, "analysis", analysis_id, "reviewed_as"),
                })
            if {name for name in by_path if name.startswith("analysis_report/")} != {
                    f"analysis_report/{task_id}.md" for kind, task_id, _, _, _ in expected_report_links
                    if kind == "harness_task"}:
                raise ValueError("orphaned analysis report in evidence archive")
            report_links = []
            for link in reference_index["links"]:
                if not isinstance(link, dict) or not isinstance(link.get("from"), dict) or not isinstance(link.get("to"), dict):
                    raise ValueError("invalid analysis report references")
                source, target = link["from"], link["to"]
                if source.get("type") == "analysis_report" or target.get("type") == "analysis_report":
                    if source.get("included") is not True or target.get("included") is not True:
                        raise ValueError("analysis report reference is incomplete")
                    report_links.append((source.get("type"), source.get("id"), target.get("type"),
                                         target.get("id"), link.get("relation")))
            if len(report_links) != len(set(report_links)) or set(report_links) != expected_report_links:
                raise ValueError("analysis report references do not match the evidence")
        if "report_case" in recorded_counts:
            if set(frozen_cases) != {name for name in by_path if name.startswith("report_case/")}:
                raise ValueError("frozen report cases do not match the report")
            expected_links = set()

            def frozen_link(source_kind, source_id, target_kind, target_id, relation):
                expected_links.add((source_kind, str(source_id), target_kind, str(target_id), relation,
                                    f"{source_kind}/{source_id}.json" in by_path if source_kind != "report_case" else True,
                                    f"{target_kind}/{target_id}.json" in by_path if target_kind != "report_case" else True))

            for path, (run_id, case_id, report_digest, frozen) in frozen_cases.items():
                record = json.loads(archive.read(path))
                if (not isinstance(record, dict)
                        or set(record) != {"id", "project_id", "report_run_id", "report_sha256", "frozen"}
                        or record.get("id") != f"{run_id}:{case_id}"
                        or record.get("project_id") != manifest["project_id"]
                        or record.get("report_run_id") != run_id
                        or record.get("report_sha256") != report_digest
                        or json.dumps(record.get("frozen"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                           != json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))):
                    raise ValueError("frozen report case does not match its report")
                logical_id = f"{run_id}:{case_id}"
                frozen_link("execution_report", run_id, "report_case", logical_id, "contains_case")
                frozen_link("run_case", case_id, "report_case", logical_id, "frozen_as")
                frozen_link("case", frozen["case_id"], "report_case", logical_id, "snapshot_of")
            for path in by_path:
                if path.startswith("defect/"):
                    defect = json.loads(archive.read(path))
                    logical_id = f"{defect.get('run_id')}:{defect.get('run_case_id')}"
                    if f"report_case/{defect.get('run_id')}-{defect.get('run_case_id')}.json" not in frozen_cases:
                        raise ValueError("defect has no archived frozen report case")
                    frozen_link("report_case", logical_id, "defect", defect["id"], "found_defect")
            actual_links = []
            for link in reference_index["links"]:
                if not isinstance(link, dict) or not isinstance(link.get("from"), dict) or not isinstance(link.get("to"), dict):
                    raise ValueError("invalid frozen report case references")
                source, target = link["from"], link["to"]
                if source.get("type") == "report_case" or target.get("type") == "report_case":
                    if (type(source.get("included")) is not bool or type(target.get("included")) is not bool
                            or any(not isinstance(value, str) for value in
                                   (source.get("type"), source.get("id"), target.get("type"),
                                    target.get("id"), link.get("relation")))):
                        raise ValueError("invalid frozen report case references")
                    actual_links.append((source.get("type"), source.get("id"), target.get("type"), target.get("id"),
                                         link.get("relation"), source["included"], target["included"]))
            if len(actual_links) != len(set(actual_links)) or set(actual_links) != expected_links:
                raise ValueError("frozen report case references do not match the archive")
        if reference_index["schema_version"] == "project-references.v2":
            indexed = Counter((link["from"]["type"], link["from"]["id"], link["to"]["type"],
                               link["to"]["id"], link["relation"]) for link in reference_index["links"])
            if indexed != _expected_references(archive, by_path):
                raise ValueError("project reference index does not match archived provenance")
        return {"project_id": manifest["project_id"], "entries": len(entries),
                "counts": counts, "integrity": "verified",
                "reference_integrity": ("complete" if reference_index["schema_version"] == "project-references.v2"
                                        else "legacy-membership-only")}


def main():
    parser = argparse.ArgumentParser(description="Check project evidence file hashes, ZIP paths and report integrity")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    args = parser.parse_args()
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    print(json.dumps(verify(args.archive, args.max_bytes), ensure_ascii=False))


if __name__ == "__main__":
    main()
