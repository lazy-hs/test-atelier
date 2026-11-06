#!/usr/bin/env python3
"""Create the protected JSON runtime configuration for macOS, Windows and Linux."""

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
CONFIG = ROOT / "config" / "runtime.json"


def _defaults() -> dict[str, Any]:
    home = Path.home()
    sibling = ROOT.parent
    harness_default = sibling / "deepseek-harness"
    extensions_default = sibling / "test-atelier-harness-extensions"
    skills_default = sibling / "qa-doc-skills"
    return {
        "schema_version": 1,
        "paths": {
            "harness_root": str(harness_default),
            "extensions_root": str(extensions_default),
            "skill_root": str(skills_default),
            "dsh_home": str(home / ".dsh"),
            "scope_secret_file": str(home / ".config/test-atelier/harness-scope-secret"),
            "task_root": str(home / ".local/state/test-atelier/harness-tasks"),
            "session_root": str(home / ".dsh/sessions"),
            "log_dir": str(home / ".local/state/test-atelier"),
            "worker_heartbeat": str(home / ".local/state/test-atelier/worker-heartbeat"),
            "projector_heartbeat": str(home / ".local/state/test-atelier/harness-projector-heartbeat"),
        },
        "server": {"api_port": 8000, "web_port": 5173},
        "application": {
            "environment": "development",
            "release": "",
            "data_dir": str(ROOT / "data"),
            "require_migrations": True,
            "migration_head": "20261047_auth_session_revocations",
            "migration_timeout": 120,
            "shutdown_timeout": 25,
        },
        "database": {
            "engine": "sqlite",
            "url": "sqlite:///./data/ai_studio.db",
            "host": "db",
            "port": 5432,
            "name": "ai_studio",
            "user": "ai_studio",
            "password_file": "",
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
        },
        "harness": {
            "port": 8787,
            "bind_host": "127.0.0.1",
            "dsh_command": "node",
            "dsh_args": ["--import", "tsx/esm", "apps/cli/src/bin.ts", "--profile", "test-atelier", "--json"],
            "input_mode": "stdin",
            "protocol": "headless",
            "confirmation_transport": "none",
            "harness_only": True,
            "task_timeout_ms": 900000,
            "staged_task_timeout_ms": 7200000,
            "staged_upload_timeout_ms": 900000,
            "max_active_tasks": 4,
            "max_request_bytes": 8388608,
            "max_response_bytes": 16777216,
            "max_output_bytes": 16777216,
            "max_event_frame_bytes": 15728640,
            "reduce_batch_chars": 48000,
            "reduce_batch_size": 16,
            "chunk_chars": 12000,
            "chunk_overlap_chars": 400,
            "max_chunks": 128,
            "projector_enabled": True,
            "projector_poll_seconds": 1,
            "projector_lease_seconds": 120,
            "projector_max_events": 256,
            "projector_heartbeat_seconds": 5,
            "web_shell": {
                "enabled": False,
                "member_access": False,
                "restricted_profile": True,
                "port": 8765,
                "gateway_port": 8091,
            },
            "url": "http://127.0.0.1:8787",
        },
        "model": {
            "api_base": "http://code.ugreencloud.com:8000/v1",
            "model": "ugreen-ai-model",
            "max_tokens": 32768,
            "api_key_file": str(home / ".config/test-atelier/harness-model-api-key"),
        },
        "security": {
            "session_max_age": 28800,
            "login_failure_limit": 8,
            "login_failure_window_seconds": 60,
            "trust_proxy_headers": False,
            "cookie_secure": False,
            "metrics_enabled": True,
            "metrics_token_file": "",
            "session_secret_file": str(home / ".config/ai-studio/session-secret"),
        },
        "limits": {
            "max_request_bytes": 26214400,
            "max_document_bytes": 104857600,
            "max_document_request_bytes": 115343360,
            "skill_script_timeout": 120,
        },
        "worker": {
            "external": False,
            "poll_seconds": 1,
            "heartbeat_seconds": 5,
            "lease_seconds": 900,
            "max_submitted_jobs": 100,
            "error_log_interval_seconds": 30,
        },
        "logging": {"level": "INFO", "format": "json", "include_traceback": False},
        "ai": {
            "remote_enabled": False,
            "remote_url": "",
            "remote_model": "",
            "codex_enabled": False,
            "codex_command": "codex",
            "codex_model": "",
            "claude_enabled": False,
            "claude_command": "claude",
            "claude_model": "",
            "cli_timeout": 600,
            "cli_test_timeout": 45,
        },
        "test_atelier": {
            "admin_user": "admin",
            "session_secret_file": str(home / ".config/ai-studio/session-secret"),
            "admin_password_file": str(home / ".config/ai-studio/admin-password"),
        },
        "secrets": {"scope_secret_file": str(home / ".config/test-atelier/harness-scope-secret")},
    }


def _merge(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return defaults
    result = defaults.copy()
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **item}
        else:
            result[key] = item
    return result


def _prompt(label: str, current: str, *, secret: bool = False) -> str:
    if current:
        answer = getpass.getpass(f"{label}（已配置，回车保留）：") if secret else input(f"{label} [{current}]：")
        return current if not answer else answer.strip()
    answer = getpass.getpass(f"{label}：") if secret else input(f"{label}：")
    return answer.strip()


def _write_secret(path_value: str, value: str, *, minimum: int, label: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} 文件必须使用绝对路径")
    if len(value) < minimum:
        raise ValueError(f"{label} 至少需要 {minimum} 个字符")
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
    return str(path)


def _read_secret(path_value: str) -> str:
    path = Path(path_value).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def main() -> int:
    config = _defaults()
    if CONFIG.exists():
        try:
            config = _merge(config, json.loads(CONFIG.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"无法读取配置文件 {CONFIG}: {exc}", file=sys.stderr)
            return 2

    paths = config["paths"]
    paths["harness_root"] = _prompt("DeepSeek Harness 路径", str(paths["harness_root"]))
    paths["extensions_root"] = _prompt("Harness 扩展路径", str(paths["extensions_root"]))
    paths["skill_root"] = _prompt("qa-doc-skills 路径", str(paths["skill_root"]))
    model = config["model"]
    model["api_base"] = _prompt("模型 API Base", str(model["api_base"]))
    model["model"] = _prompt("模型名称", str(model["model"]))
    key_file = Path(str(model.get("api_key_file", ""))).expanduser()
    if not key_file.is_absolute():
        print("模型 API Key 文件必须使用绝对路径", file=sys.stderr)
        return 2
    existing_key = ""
    try:
        existing_key = key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    api_key = _prompt("已经轮换的新模型 API Key", existing_key, secret=True)
    if len(api_key) < 16:
        print("模型 API Key 至少需要 16 个字符", file=sys.stderr)
        return 2
    key_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_file.parent, 0o700)
    fd, temporary_key = tempfile.mkstemp(prefix=".model-key.", dir=key_file.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(api_key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_key, key_file)
        os.chmod(key_file, 0o600)
    finally:
        Path(temporary_key).unlink(missing_ok=True)
    model["api_key_file"] = str(key_file)

    atelier = config["test_atelier"]
    atelier["admin_user"] = _prompt("Test Atelier 管理员用户名", str(atelier.get("admin_user", "admin"))) or "admin"
    admin_file = str(atelier.get("admin_password_file", Path.home() / ".config/ai-studio/admin-password"))
    admin_password = _prompt("Test Atelier 管理员密码", _read_secret(admin_file), secret=True)
    try:
        atelier["admin_password_file"] = _write_secret(admin_file, admin_password, minimum=6, label="管理员密码")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    session_file = str(atelier.get("session_secret_file", Path.home() / ".config/ai-studio/session-secret"))
    session_secret = _read_secret(session_file) or secrets.token_urlsafe(48)
    try:
        atelier["session_secret_file"] = _write_secret(session_file, session_secret, minimum=32, label="会话密钥")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    secret_config = config.setdefault("secrets", {})
    scope_file = str(secret_config.get("scope_secret_file", Path.home() / ".config/test-atelier/harness-scope-secret"))
    scope = _read_secret(scope_file) or secrets.token_urlsafe(48)
    try:
        secret_config["scope_secret_file"] = _write_secret(scope_file, scope, minimum=32, label="Harness 作用域密钥")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # Migrate older generated configurations without carrying plaintext
    # credentials forward into the new JSON file.
    atelier.pop("admin_password", None)
    atelier.pop("session_secret", None)
    secret_config.pop("scope_secret", None)
    config["schema_version"] = 1

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runtime.", suffix=".tmp", dir=CONFIG.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG)
        os.chmod(CONFIG, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"配置已写入：{CONFIG}")
    print("配置文件权限：600；模型 Key 单独保存在配置指定的 600 权限文件中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
