"""Stable, content-free identity for the confirmed analysis sent to Harness."""

import hashlib
import json


class AnalysisContextChanged(ValueError):
    """A case task no longer matches the analysis snapshot it consumed."""


def case_analysis_context(run_id: str, version: int, result: dict) -> dict:
    original = result["harness_result"]
    data = original["data"]
    decisions = {
        item["id"]: item["decision"].strip()
        for item in result.get("ambiguities", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("decision"), str) and item["decision"].strip()
    }
    ambiguities = [
        {**item, "resolution": decisions.get(item.get("id"), item.get("resolution"))}
        for item in data.get("ambiguities", []) if isinstance(item, dict)
    ]
    return {
        "schema": "requirement-analysis.v1",
        "analysis_run_id": run_id,
        "analysis_version": version,
        "requirements": data.get("requirements", []),
        "ambiguities": ambiguities,
        "quality_warnings": data.get("quality_warnings", []),
        "warnings": original.get("warnings", []),
    }


def case_context_sha256(context: dict) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(context):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()
