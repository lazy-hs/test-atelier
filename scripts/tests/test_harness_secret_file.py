import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.harness_secret_file import host_token, scope_secret


def test_native_bridge_reads_optional_host_token_file_without_exposing_it(tmp_path):
    secret = tmp_path / "host-token"
    secret.write_bytes(b"h" * 20 + b"\r\n")
    values = {"TEST_ATELIER_HARNESS_TOKEN_FILE": str(secret)}
    assert host_token(values) == "h" * 20
    assert host_token({}) == ""
    with pytest.raises(ValueError):
        host_token({**values, "TEST_ATELIER_HARNESS_TOKEN": "different-token-123"})
    secret.write_bytes(b"short")
    with pytest.raises(ValueError):
        host_token(values)


def test_scope_secret_reads_single_line_file_without_inline_value(tmp_path):
    secret = tmp_path / "scope-key"
    secret.write_text("s" * 40 + "\n", encoding="utf-8")
    values = {"TEST_ATELIER_SCOPE_SECRET_FILE": str(secret)}
    assert scope_secret(values) == "s" * 40
    with pytest.raises(ValueError):
        scope_secret({**values, "TEST_ATELIER_SCOPE_SECRET": "inline-value"})


def test_scope_secret_accepts_windows_line_ending_but_rejects_embedded_crlf(tmp_path):
    secret = tmp_path / "scope-key"
    values = {"TEST_ATELIER_SCOPE_SECRET_FILE": str(secret)}
    secret.write_bytes(b"s" * 40 + b"\r\n")
    assert scope_secret(values) == "s" * 40
    secret.write_bytes(b"s" * 40 + b"\r\nother")
    with pytest.raises(ValueError):
        scope_secret(values)


@pytest.mark.parametrize("content", ["short", "s" * 40 + "\nsecond", "x" * 4097])
def test_scope_secret_rejects_invalid_file_content(tmp_path, content):
    secret = tmp_path / "scope-key"
    secret.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        scope_secret({"TEST_ATELIER_SCOPE_SECRET_FILE": str(secret)})


def test_scope_secret_rejects_symlink_and_relative_path(tmp_path):
    secret = tmp_path / "scope-key"
    secret.write_text("s" * 40, encoding="utf-8")
    if os.name != "nt":
        link = tmp_path / "link"
        link.symlink_to(secret)
        with pytest.raises(ValueError):
            scope_secret({"TEST_ATELIER_SCOPE_SECRET_FILE": str(link)})
    with pytest.raises(ValueError):
        scope_secret({"TEST_ATELIER_SCOPE_SECRET_FILE": "relative-key"})


def test_native_launcher_check_reads_secret_file_without_printing_value(tmp_path):
    secret = tmp_path / "scope-key"
    token = tmp_path / "host-token"
    secret.write_bytes(b"s" * 40 + b"\r\n")
    token.write_bytes(b"h" * 24 + b"\r\n")
    script = Path(__file__).resolve().parents[1] / "harness_secret_file.py"
    env = {**os.environ, "TEST_ATELIER_SCOPE_SECRET": "", "TEST_ATELIER_SCOPE_SECRET_FILE": str(secret),
           "TEST_ATELIER_HARNESS_TOKEN": "", "TEST_ATELIER_HARNESS_TOKEN_FILE": str(token)}
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
    env["TEST_ATELIER_SCOPE_SECRET"] = "x" * 40
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 78
    assert "s" * 40 not in result.stderr and "x" * 40 not in result.stderr
    env["TEST_ATELIER_SCOPE_SECRET"] = ""
    token.write_text("short", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 78 and "short" not in result.stderr


def test_native_launcher_can_load_secret_paths_from_runtime_config(tmp_path):
    secret = tmp_path / "scope-key"
    secret.write_text("s" * 40 + "\n", encoding="utf-8")
    config = tmp_path / "runtime.json"
    config.write_text(
        '{"schema_version": 1, "secrets": {"scope_secret_file": "'
        + str(secret)
        + '"}, "harness": {}}',
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "harness_secret_file.py"
    env = {key: value for key, value in os.environ.items()
           if key not in {"TEST_ATELIER_SCOPE_SECRET", "TEST_ATELIER_SCOPE_SECRET_FILE",
                          "TEST_ATELIER_HARNESS_TOKEN", "TEST_ATELIER_HARNESS_TOKEN_FILE"}}
    result = subprocess.run([sys.executable, str(script), "--config", str(config)],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
