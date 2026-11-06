"""Read the Harness scope secret from an explicit, bounded deployment file."""

from __future__ import annotations

import os
import sys
import argparse
import json
from pathlib import Path
from typing import Mapping

try:
    from .safe_secret_file import read_secret_bytes
except ImportError:
    from safe_secret_file import read_secret_bytes


def _secret(values: Mapping[str, str], name: str, minimum: int) -> str:
    inline = values.get(name, "").strip()
    filename = values.get(f"{name}_FILE", "").strip()
    if not filename:
        return inline
    if inline or not Path(filename).is_absolute():
        raise ValueError("Harness secret requires one absolute file path")
    path = Path(filename)
    try:
        content = read_secret_bytes(path)
        content = content.removesuffix(b"\r\n").removesuffix(b"\n")
        if not (minimum <= len(content) <= 4096) or content != content.strip() or any(c in content for c in (b"\r", b"\n", b"\x00")):
            raise ValueError("Harness secret file is invalid")
        return content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Harness secret file is unavailable") from exc


def scope_secret(values: Mapping[str, str] | None = None) -> str:
    return _secret(os.environ if values is None else values, "TEST_ATELIER_SCOPE_SECRET", 32)


def host_token(values: Mapping[str, str] | None = None) -> str:
    return _secret(os.environ if values is None else values, "TEST_ATELIER_HARNESS_TOKEN", 16)


def _install_runtime_config(filename: str) -> None:
    """Load file-backed secret paths for launcher checks without printing secrets."""
    path = Path(filename).expanduser().resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or int(document.get("schema_version", 0)) != 1:
        raise ValueError("unsupported runtime config schema")
    try:
        from api.runtime_config import environment_from_config
    except ImportError:
        # Keep this script usable when invoked from a copied deployment tree.
        values = {}
        secrets = document.get("secrets", {})
        harness = document.get("harness", {})
        if isinstance(secrets, dict) and secrets.get("scope_secret_file"):
            values["TEST_ATELIER_SCOPE_SECRET_FILE"] = str(Path(secrets["scope_secret_file"]).expanduser())
        if isinstance(harness, dict) and harness.get("token_file"):
            values["TEST_ATELIER_HARNESS_TOKEN_FILE"] = str(Path(harness["token_file"]).expanduser())
    else:
        values = environment_from_config(document, load_model_key=False)
    for name in ("TEST_ATELIER_SCOPE_SECRET_FILE", "TEST_ATELIER_HARNESS_TOKEN_FILE"):
        value = values.get(name, "")
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", type=Path, help="load file-backed secret paths from runtime.json")
        args = parser.parse_args()
        if args.config:
            _install_runtime_config(str(args.config))
        if len(scope_secret()) < 32:
            raise ValueError("missing Harness scope secret")
        token = host_token()
        if token and len(token) < 16:
            raise ValueError("invalid Harness Host API token")
    except (OSError, UnicodeError, ValueError, TypeError):
        print("Harness 作用域密钥或 Host 凭据无效或不可读取", file=sys.stderr)
        raise SystemExit(78)
