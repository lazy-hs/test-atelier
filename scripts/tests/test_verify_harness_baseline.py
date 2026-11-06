import hashlib
import json

import pytest

from scripts import verify_harness_baseline
from scripts.verify_harness_baseline import (check_harness_manifest, check_sha256, check_upstream_remote,
                                             checkout_paths)


def test_release_gate_defaults_to_sibling_checkouts_without_machine_paths(tmp_path):
    root = tmp_path / "test-atelier"
    assert checkout_paths({"HARNESS_ROOT": "", "HARNESS_EXTENSIONS_ROOT": ""}, root=root) == (
        tmp_path / "deepseek-harness", tmp_path / "test-atelier-harness-extensions",
    )


def test_release_gate_accepts_explicit_absolute_checkout_overrides(tmp_path):
    root = tmp_path / "test-atelier"
    harness = tmp_path / "source" / "harness"
    extension = tmp_path / "source" / "extensions"
    assert checkout_paths({"HARNESS_ROOT": str(harness), "HARNESS_EXTENSIONS_ROOT": str(extension)}, root=root) == (
        harness, extension,
    )


@pytest.mark.parametrize("setting", ["HARNESS_ROOT", "HARNESS_EXTENSIONS_ROOT"])
def test_release_gate_rejects_relative_checkout_override(tmp_path, setting):
    with pytest.raises(RuntimeError, match=f"{setting} must be an absolute path"):
        checkout_paths({setting: "relative-checkout"}, root=tmp_path / "test-atelier")


def write_harness_manifest(tmp_path):
    package = {
        "version": "0.2.1-rc.3",
        "packageManager": "pnpm@11.28.5",
        "engines": {"node": ">=24.9.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    return package


def test_harness_manifest_check_covers_release_metadata_and_lockfile(tmp_path):
    write_harness_manifest(tmp_path)
    lock_hash = hashlib.sha256((tmp_path / "pnpm-lock.yaml").read_bytes()).hexdigest()

    metadata = check_harness_manifest(
        tmp_path,
        {
            "version": "0.2.1-rc.3",
            "packageManager": "pnpm@11.28.5",
            "node": ">=24.9.0",
            "lockfileSha256": lock_hash,
        },
    )

    assert metadata["version"] == "0.2.1-rc.3"
    assert metadata["package_manager"] == "pnpm@11.28.5"
    assert metadata["lockfile_sha256"] == lock_hash


def test_harness_manifest_check_rejects_lockfile_drift(tmp_path):
    write_harness_manifest(tmp_path)

    with pytest.raises(RuntimeError, match="Harness lockfile hash mismatch"):
        check_harness_manifest(
            tmp_path,
            {
                "version": "0.2.1-rc.3",
                "packageManager": "pnpm@11.28.5",
                "node": ">=24.9.0",
                "lockfileSha256": "0" * 64,
            },
        )


def test_check_sha256_rejects_missing_artifact(tmp_path):
    with pytest.raises(RuntimeError, match="file does not exist"):
        check_sha256(tmp_path / "missing.lock", "", "Harness lockfile")


def test_upstream_remote_check_normalizes_git_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(
        verify_harness_baseline,
        "git",
        lambda _root, *_args: "https://github.com/deepseek-ai/deepseek-harness.git",
    )
    assert check_upstream_remote(tmp_path, "https://github.com/deepseek-ai/deepseek-harness")


def test_upstream_remote_check_rejects_wrong_source(monkeypatch, tmp_path):
    monkeypatch.setattr(
        verify_harness_baseline,
        "git",
        lambda _root, *_args: "https://gitee.com/mirror/deepseek-harness.git",
    )
    with pytest.raises(RuntimeError, match="upstream remote mismatch"):
        check_upstream_remote(tmp_path, "https://github.com/deepseek-ai/deepseek-harness")
