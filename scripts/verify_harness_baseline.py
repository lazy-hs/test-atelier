#!/usr/bin/env python3
"""Verify the locked Harness and Extension checkouts without changing either.

The Test Atelier checkout may contain normal application changes.  This gate
only requires the two integration checkouts to be clean and to match the
recorded SHA, release metadata, supported platform and lockfiles in
``docs/harness-baseline.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "harness-baseline.json"


def checkout_paths(values: Mapping[str, str] | None = None, *, root: Path = ROOT) -> tuple[Path, Path]:
    """Use sibling checkouts on every supported platform, not a developer path."""
    settings = os.environ if values is None else values
    paths = []
    for setting, name in (("HARNESS_ROOT", "deepseek-harness"),
                          ("HARNESS_EXTENSIONS_ROOT", "test-atelier-harness-extensions")):
        configured = settings.get(setting, "").strip()
        path = Path(configured).expanduser() if configured else root.parent / name
        if not path.is_absolute():
            raise RuntimeError(f"{setting} must be an absolute path")
        paths.append(path)
    return paths[0], paths[1]


def git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def check_checkout(root: Path, expected_sha: str, label: str) -> None:
    if not root.is_dir():
        raise RuntimeError(f"{label} checkout does not exist: {root}")
    actual = git(root, "rev-parse", "HEAD")
    if expected_sha and actual != expected_sha:
        raise RuntimeError(f"{label} SHA mismatch: expected {expected_sha}, got {actual}")
    status = git(root, "status", "--porcelain")
    if status:
        raise RuntimeError(f"{label} checkout is dirty:\n{status}")


def check_sha256(path: Path, expected: str, label: str) -> str:
    """Return a file digest and fail when a recorded artifact drifted."""
    if not path.is_file():
        raise RuntimeError(f"{label} file does not exist: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and actual != expected:
        raise RuntimeError(f"{label} hash mismatch: expected {expected}, got {actual}")
    return actual


def check_harness_manifest(root: Path, baseline: dict[str, object]) -> dict[str, str]:
    """Check release metadata in addition to the Harness Git SHA."""
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Harness package manifest is unreadable: {exc}") from exc
    for key in ("version", "packageManager"):
        expected = baseline.get(key)
        actual = package.get(key)
        if expected and actual != expected:
            raise RuntimeError(f"Harness {key} mismatch: expected {expected}, got {actual}")
    expected_node = baseline.get("node")
    actual_node = (package.get("engines") or {}).get("node")
    if expected_node and actual_node != expected_node:
        raise RuntimeError(f"Harness Node engine mismatch: expected {expected_node}, got {actual_node}")
    lock_hash = check_sha256(root / "pnpm-lock.yaml", str(baseline.get("lockfileSha256", "")), "Harness lockfile")
    return {
        "version": str(package.get("version", "")),
        "package_manager": str(package.get("packageManager", "")),
        "node_engine": str(actual_node or ""),
        "lockfile_sha256": lock_hash,
    }


def check_upstream_remote(root: Path, expected: str) -> str:
    """Ensure the locked checkout still points at the recorded upstream."""
    if not expected:
        return ""
    try:
        actual = git(root, "remote", "get-url", "upstream")
    except RuntimeError as exc:
        raise RuntimeError("Harness upstream remote is missing") from exc

    def normalize(value: str) -> str:
        return value.rstrip("/").removesuffix(".git")

    if normalize(actual) != normalize(expected):
        raise RuntimeError(f"Harness upstream remote mismatch: expected {expected}, got {actual}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON result")
    args = parser.parse_args()
    try:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        harness_root, extension_root = checkout_paths()
        current_platform = sys.platform
        supported_platforms = baseline.get("supportedPlatforms", [])
        if supported_platforms and current_platform not in supported_platforms:
            raise RuntimeError(f"Unsupported platform for Harness baseline: {current_platform}")
        check_checkout(harness_root, baseline["commit"], "Harness")
        harness_metadata = check_harness_manifest(harness_root, baseline)
        harness_remote = check_upstream_remote(harness_root, str(baseline.get("upstreamRemote", "")))
        check_checkout(extension_root, baseline["extensionCommit"], "Extension")
        lockfile = extension_root / "package-lock.json"
        actual_lock = check_sha256(lockfile, str(baseline.get("extensionLockfileSha256", "")), "Extension lockfile")
        package = json.loads((extension_root / "package.json").read_text(encoding="utf-8"))
        expected_extension_version = baseline.get("extensionVersion")
        if expected_extension_version and package.get("version") != expected_extension_version:
            raise RuntimeError(
                f"Extension version mismatch: expected {expected_extension_version}, got {package.get('version')}"
            )
        result = {
            "harness_root": str(harness_root),
            "harness_sha": baseline["commit"],
            "harness_version": harness_metadata["version"],
            "harness_package_manager": harness_metadata["package_manager"],
            "harness_node_engine": harness_metadata["node_engine"],
            "harness_lockfile_sha256": harness_metadata["lockfile_sha256"],
            "harness_upstream_remote": harness_remote,
            "extension_root": str(extension_root),
            "extension_sha": baseline["extensionCommit"],
            "extension_version": package.get("version"),
            "extension_lockfile_sha256": actual_lock,
            "platform": current_platform,
            "status": "clean",
        }
    except (OSError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Harness baseline verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False) if args.json else "Harness baseline: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
