#!/usr/bin/env python3
"""Create a config-file-only production bundle for Docker Compose.

The generated JSON is safe to commit only after removing the local file; the
actual runtime and Secret files are ignored by Git and should be provisioned
on the deployment host.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / "runtime.example.json"
OUTPUT = ROOT / "config" / "runtime.json"
SECRET_DIR = ROOT / "config" / "secrets"


def write_secret(path: Path, value: str, minimum: int) -> None:
    if len(value) < minimum or any(char in value for char in "\r\n\x00"):
        raise ValueError(f"secret is shorter than {minimum} characters: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prompt_or_generate(label: str, path: Path, minimum: int, *, prompt: bool = False) -> str:
    if path.exists():
        current = path.read_text(encoding="utf-8").strip()
        if len(current) >= minimum:
            return current
    value = getpass.getpass(f"{label}（回车自动生成）：") if prompt else ""
    value = value.strip() or secrets.token_urlsafe(max(32, minimum))
    write_secret(path, value, minimum)
    return value


def main() -> int:
    if not EXAMPLE.is_file():
        print(f"missing template: {EXAMPLE}", file=sys.stderr)
        return 2
    document: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)

    application = document.setdefault("application", {})
    application.update({"environment": "production", "data_dir": "/app/data", "require_migrations": True})
    paths = document.setdefault("paths", {})
    paths.update({
        "harness_root": "/opt/deepseek-harness",
        "extensions_root": "/opt/test-atelier-harness-extensions",
        "skill_root": "/app/qa-doc-skills",
        "dsh_home": "/home/app/.dsh",
        "task_root": "/app/data/harness-tasks",
        "session_root": "/home/app/.dsh/sessions",
        "log_dir": "/app/data/logs",
        "worker_heartbeat": "/app/data/.worker-heartbeat",
        "projector_heartbeat": "/app/data/.harness-projector-heartbeat",
    })
    document.setdefault("database", {}).update({"engine": "postgresql", "url": "", "password_file": "/run/secrets/postgres_password"})
    document.setdefault("security", {}).update({
        "cookie_secure": True,
        "trust_proxy_headers": True,
        "session_secret_file": "/run/secrets/atelier_session_secret",
        "metrics_token_file": "/run/secrets/atelier_metrics_token",
    })
    document.setdefault("test_atelier", {}).update({
        "session_secret_file": "/run/secrets/atelier_session_secret",
        "admin_password_file": "/run/secrets/atelier_admin_password",
    })
    document.setdefault("secrets", {}).update({"scope_secret_file": "/run/secrets/harness_scope_secret"})
    document.setdefault("harness", {}).update({
        "url": "http://harness:8787",
        "token_file": "/run/secrets/harness_host_api_token",
    })
    document.setdefault("model", {}).update({"api_key_file": "/run/secrets/harness_model_api_key"})

    generated = {
        "postgres_password": ("PostgreSQL 密码", 16, False),
        "atelier_session_secret": ("浏览器会话密钥", 32, False),
        "atelier_admin_password": ("初始管理员密码", 6, True),
        "atelier_metrics_token": ("指标令牌", 16, False),
        "harness_scope_secret": ("Harness 作用域密钥", 32, False),
        "harness_host_api_token": ("Harness Host Token", 16, False),
        "model_api_key": ("模型 API Key", 16, True),
    }
    for name, (label, minimum, prompt) in generated.items():
        prompt_or_generate(label, SECRET_DIR / name, minimum, prompt=prompt)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runtime.", suffix=".tmp", dir=OUTPUT.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, OUTPUT)
        os.chmod(OUTPUT, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"production runtime written: {OUTPUT}")
    print(f"secret files written: {SECRET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
