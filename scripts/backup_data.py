#!/usr/bin/env python3
"""Create and verify encrypted-storage-independent Test Atelier backups.

The archive contains a consistent SQLite snapshot (or a ``pg_dump`` custom
dump) plus the uploaded-file directory.  Secrets are never copied from the
environment into the manifest.  Restore is deliberately explicit and refuses
to overwrite an existing destination unless ``--force`` is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "sqlite:///./data/ai_studio.db"
ARCHIVE_PREFIX = "test-atelier-"
TRANSIENT_DATA_FILES = {".worker-heartbeat", ".worker-heartbeat.tmp"}
MANIFEST_FORMAT = 1
MAX_ARCHIVE_MEMBERS = 100_000
MAX_MANIFEST_PATH_LENGTH = 1_024
# Keep restore/verify bounded against decompression bombs or an accidentally
# pointed-at data volume. The limit is configurable for larger installations.
DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_ERROR_MESSAGE_LENGTH = 64 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_URL_PATTERN = re.compile(
    r"(?i)\b((?:postgres(?:ql)?(?:\+\w+)?|mysql(?:\+\w+)?|redis(?:s)?|mongodb(?:\+srv)?)://[^:\s/@]+:)([^@\s]+)(@)"
)
_ERROR_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization|cookie|set[-_ ]?cookie|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|password|secret|token|api[-_ ]?key)\s*[:=]\s*([^\s,;&}]+)"
)
_ERROR_ENV_PATTERN = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AZURE_OPENAI_API_KEY|AWS_ACCESS_KEY_ID|"
    r"AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|GOOGLE_API_KEY|HF_TOKEN|GITHUB_TOKEN|NPM_TOKEN|"
    r"DOCKER_AUTH_CONFIG|DATABASE_URL|POSTGRES_PASSWORD|METRICS_TOKEN|AI_STUDIO_SESSION_SECRET|"
    r"AI_STUDIO_ADMIN_PASSWORD)\s*=\s*[^\s,;&}]+"
)
_ERROR_COOKIE_PATTERN = re.compile(
    r"(?i)((?:set[-_ ]?cookie|cookie)\s*[:=]\s*)[^\r\n]+"
)
_ERROR_JSON_PATTERN = re.compile(
    r"(?i)([\"'](?:authorization|cookie|set[-_ ]?cookie|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|password|secret|token|api[-_ ]?key)[\"']\s*:\s*)([\"'][^\"'\r\n]*[\"']|[^,}\s]+)"
)
_ERROR_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|cookie|password|secret|token|api[-_ ]?key)=)[^&#\s]+"
)
_ERROR_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ERROR_OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_ERROR_AWS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_ERROR_GITHUB_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b")
_ERROR_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_ERROR_PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----.*?-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----",
    re.DOTALL,
)
_ERROR_PRIVATE_KEY_MARKER_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----")
_RELEASE_PATTERN = re.compile(r"[^A-Za-z0-9._/@:-]+")


def event_payload(event: str, level: str = "INFO", **fields: object) -> dict[str, object]:
    """Return an operator event with the same envelope as service logs."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "schema_version": 1,
        "level": level,
        "service": "test-atelier",
        "environment": os.getenv("AI_STUDIO_ENV", "development")[:32],
        "event": event,
        "component": "backup",
        # Backup CLI events run outside HTTP and therefore use the shared
        # non-request sentinel used by service startup logs.
        "request_id": "-",
        **_sanitize_event_fields(fields),
    }
    raw_release = os.getenv("AI_STUDIO_RELEASE", "").strip()
    if raw_release:
        payload["release"] = _RELEASE_PATTERN.sub("_", safe_error_message(raw_release)[:128])[:128]
    return payload


def manifest_summary(manifest: dict) -> dict[str, object]:
    """Expose backup metadata without logging every uploaded file name."""
    entries = manifest.get("files") if isinstance(manifest, dict) else []
    return {
        "database_kind": manifest.get("database_kind", "unknown"),
        "schema_revision": manifest.get("schema_revision", "unknown"),
        "data_file_count": manifest.get("data_file_count", 0),
        "file_count": len(entries) if isinstance(entries, list) else 0,
    }


def archive_name(path: Path) -> str:
    """Return only the portable archive name for operator events."""
    return path.name[:255]


def safe_error_message(value: str) -> str:
    """Keep backup diagnostics useful without echoing credentials."""
    value = str(value)[:MAX_ERROR_MESSAGE_LENGTH]
    value = _ERROR_URL_PATTERN.sub(r"\1[REDACTED]\3", value)
    value = _ERROR_COOKIE_PATTERN.sub(r"\1[REDACTED]", value)
    value = _ERROR_JSON_PATTERN.sub(r"\1[REDACTED]", value)
    value = _ERROR_QUERY_PATTERN.sub(r"\1[REDACTED]", value)
    value = _ERROR_ENV_PATTERN.sub(lambda match: f"{match.group(0).split('=', 1)[0]}=[REDACTED]", value)
    value = _ERROR_BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    value = _ERROR_SECRET_PATTERN.sub(r"\1=[REDACTED]", value)
    value = _ERROR_OPENAI_KEY_PATTERN.sub("[REDACTED]", value)
    value = _ERROR_AWS_KEY_PATTERN.sub("[REDACTED]", value)
    value = _ERROR_GITHUB_TOKEN_PATTERN.sub("[REDACTED]", value)
    value = _ERROR_JWT_PATTERN.sub("[REDACTED]", value)
    value = _ERROR_PRIVATE_KEY_BLOCK_PATTERN.sub("[PRIVATE_KEY_REDACTED]", value)
    return _ERROR_PRIVATE_KEY_MARKER_PATTERN.sub("[PRIVATE_KEY_REDACTED]", value)[:500]


_EVENT_FIELDS = {
    "archive", "archive_size", "removed", "removed_count", "duration_ms",
    "database_kind", "schema_revision", "data_file_count", "file_count",
    "target_name", "data_dir_name", "error_type", "message",
}


class _ArgumentParser(argparse.ArgumentParser):
    """Keep malformed backup invocations inside the JSON event contract."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def _sanitize_event_fields(fields: dict[str, object]) -> dict[str, object]:
    """Keep direct backup CLI JSON events within the shared log contract."""
    sanitized: dict[str, object] = {}
    for key, value in fields.items():
        if key not in _EVENT_FIELDS:
            continue
        if isinstance(value, str):
            sanitized[key] = safe_error_message(value)[:256]
        elif isinstance(value, (list, tuple, set)):
            sanitized[key] = [safe_error_message(str(item))[:256] for item in list(value)[:128]]
        elif isinstance(value, float):
            sanitized[key] = value if math.isfinite(value) else None
        elif isinstance(value, (bool, int)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = safe_error_message(str(value))[:256]
    return sanitized


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_private_file(path: Path) -> None:
    """Set mode through a no-following descriptor after publication."""
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags | nofollow | cloexec)
    except (AttributeError, NotImplementedError):
        # The fallback is for platforms without descriptor-level no-following;
        # normal POSIX deployments take the race-safe branch above.
        path.chmod(0o600)
        return
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            path.chmod(0o600)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Persist an atomic rename when directory fsync is supported."""
    flags = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except (AttributeError, NotImplementedError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_path(path: Path) -> None:
    """Remove a staged file or directory without ever following a link."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _best_effort_remove(path: Path) -> None:
    """Clean staging leftovers without masking a completed restore."""
    try:
        _remove_path(path)
    except OSError:
        # The restored targets are already committed at this point.  Leaving
        # a private staging/backup directory is safer than reporting failure
        # after a successful replacement; operators can remove it later.
        pass


def _private_tree(path: Path) -> None:
    """Apply private permissions to every restored directory and file."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"恢复目录不是普通目录：{path}")
    path.chmod(0o700)
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"恢复目录不允许包含符号链接：{item}")
        if item.is_dir():
            item.chmod(0o700)
        elif item.is_file():
            _chmod_private_file(item)
        else:
            raise ValueError(f"恢复目录包含特殊文件：{item}")


def safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def sqlite_path(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"sqlite", "sqlite+pysqlite"}:
        raise ValueError("当前 SQLite 备份只支持 sqlite:// 数据库地址")
    raw = parsed.path
    if not raw or raw in {"/:memory:", "/"} or "mode=memory" in parsed.query:
        raise ValueError("不支持内存 SQLite 数据库备份")
    if raw.startswith("//"):
        raw = raw[1:]
    else:
        raw = raw.lstrip("/")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def database_kind(database_url: str) -> str:
    scheme = urlparse(database_url).scheme
    if scheme in {"sqlite", "sqlite+pysqlite"}:
        return "sqlite"
    if scheme in {"postgresql", "postgresql+psycopg", "postgres"}:
        return "postgresql"
    raise ValueError(f"不支持的数据库地址：{scheme or '空'}")


_PG_QUERY_ENV = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "gssencmode": "PGGSSENCMODE",
    "channel_binding": "PGCHANNELBINDING",
}


def command_timeout() -> float:
    """Bound pg_dump/pg_restore so a broken database cannot hang a backup job."""
    try:
        value = float(os.getenv("AI_STUDIO_BACKUP_TIMEOUT", "120"))
        if not math.isfinite(value):
            raise ValueError
        return max(1.0, value)
    except (TypeError, ValueError):
        return 120.0


def max_archive_uncompressed_bytes() -> int:
    """Bound total extracted backup bytes before any restore extraction."""
    raw = os.getenv(
        "AI_STUDIO_BACKUP_MAX_UNCOMPRESSED_BYTES",
        str(DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ARCHIVE_UNCOMPRESSED_BYTES
    return max(1, value)


def postgres_environment(parsed) -> dict[str, str]:
    """Build libpq environment without putting credentials in argv or logs."""
    environment = os.environ.copy()
    # pg_dump/pg_restore only need the explicit libpq variables below. Do not
    # leak application connection strings or session/provider secrets into the
    # child environment where diagnostics and process inspection may expose
    # them.
    secret_names = {
        "DATABASE_URL", "POSTGRES_PASSWORD", "AI_STUDIO_SESSION_SECRET",
        "AI_STUDIO_ADMIN_PASSWORD", "METRICS_TOKEN", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
    }
    for name in list(environment):
        # Remove all libpq connection overrides, not only obvious secrets.
        # Inherited PGHOST/PGSERVICE/PGPASSWORD could otherwise silently
        # redirect a restore or override the explicit target URL.
        if name.startswith("PG") or name in secret_names or name.startswith("AI_STUDIO_REMOTE_"):
            environment.pop(name, None)
    if parsed.password:
        environment["PGPASSWORD"] = unquote(parsed.password)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        env_name = _PG_QUERY_ENV.get(key.casefold())
        if env_name:
            environment[env_name] = unquote(value)
    return environment


def redacted_url(database_url: str) -> str:
    parsed = urlparse(database_url)
    user = parsed.username or ""
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{user}:REDACTED@{host}" if parsed.password is not None and user else host
    sensitive_query_keys = {"password", "passwd", "secret", "token", "api_key", "apikey", "access_token"}
    query = urlencode([
        (key, "REDACTED" if key.casefold() in sensitive_query_keys else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    if parsed.scheme in {"sqlite", "sqlite+pysqlite"}:
        # ``urlunparse`` collapses the empty SQLite authority from
        # ``sqlite:///relative.db`` to ``sqlite:/relative.db``. Preserve the
        # original three/four-slash form so the manifest remains usable.
        suffix = f"?{query}" if query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{suffix}{fragment}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, query, parsed.fragment))


def sqlite_snapshot(source: Path, target: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(f"SQLite 数据库不存在：{source}")
    source_db = sqlite3.connect(str(source), timeout=10)
    target_db = sqlite3.connect(str(target))
    try:
        source_db.execute("PRAGMA busy_timeout=10000")
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity_check 失败：{result}")
        try:
            revision = target_db.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # A local database created before Alembic was introduced can still
            # be snapshotted safely; record the absence of a schema marker
            # instead of turning a valid backup into an opaque traceback.
            if "no such table" not in str(exc).lower():
                raise
            revision = None
        return revision[0] if revision else "unversioned"
    finally:
        target_db.close()
        source_db.close()


def postgres_dump(database_url: str, target: Path) -> None:
    parsed = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("PostgreSQL 地址缺少主机或数据库名")
    command = [
        "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
        "--file", str(target), "--host", parsed.hostname,
        "--port", str(parsed.port or 5432), "--username", unquote(parsed.username or ""),
        "--dbname", unquote(parsed.path.lstrip("/")),
    ]
    environment = postgres_environment(parsed)
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                check=False, timeout=command_timeout())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pg_dump 执行超时") from exc
    if result.returncode:
        detail = safe_error_message((result.stderr or result.stdout or "pg_dump failed").strip())
        raise RuntimeError(f"pg_dump 失败：{detail}")


def postgres_restore(archive: Path, database_url: str, data_dir: Path, force: bool) -> None:
    """Restore a PostgreSQL custom dump and the uploaded-file directory.

    The target URL is required explicitly because backup manifests redact the
    password.  ``--clean`` is only allowed with ``--force`` so an operator
    cannot accidentally replace a live database with a backup.
    """
    manifest = verify_archive(archive)
    if manifest.get("database_kind") != "postgresql":
        raise ValueError("该归档不是 PostgreSQL 备份")
    if not database_url or database_kind(database_url) != "postgresql":
        raise ValueError("PostgreSQL 恢复必须提供 postgresql 数据库地址")
    if data_dir.is_symlink():
        raise ValueError(f"目标数据目录不允许是符号链接：{data_dir}")
    if data_dir.exists() and not data_dir.is_dir():
        raise ValueError(f"目标数据路径不是目录：{data_dir}")
    if data_dir.exists() and not force:
        raise FileExistsError("目标数据目录已存在；恢复会覆盖数据，请显式传入 --force")
    parsed = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("PostgreSQL 地址缺少主机或数据库名")
    environment = postgres_environment(parsed)
    with tempfile.TemporaryDirectory(prefix="test-atelier-pg-restore-") as temporary:
        stage = Path(temporary)
        extract_verified_archive(archive, stage, manifest)
        (stage / "data").mkdir(parents=True, exist_ok=True)
        dump = stage / "database.dump"
        command = [
            "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
            # Keep DDL/data changes transactional when the target server
            # supports it, so a failed restore does not leave a half-applied
            # schema behind while the uploaded-file tree is rolled back.
            "--single-transaction",
            "--exit-on-error", "--host", parsed.hostname, "--port", str(parsed.port or 5432),
            "--username", unquote(parsed.username or ""), "--dbname", unquote(parsed.path.lstrip("/")), str(dump),
        ]
        if not force:
            raise ValueError("PostgreSQL 恢复必须显式传入 --force")
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        (stage / "data").mkdir(exist_ok=True)
        staged_data_parent = Path(tempfile.mkdtemp(
            prefix=f".{data_dir.name}.restore-", dir=data_dir.parent
        ))
        staged_data = staged_data_parent / data_dir.name
        old_data: Path | None = None
        data_installed = False
        committed = False
        try:
            # Prepare and validate the file tree before touching either
            # destination. Install it first; if pg_restore then fails, the
            # rollback below returns the files to their original state while
            # --single-transaction asks PostgreSQL to roll its changes back.
            shutil.copytree(stage / "data", staged_data)
            _private_tree(staged_data)
            if data_dir.exists() or data_dir.is_symlink():
                old_data = Path(tempfile.mkdtemp(
                    prefix=f".{data_dir.name}.backup-", dir=data_dir.parent
                ))
                old_data.rmdir()
                os.replace(data_dir, old_data)
            os.replace(staged_data, data_dir)
            data_installed = True
            _fsync_directory(data_dir.parent)
            try:
                result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                        check=False, timeout=command_timeout())
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("pg_restore 执行超时") from exc
            if result.returncode:
                detail = safe_error_message((result.stderr or result.stdout or "pg_restore failed").strip())
                raise RuntimeError(f"pg_restore 失败：{detail}")
            committed = True
        except Exception:
            # Restore the file tree when the database command fails or the
            # same-filesystem rename cannot complete. PostgreSQL is asked to
            # provide the matching transaction boundary above.
            if data_installed:
                _remove_path(data_dir)
            if old_data is not None and old_data.exists():
                os.replace(old_data, data_dir)
            _fsync_directory(data_dir.parent)
            raise
        finally:
            if staged_data_parent.exists():
                _best_effort_remove(staged_data_parent)
            if committed and old_data is not None and old_data.exists():
                _best_effort_remove(old_data)


def copy_data(source: Path, target: Path) -> int:
    if not source.exists():
        target.mkdir(parents=True)
        return 0
    if not source.is_dir():
        raise ValueError(f"数据目录不是目录：{source}")
    count = 0
    for item in source.rglob("*"):
        if item.is_symlink():
            # Uploaded artifacts must stay inside the configured data root;
            # following a symlink here could silently copy unrelated secrets.
            raise ValueError(f"数据目录不允许包含符号链接：{item}")
        if item.is_file():
            relative = item.relative_to(source)
            # A restored heartbeat must never make the API report a worker as
            # healthy before a real worker has started and renewed it.
            if relative.as_posix() in TRANSIENT_DATA_FILES:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            count += 1
    return count


def safe_data_root(path: Path) -> Path:
    """Resolve a data root only after rejecting a symlinked root."""
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError(f"数据目录不允许是符号链接：{candidate}")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError(f"数据目录不是目录：{candidate}")
    return candidate.resolve()


def build_manifest(stage: Path, database_url: str, kind: str, revision: str, data_count: int) -> dict:
    files = []
    for item in sorted(stage.rglob("*")):
        if not item.is_file() or item.name == "manifest.json":
            continue
        relative = item.relative_to(stage).as_posix()
        files.append({"path": relative, "size": item.stat().st_size, "sha256": sha256(item)})
    if sum(item["size"] for item in files) > max_archive_uncompressed_bytes():
        raise ValueError("备份解压后大小超过限制")
    return {
        "format": MANIFEST_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_kind": kind,
        "database_url": redacted_url(database_url),
        "schema_revision": revision,
        "data_file_count": data_count,
        "files": files,
    }


def verify_archive(archive: Path) -> dict:
    if archive.is_symlink() or not archive.is_file():
        raise FileNotFoundError(f"备份不存在：{archive}")
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("备份包含过多归档成员")
        if any(not safe_member(member.name) for member in members):
            raise ValueError("备份包含不安全的路径")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("备份包含重复的归档路径")
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("备份不允许包含符号链接或硬链接")
        if any(not member.isfile() and not member.isdir() for member in members):
            raise ValueError("备份包含不支持的特殊文件")
        try:
            manifest_member = stream.getmember("manifest.json")
            if not manifest_member.isfile():
                raise ValueError("备份清单不是普通文件")
            manifest_stream = stream.extractfile(manifest_member)
            if manifest_stream is None:
                raise ValueError("备份清单无法读取")
            manifest = json.load(manifest_stream)
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份清单不是有效 JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
            raise ValueError("备份清单版本不受支持")
        if manifest.get("database_kind") not in {"sqlite", "postgresql"}:
            raise ValueError("备份清单缺少有效的数据库类型")
        if (not isinstance(manifest.get("schema_revision"), str)
                or len(manifest["schema_revision"]) > 128):
            raise ValueError("备份清单包含无效迁移版本")
        if (not isinstance(manifest.get("database_url"), str)
                or len(manifest["database_url"]) > 2_048):
            raise ValueError("备份清单包含无效数据库地址")
        entries = manifest.get("files")
        if not isinstance(entries, list) or len(entries) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("备份清单缺少文件列表")
        listed: dict[str, dict] = {}
        total_size = 0
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("备份清单包含无效文件条目")
            name, size, digest = item.get("path"), item.get("size"), item.get("sha256")
            if (not isinstance(name, str) or not name or len(name) > MAX_MANIFEST_PATH_LENGTH
                    or name == "manifest.json" or
                    not safe_member(name) or name in listed):
                raise ValueError("备份清单包含不安全或重复的路径")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"备份清单包含无效文件大小：{name}")
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                raise ValueError(f"备份清单包含无效 SHA-256：{name}")
            listed[name] = item
            total_size += size
            if total_size > max_archive_uncompressed_bytes():
                raise ValueError("备份解压后大小超过限制")
        database_member = "database.sqlite3" if manifest["database_kind"] == "sqlite" else "database.dump"
        if database_member not in listed or listed[database_member]["size"] <= 0:
            raise ValueError(f"备份缺少有效数据库文件：{database_member}")
        actual = {member.name for member in members if member.isfile() and member.name != "manifest.json"}
        if actual != set(listed):
            raise ValueError("备份清单与归档文件不一致")
        members_by_name = {member.name: member for member in members if member.isfile()}
        for name, item in listed.items():
            if members_by_name[name].size != item["size"]:
                raise ValueError(f"备份清单与归档文件大小不一致：{name}")
            extracted = stream.extractfile(name)
            if extracted is None:
                raise ValueError(f"备份文件无法读取：{name}")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"备份校验失败：{name}")
    return manifest


def extract_verified_archive(archive: Path, stage: Path, manifest: dict | None = None) -> dict:
    """Extract a verified archive on every supported Python version.

    ``TarFile.extractall(filter=...)`` is unavailable on Python 3.10/3.11.
    Copy regular files explicitly after validation so restore keeps the same
    path, link and checksum protections without relying on that newer API.
    """
    manifest = manifest or verify_archive(archive)
    listed = {item["path"]: item for item in manifest.get("files", [])}
    stage.mkdir(parents=True, exist_ok=True)
    stage_root = stage.resolve()
    extracted_names: set[str] = set()
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream.getmembers():
            if not safe_member(member.name):
                raise ValueError("备份包含不安全的路径")
            target = stage / member.name
            resolved = target.resolve()
            if resolved != stage_root and stage_root not in resolved.parents:
                raise ValueError("备份路径越界")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("备份不允许包含符号链接或特殊文件")
            expected = listed.get(member.name)
            if expected is None:
                if member.name == "manifest.json":
                    continue
                raise ValueError("备份清单与归档文件不一致")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = stream.extractfile(member)
            if source is None:
                raise ValueError(f"备份文件无法读取：{member.name}")
            digest = hashlib.sha256()
            size = 0
            with source, target.open("xb") as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                raise ValueError(f"备份校验失败：{member.name}")
            extracted_names.add(member.name)
    if extracted_names != set(listed):
        raise ValueError("备份清单与归档文件不一致")
    return manifest


def create_backup(database_url: str, data_dir: Path, output_dir: Path) -> Path:
    kind = database_kind(database_url)
    # A symlinked output root could redirect an otherwise trusted backup or
    # prune operation to an unrelated directory. Resolve only after rejecting
    # the root itself as a link, matching the uploaded-data safety boundary.
    output_dir = safe_data_root(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.chmod(0o700)
    except OSError:
        # Some managed volumes do not expose chmod; archive files are still
        # written with mode 0600 below.
        pass
    # Include microseconds so two scheduled backups in one second cannot
    # silently replace one another before retention pruning runs.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = output_dir / f"{ARCHIVE_PREFIX}{timestamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="test-atelier-backup-", dir=output_dir) as temporary:
        stage = Path(temporary)
        if kind == "sqlite":
            revision = sqlite_snapshot(sqlite_path(database_url), stage / "database.sqlite3")
        else:
            postgres_dump(database_url, stage / "database.dump")
            revision = "external-pg-dump"
        data_count = copy_data(safe_data_root(data_dir), stage / "data")
        manifest = build_manifest(stage, database_url, kind, revision, data_count)
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{final.name}.", suffix=".tmp", dir=output_dir
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as archive_file:
                descriptor = -1
                with tarfile.open(fileobj=archive_file, mode="w:gz") as stream:
                    for item in sorted(stage.rglob("*")):
                        if item.is_file():
                            stream.add(item, arcname=item.relative_to(stage).as_posix(), recursive=False)
                archive_file.flush()
                os.fsync(archive_file.fileno())
            os.replace(temporary_name, final)
            temporary_name = ""
            _fsync_directory(output_dir)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        _chmod_private_file(final)
    verify_archive(final)
    return final


def prune_backups(output_dir: Path, keep: int) -> list[str]:
    if keep < 1:
        raise ValueError("--keep 必须至少为 1")
    output_dir = safe_data_root(output_dir)
    archives = [item for item in output_dir.glob(f"{ARCHIVE_PREFIX}*.tar.gz")
                if item.is_file() and not item.is_symlink()]
    archives.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    removed = []
    for archive in archives[keep:]:
        archive.unlink()
        removed.append(archive.name)
    return removed


def restore_sqlite(archive: Path, database_path: Path, data_dir: Path, force: bool) -> None:
    manifest = verify_archive(archive)
    if manifest.get("database_kind") != "sqlite":
        raise ValueError("restore 目前只支持 SQLite 归档；PostgreSQL 请使用 pg_restore")
    if database_path.is_symlink():
        raise ValueError(f"目标数据库不允许是符号链接：{database_path}")
    if data_dir.is_symlink():
        raise ValueError(f"目标数据目录不允许是符号链接：{data_dir}")
    if (database_path.exists() or data_dir.exists()) and not force:
        raise FileExistsError("目标已存在；恢复会覆盖数据，请显式传入 --force")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="test-atelier-restore-") as temporary:
        stage = Path(temporary)
        extract_verified_archive(archive, stage, manifest)
        # Archives created before empty-directory handling may contain no
        # ``data/`` member; restoring them should still produce an empty tree.
        (stage / "data").mkdir(parents=True, exist_ok=True)
        snapshot = stage / "database.sqlite3"
        check = sqlite3.connect(snapshot)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("恢复前 SQLite integrity_check 失败")
        finally:
            check.close()

        # Stage both outputs beside their final destinations.  A temporary
        # directory under /tmp can be on a different filesystem, making an
        # apparently atomic os.replace fail for a mounted production volume.
        staged_db_fd, staged_db_name = tempfile.mkstemp(
            prefix=f".{database_path.name}.restore-", dir=database_path.parent
        )
        os.close(staged_db_fd)
        staged_db = Path(staged_db_name)
        staged_data_parent = Path(tempfile.mkdtemp(
            prefix=f".{data_dir.name}.restore-", dir=data_dir.parent
        ))
        staged_data = staged_data_parent / data_dir.name
        old_db: Path | None = None
        old_data: Path | None = None
        db_installed = False
        data_installed = False
        committed = False
        try:
            shutil.copyfile(snapshot, staged_db)
            _chmod_private_file(staged_db)
            with staged_db.open("rb") as stream:
                os.fsync(stream.fileno())

            shutil.copytree(stage / "data", staged_data)
            _private_tree(staged_data)

            # Move current targets out of the way first.  If the second
            # install fails, the rollback below restores both old targets.
            if database_path.exists() or database_path.is_symlink():
                old_db = Path(tempfile.mkdtemp(
                    prefix=f".{database_path.name}.backup-", dir=database_path.parent
                ))
                old_db.rmdir()
                os.replace(database_path, old_db)
            if data_dir.exists() or data_dir.is_symlink():
                old_data = Path(tempfile.mkdtemp(
                    prefix=f".{data_dir.name}.backup-", dir=data_dir.parent
                ))
                old_data.rmdir()
                os.replace(data_dir, old_data)

            os.replace(staged_db, database_path)
            db_installed = True
            os.replace(staged_data, data_dir)
            data_installed = True
            _fsync_directory(database_path.parent)
            _fsync_directory(data_dir.parent)
            committed = True
        except Exception:
            # Restore the previous state before propagating the failure.  This
            # keeps a failed restore from leaving a live service with a new
            # database but an incomplete uploads directory (or vice versa).
            if db_installed:
                _remove_path(database_path)
            if data_installed:
                _remove_path(data_dir)
            if old_db is not None and old_db.exists():
                os.replace(old_db, database_path)
            if old_data is not None and old_data.exists():
                os.replace(old_data, data_dir)
            _fsync_directory(database_path.parent)
            _fsync_directory(data_dir.parent)
            raise
        finally:
            if staged_db.exists() or staged_db.is_symlink():
                _best_effort_remove(staged_db)
            if staged_data_parent.exists():
                _best_effort_remove(staged_data_parent)
            if committed:
                if old_db is not None and old_db.exists():
                    _best_effort_remove(old_db)
                if old_data is not None and old_data.exists():
                    _best_effort_remove(old_data)


def main() -> int:
    started = time.perf_counter()
    parser = _ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup = subparsers.add_parser("backup", help="创建并校验备份")
    backup.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    backup.add_argument("--data-dir", type=Path, default=Path(os.getenv("AI_STUDIO_DATA_DIR", ROOT / "data")))
    backup.add_argument("--output-dir", type=Path, default=ROOT / "backups")
    backup.add_argument("--keep", type=int, default=7, help="保留最近的备份数量")
    verify = subparsers.add_parser("verify", help="校验已有备份")
    verify.add_argument("archive", type=Path)
    restore = subparsers.add_parser("restore", help="恢复 SQLite 或 PostgreSQL 备份")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--database-path", type=Path, help="SQLite 目标文件")
    restore.add_argument("--database-url", default="", help="PostgreSQL 目标地址")
    restore.add_argument("--data-dir", type=Path, required=True)
    restore.add_argument("--force", action="store_true")
    try:
        args = parser.parse_args()
    except (argparse.ArgumentError, ValueError) as exc:
        print(json.dumps(event_payload(
            "backup.failed", level="ERROR", error_type="InvalidArguments",
            message=safe_error_message(str(exc)), duration_ms=round((time.perf_counter() - started) * 1000, 2),
        ), ensure_ascii=False), file=sys.stderr)
        return 2

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        if args.action == "backup":
            archive = create_backup(args.database_url, args.data_dir, args.output_dir)
            removed = prune_backups(args.output_dir, args.keep)
            manifest = verify_archive(archive)
            removed_names = [archive_name(Path(item)) for item in removed]
            print(json.dumps(event_payload(
                "backup.created", archive=archive_name(archive),
                removed=removed_names[:128], removed_count=len(removed_names),
                archive_size=archive.stat().st_size, duration_ms=elapsed_ms(), **manifest_summary(manifest),
            ), ensure_ascii=False))
        elif args.action == "verify":
            manifest = verify_archive(args.archive)
            print(json.dumps(event_payload(
                "backup.verified", archive=archive_name(args.archive), archive_size=args.archive.stat().st_size,
                duration_ms=elapsed_ms(), **manifest_summary(manifest),
            ), ensure_ascii=False))
        else:
            manifest = verify_archive(args.archive)
            if manifest.get("database_kind") == "sqlite":
                if args.database_path is None:
                    raise ValueError("SQLite 恢复必须提供 --database-path")
                restore_sqlite(args.archive, args.database_path, args.data_dir, args.force)
                database_target = str(args.database_path)
            else:
                postgres_restore(args.archive, args.database_url, args.data_dir, args.force)
                database_target = redacted_url(args.database_url)
            print(json.dumps(event_payload(
                "backup.restored", archive=archive_name(args.archive),
                database_kind=manifest.get("database_kind", "unknown"),
                target_name=Path(database_target).name[:255] if manifest.get("database_kind") == "sqlite" else "postgresql-target",
                data_dir_name=args.data_dir.name[:255], duration_ms=elapsed_ms(),
            ), ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps(event_payload(
            "backup.cancelled", level="WARNING", error_type="KeyboardInterrupt",
            duration_ms=elapsed_ms(),
        ), ensure_ascii=False), file=sys.stderr)
        return 130
    except (FileNotFoundError, FileExistsError, OSError, RuntimeError, ValueError,
            sqlite3.Error, tarfile.TarError) as exc:
        print(json.dumps(event_payload(
            "backup.failed", level="ERROR", error_type=type(exc).__name__,
            message=safe_error_message(str(exc)), duration_ms=elapsed_ms(),
        ), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
