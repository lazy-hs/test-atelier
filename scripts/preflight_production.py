#!/usr/bin/env python3
"""Validate production prerequisites before starting Test Atelier.

The check is intentionally local and side-effect-light: it validates secrets,
database/worker topology, the external skill repository, writable storage and
enabled CLI executables without contacting a model or database server. The
``--compose`` operator mode additionally checks the local Docker Compose CLI
and validates compose interpolation without starting containers.
"""

from __future__ import annotations

import argparse
import json
import importlib.metadata
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


PLACEHOLDERS = {
    "", "change-me", "replace-with-at-least-32-random-characters", "replace-with-secret",
    "replace-with-strong-admin-password", "replace-with-db-password-123", "管理员密码（至少 6 位）",
}
_MIGRATION_REVISION = re.compile(r"^revision\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _result(results: list[Check], name: str, status: str, detail: str) -> None:
    results.append(Check(name, status, detail))


def _check_secret(results: list[Check], env: dict[str, str], name: str, minimum: int, *, required: bool = True) -> None:
    value = env.get(name, "").strip()
    if not value or value.casefold() in {item.casefold() for item in PLACEHOLDERS}:
        _result(results, name, "fail" if required else "warn", "未设置或仍是示例值")
    elif len(value) < minimum:
        _result(results, name, "fail", f"长度不足 {minimum} 个字符")
    else:
        _result(results, name, "ok", "已设置")


def _check_compose_password_syntax(results: list[Check], value: str) -> None:
    """Reject characters that would make the interpolated DB URL ambiguous."""
    if value and any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        _result(results, "POSTGRES_PASSWORD", "fail", "必须只包含字母、数字、点、下划线或连字符，以便安全插入 DATABASE_URL")


def _check_writable_directory(results: list[Check], name: str, raw_path: str) -> None:
    path = Path(raw_path).expanduser()
    try:
        if path.is_symlink():
            raise OSError("目录根路径不允许是符号链接")
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=path, delete=True):
            pass
    except OSError as exc:
        _result(results, name, "fail", f"目录不可写：{exc}")
    else:
        _result(results, name, "ok", str(path))


def _check_numeric(results: list[Check], values: dict[str, str], name: str, default: str,
                   minimum: float, *, integer: bool = False) -> None:
    raw = values.get(name, default).strip()
    try:
        parsed = int(raw) if integer else float(raw)
        valid = math.isfinite(parsed) and parsed >= minimum
    except ValueError:
        valid = False
    detail = raw if valid else f"必须是至少 {minimum:g} 的数字"
    _result(results, name, "ok" if valid else "fail", detail)


def _check_timeout_order(results: list[Check], values: dict[str, str], short_name: str,
                         short_default: str, long_name: str, long_default: str) -> None:
    """Reject a diagnostic timeout that can outlive the operation it checks."""
    try:
        short_value = float(values.get(short_name, short_default).strip())
        long_value = float(values.get(long_name, long_default).strip())
    except (AttributeError, TypeError, ValueError):
        return
    if not math.isfinite(short_value) or not math.isfinite(long_value):
        return
    if short_value > long_value:
        _result(results, f"{short_name}_ORDER", "fail",
                f"{short_name} 不能大于 {long_name}")
    else:
        _result(results, f"{short_name}_ORDER", "ok", "超时顺序有效")


def _check_cryptography_baseline(results: list[Check], environment: str) -> None:
    """Surface the patched crypto baseline before a process accepts traffic."""
    try:
        raw_version = importlib.metadata.version("cryptography")
    except importlib.metadata.PackageNotFoundError:
        _result(results, "cryptography", "fail", "未安装 cryptography 依赖")
        return
    match = re.match(r"^(\d+)\.(\d+)", raw_version)
    if not match:
        _result(results, "cryptography", "fail", "无法识别 cryptography 版本")
        return
    major = int(match.group(1))
    if major >= 50:
        _result(results, "cryptography", "ok", raw_version)
        return
    # Intel macOS has no supported patched binary wheel in the current local
    # toolchain. Keep this development-only limitation visible without making
    # ordinary local checks unusable; production Linux/Apple Silicon must fail.
    if sys.platform == "darwin" and platform.machine().lower() in {"x86_64", "i386"}:
        level = "fail" if environment.casefold() == "production" else "warn"
        _result(results, "cryptography", level,
                f"{raw_version} 受已知 advisories 影响；Intel macOS 仅限开发，生产请使用 Linux/Apple Silicon 的 50+ 版本")
        return
    _result(results, "cryptography", "fail",
            f"{raw_version} 低于生产最低版本 50；请升级 cryptography")


def _known_migration_revisions() -> set[str]:
    """Read revision ids without importing application dependencies."""
    versions = Path(__file__).resolve().parents[1] / "api" / "migrations" / "versions"
    revisions: set[str] = set()
    try:
        paths = versions.glob("*.py")
        for path in paths:
            try:
                match = _MIGRATION_REVISION.search(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if match:
                revisions.add(match.group(1))
    except OSError:
        return set()
    return revisions


def run_preflight(env: dict[str, str] | None = None, *, compose: bool = False,
                  require_admin_password: bool = True,
                  require_http_security: bool = True) -> list[Check]:
    """Validate deployment settings; worker-only callers can skip HTTP secrets."""
    values = dict(os.environ if env is None else env)
    results: list[Check] = []
    environment = values.get("AI_STUDIO_ENV", "production" if compose else "development").strip()
    if compose and environment != "production":
        _result(results, "AI_STUDIO_ENV", "warn", "Compose 日志环境建议设置为 production")
    else:
        _result(results, "AI_STUDIO_ENV", "ok", environment or "未设置")
    release = values.get("AI_STUDIO_RELEASE", "").strip()
    if (compose or environment.casefold() == "production") and not release:
        # A missing release does not make the process unsafe, but it removes
        # the ability to correlate a health probe and log line with a build.
        _result(results, "AI_STUDIO_RELEASE", "warn", "未设置；建议填写 commit SHA 或构建号以支持发布关联")
    elif release and len(release) > 128:
        _result(results, "AI_STUDIO_RELEASE", "fail", "长度不能超过 128 个字符")
    else:
        _result(results, "AI_STUDIO_RELEASE", "ok" if release else "warn", release or "开发环境未设置")
    _check_cryptography_baseline(results, environment)
    _check_secret(results, values, "AI_STUDIO_SESSION_SECRET", 32)
    # The API creates and authenticates the first administrator, while the
    # independent worker only needs the session secret for encrypted settings.
    # Avoid copying the administrator password into the worker container.
    _check_secret(results, values, "AI_STUDIO_ADMIN_PASSWORD", 6, required=require_admin_password)
    if compose:
        _check_secret(results, values, "POSTGRES_PASSWORD", 12)
        _check_compose_password_syntax(results, values.get("POSTGRES_PASSWORD", "").strip())

    database_url = values.get("DATABASE_URL", "sqlite:///./data/ai_studio.db")
    scheme = urlparse(database_url).scheme
    external_worker = _truthy(values.get("AI_STUDIO_EXTERNAL_WORKER", "0"))
    if compose and not external_worker:
        _result(results, "AI_STUDIO_EXTERNAL_WORKER", "fail", "Compose 生产部署必须启用独立 worker")
    elif compose:
        _result(results, "AI_STUDIO_EXTERNAL_WORKER", "ok", "独立 worker 已启用")
    if external_worker and scheme.startswith("sqlite"):
        _result(results, "DATABASE_URL", "fail", "独立 worker 不应与 SQLite 共享生产队列，请使用 PostgreSQL")
    elif scheme not in {"sqlite", "sqlite+pysqlite", "postgresql", "postgresql+psycopg", "postgres"}:
        _result(results, "DATABASE_URL", "fail", f"不支持的数据库协议：{scheme or '空'}")
    else:
        _result(results, "DATABASE_URL", "ok", "数据库协议有效")

    # These values are consumed while importing the SQLAlchemy engine and
    # request limiter. Validate them before a deployment attempts to boot;
    # otherwise a typo would only surface as an unstructured import traceback.
    _check_numeric(results, values, "DB_POOL_SIZE", "5", 1, integer=True)
    _check_numeric(results, values, "DB_MAX_OVERFLOW", "10", 0, integer=True)
    _check_numeric(results, values, "DB_POOL_TIMEOUT", "30", 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_SQLITE_TIMEOUT", "30", 1)
    _check_numeric(results, values, "AI_STUDIO_LOGIN_FAILURE_LIMIT", "8", 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_LOGIN_FAILURE_WINDOW_SECONDS", "60", 1)

    skill_root = Path(values.get("TEST_ATELIER_SKILL_ROOT", "")).expanduser()
    required_skill_files = (
        skill_root / "AGENTS.md",
        skill_root / "skills" / "hub" / "SKILL.md",
        skill_root / "skills" / "requirement-analysis" / "SKILL.md",
        skill_root / "skills" / "test-cases" / "SKILL.md",
    )
    missing_skill_files = [str(path) for path in required_skill_files if not path.is_file()]
    if missing_skill_files:
        _result(results, "TEST_ATELIER_SKILL_ROOT", "fail", "缺少技能工作流文件：" + ", ".join(missing_skill_files))
    else:
        _result(results, "TEST_ATELIER_SKILL_ROOT", "ok", str(skill_root))

    _check_writable_directory(results, "AI_STUDIO_DATA_DIR", values.get("AI_STUDIO_DATA_DIR", "./data"))
    require_migrations = _truthy(values.get(
        "AI_STUDIO_REQUIRE_MIGRATIONS", "1" if environment.casefold() == "production" else "0"
    ))
    if environment.casefold() == "production" and not require_migrations:
        _result(results, "AI_STUDIO_REQUIRE_MIGRATIONS", "fail",
                "生产环境必须启用显式 Alembic 迁移，不能回退到隐式 create_all")
    else:
        _result(results, "AI_STUDIO_REQUIRE_MIGRATIONS", "ok" if require_migrations else "warn",
                "已启用" if require_migrations else "开发环境允许隐式初始化")
    migration_head = values.get("AI_STUDIO_MIGRATION_HEAD", "20261028_codex_sessions").strip()
    known_revisions = _known_migration_revisions()
    if not migration_head:
        _result(results, "AI_STUDIO_MIGRATION_HEAD", "fail", "未设置迁移 head")
    elif known_revisions and migration_head not in known_revisions:
        _result(results, "AI_STUDIO_MIGRATION_HEAD", "fail", f"仓库不存在迁移 revision：{migration_head}")
    else:
        _result(results, "AI_STUDIO_MIGRATION_HEAD", "ok", migration_head)
    migration_timeout = values.get("AI_STUDIO_MIGRATION_TIMEOUT", "120").strip()
    try:
        parsed_timeout = float(migration_timeout)
        valid_timeout = math.isfinite(parsed_timeout) and parsed_timeout >= 1
    except ValueError:
        valid_timeout = False
    _result(results, "AI_STUDIO_MIGRATION_TIMEOUT", "ok" if valid_timeout else "fail",
            migration_timeout if valid_timeout else "必须是至少 1 秒的数字")
    _check_numeric(results, values, "AI_STUDIO_MAX_REQUEST_BYTES", "26214400", 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_SESSION_MAX_AGE", "28800", 300, integer=True)
    _check_numeric(results, values, "AI_STUDIO_MAX_SUBMITTED_JOBS", "100", 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_SHUTDOWN_TIMEOUT", "25", 0)
    _check_numeric(results, values, "AI_STUDIO_WORKER_POLL_SECONDS", "1", 0.1)
    _check_numeric(results, values, "AI_STUDIO_WORKER_HEARTBEAT_SECONDS", "5", 1)
    _check_numeric(results, values, "AI_STUDIO_WORKER_LEASE_SECONDS", "900", 30)
    _check_numeric(results, values, "AI_STUDIO_WORKER_ERROR_LOG_INTERVAL_SECONDS", "30", 1)
    _check_numeric(results, values, "AI_STUDIO_BACKUP_TIMEOUT", "120", 1)
    _check_numeric(results, values, "AI_STUDIO_BACKUP_MAX_UNCOMPRESSED_BYTES", str(8 * 1024 * 1024 * 1024), 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_CLI_TIMEOUT", "600", 1, integer=True)
    _check_numeric(results, values, "AI_STUDIO_CLI_TEST_TIMEOUT", "45", 5, integer=True)
    _check_timeout_order(results, values, "AI_STUDIO_CLI_TEST_TIMEOUT", "45",
                         "AI_STUDIO_CLI_TIMEOUT", "600")
    _check_numeric(results, values, "AI_STUDIO_SKILL_SCRIPT_TIMEOUT", "120", 1)
    _check_timeout_order(results, values, "AI_STUDIO_CLI_TIMEOUT", "600",
                         "AI_STUDIO_WORKER_LEASE_SECONDS", "900")
    _check_timeout_order(results, values, "AI_STUDIO_SKILL_SCRIPT_TIMEOUT", "120",
                         "AI_STUDIO_WORKER_LEASE_SECONDS", "900")

    for enabled_name, command_name, default_command, label in (
        ("AI_STUDIO_CODEX_ENABLED", "AI_STUDIO_CODEX_COMMAND", "codex", "Codex CLI"),
        ("AI_STUDIO_CLAUDE_ENABLED", "AI_STUDIO_CLAUDE_COMMAND", "claude", "Claude Code"),
    ):
        if not _truthy(values.get(enabled_name, "0")):
            _result(results, label, "ok", "未启用")
            continue
        # Keep preflight defaults aligned with the provider adapter so enabling
        # a CLI without an explicit command does not fail for an empty value.
        command = values.get(command_name, default_command).strip() or default_command
        executable = Path(command).expanduser() if Path(command).is_absolute() else None
        found = executable if executable and executable.is_file() and os.access(executable, os.X_OK) else shutil.which(command)
        if not found:
            _result(results, label, "fail", f"找不到可执行文件：{command or '(空)'}")
        else:
            _result(results, label, "ok", str(found))

    production_security = require_http_security and (compose or environment == "production")
    if values.get("COOKIE_SECURE", "0").strip() != "1":
        _result(results, "COOKIE_SECURE", "fail" if production_security else "warn",
                "生产环境必须启用 HTTPS Cookie；本机 HTTP 验收请使用 development 环境")
    else:
        _result(results, "COOKIE_SECURE", "ok", "已启用")
    metrics_token = values.get("METRICS_TOKEN", "").strip()
    if not metrics_token:
        _result(results, "METRICS_TOKEN", "fail" if production_security else "warn",
                "生产环境必须为指标端点设置访问令牌")
    elif len(metrics_token) < 16:
        _result(results, "METRICS_TOKEN", "fail" if production_security else "warn",
                "指标访问令牌至少需要 16 个字符")
    else:
        _result(results, "METRICS_TOKEN", "ok", "已设置")
    log_format = values.get("LOG_FORMAT", "json").strip().lower()
    if log_format != "json":
        _result(results, "LOG_FORMAT", "fail", "生产日志应使用 json，便于集中检索和字段脱敏")
    else:
        _result(results, "LOG_FORMAT", "ok", "JSON 结构化日志")
    log_level = values.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        _result(results, "LOG_LEVEL", "fail", "必须是 CRITICAL、ERROR、WARNING、INFO 或 DEBUG")
    else:
        _result(results, "LOG_LEVEL", "ok", log_level)
    if _truthy(values.get("LOG_INCLUDE_TRACEBACK", "0")):
        _result(results, "LOG_INCLUDE_TRACEBACK", "warn", "已开启脱敏 traceback，仅适合临时诊断")
    else:
        _result(results, "LOG_INCLUDE_TRACEBACK", "ok", "默认隐藏 traceback")
    return results


def check_compose_runtime(env: dict[str, str] | None = None) -> Check:
    """Verify Docker Compose and the repository topology before deployment.

    Configuration checks intentionally remain dependency-free and are reused by
    API/worker startup.  The operator-facing ``--compose`` command performs
    bounded CLI and interpolation checks so a missing runtime or a broken
    required variable is reported before ``docker compose up`` starts a partial
    deployment.  Command output is intentionally discarded because it can
    contain interpolated credentials or host paths.
    """
    docker = shutil.which("docker")
    if not docker:
        return Check("DOCKER_COMPOSE", "fail", "找不到 Docker CLI；请安装 Docker Desktop 或 Docker Engine")
    environment = dict(os.environ if env is None else env)
    try:
        version = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("DOCKER_COMPOSE", "fail", f"Docker Compose 不可用：{type(exc).__name__}")
    if version.returncode != 0:
        return Check("DOCKER_COMPOSE", "fail", "Docker Compose 插件不可用")
    root = Path(__file__).resolve().parents[1]
    try:
        config = subprocess.run(
            [docker, "compose", "config", "--quiet"],
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("DOCKER_COMPOSE", "fail", f"Docker Compose 配置检查失败：{type(exc).__name__}")
    if config.returncode != 0:
        return Check("DOCKER_COMPOSE", "fail", "Docker Compose 配置无法解析；请检查必填变量和 compose.yaml")
    return Check("DOCKER_COMPOSE", "ok", "Docker Compose CLI 和配置均可用")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", action="store_true", help="按 Docker Compose 生产要求额外检查 PostgreSQL 密码")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON 检查结果")
    output.add_argument("--jsonl", action="store_true", help="输出一条符合日志契约的单行 JSON 事件")
    args = parser.parse_args(argv)
    try:
        results = run_preflight(compose=args.compose)
        if args.compose:
            results.append(check_compose_runtime())
    except KeyboardInterrupt:
        if args.jsonl:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "schema_version": 1, "level": "WARNING", "service": "test-atelier",
                "environment": os.getenv("AI_STUDIO_ENV", "production" if args.compose else "development")[:32],
                "event": "production.preflight.cancelled", "request_id": "-",
                "compose": args.compose, "check_count": 0, "failure_count": 0,
                "warning_count": 0, "failed_checks": [], "checks": [],
            }
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print("生产前置检查已取消")
        return 130
    failures = [item.name for item in results if item.status == "fail"]
    warnings = sum(item.status == "warn" for item in results)
    if args.jsonl:
        # Keep operator diagnostics safe for direct ingestion: include check
        # names and statuses, but not free-form details such as host paths.
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "schema_version": 1,
            "level": "ERROR" if failures else "INFO",
            "service": "test-atelier",
            "environment": os.getenv("AI_STUDIO_ENV", "production" if args.compose else "development")[:32],
            "event": "production.preflight.failed" if failures else "production.preflight.completed",
            "request_id": "-",
            "compose": args.compose,
            "check_count": len(results),
            "failure_count": len(failures),
            "warning_count": warnings,
            "failed_checks": failures[:32],
            "checks": [{"name": item.name, "status": item.status} for item in results],
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif args.as_json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"[{item.status.upper():4}] {item.name}: {item.detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
