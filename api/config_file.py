"""Durable JSON configuration for settings that must survive a rebuild.

The relational database remains the source of truth for project state and
secrets. This file is a small, human-readable mirror of the runtime control
plane so operators can inspect and back up model and skill configuration with
the data volume. API keys are never written in plaintext; the encrypted values
are tied to ``AI_STUDIO_SESSION_SECRET`` in the same way as the database copy.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .storage import ensure_private_directory, _fsync_directory


logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
DEFAULT_FILENAME = "settings.json"
DEFAULT_SERVER_CONFIG = {"port": 8000, "web_port": 5173}


@contextmanager
def _config_lock():
    """Serialize read/modify/write updates across API and worker processes."""
    path = config_path()
    ensure_private_directory(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    stream = lock_path.open("a+", encoding="utf-8")
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    try:
        try:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows local development normally has one API process. Atomic
            # replacement still protects readers when advisory locks are not
            # available.
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        stream.close()


def config_path() -> Path:
    """Return the configurable path on the durable application volume."""
    configured = os.getenv("AI_STUDIO_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = Path(os.getenv("AI_STUDIO_DATA_DIR", "data")).expanduser()
    return data_dir / "config" / DEFAULT_FILENAME


def _empty_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "server": dict(DEFAULT_SERVER_CONFIG),
        "models": {},
        "skills": [],
    }


def _normalize_server_config(value: Any) -> dict[str, int]:
    server = value if isinstance(value, Mapping) else {}
    normalized = dict(DEFAULT_SERVER_CONFIG)
    normalized.update(server)
    for key in ("port", "web_port"):
        try:
            port = int(normalized[key])
        except (TypeError, ValueError):
            port = DEFAULT_SERVER_CONFIG[key]
        normalized[key] = port if 1 <= port <= 65535 else DEFAULT_SERVER_CONFIG[key]
    return normalized


def read_config() -> dict[str, Any]:
    """Read the JSON file, treating a missing or invalid file as empty."""
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_config()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("config.file.read_failed", extra={
            "event": "config.file.read_failed", "component": "config-file",
            "error_type": type(exc).__name__,
        })
        return _empty_config()
    if not isinstance(payload, dict):
        return _empty_config()
    result = _empty_config()
    result.update(payload)
    try:
        result["schema_version"] = int(result.get("schema_version") or SCHEMA_VERSION)
    except (TypeError, ValueError):
        result["schema_version"] = SCHEMA_VERSION
    if not isinstance(result.get("models"), dict):
        result["models"] = {}
    if not isinstance(result.get("skills"), list):
        result["skills"] = []
    # Keep the runtime ports in the durable settings file while accepting
    # older files that predate the server section.
    result["server"] = _normalize_server_config(result.get("server"))
    return result


def server_config() -> dict[str, int]:
    """Return validated API and web development server ports."""
    server = read_config().get("server", DEFAULT_SERVER_CONFIG)
    return {
        "port": int(server.get("port", DEFAULT_SERVER_CONFIG["port"])),
        "web_port": int(server.get("web_port", DEFAULT_SERVER_CONFIG["web_port"])),
    }


def write_config(payload: Mapping[str, Any]) -> None:
    """Atomically write a private UTF-8 JSON configuration file."""
    path = config_path()
    parent = path.parent
    ensure_private_directory(parent)
    document = dict(payload)
    document["schema_version"] = SCHEMA_VERSION
    document["server"] = _normalize_server_config(document.get("server"))
    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(parent)
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def _provider_catalog(serialized: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(serialized or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def model_config_from_settings(settings: Any) -> dict[str, Any]:
    """Serialize an ``AiSettings`` row without exposing plaintext secrets."""
    providers = []
    for item in _provider_catalog(getattr(settings, "providers_json", "[]")):
        provider = {
            key: item[key]
            for key in ("id", "kind", "name", "endpoint", "model", "enabled", "detail", "protocol")
            if key in item
        }
        encrypted = str(item.get("api_key_encrypted") or "")
        if encrypted:
            provider["api_key_encrypted"] = encrypted
        providers.append(provider)
    return {
        "local": {
            "enabled": _provider_enabled(providers, "local", True),
            "url": str(getattr(settings, "local_url", "") or ""),
            "model": str(getattr(settings, "local_model", "") or ""),
        },
        "remote": {
            "enabled": bool(getattr(settings, "remote_enabled", False)),
            "url": str(getattr(settings, "remote_url", "") or ""),
            "model": str(getattr(settings, "remote_model", "") or ""),
            "api_key_encrypted": str(getattr(settings, "remote_api_key", "") or ""),
        },
        "cli": {
            "codex": {
                "enabled": bool(getattr(settings, "codex_enabled", False)),
                "command": str(getattr(settings, "codex_command", "") or ""),
                "model": str(getattr(settings, "codex_model", "") or ""),
            },
            "claude": {
                "enabled": bool(getattr(settings, "claude_enabled", False)),
                "command": str(getattr(settings, "claude_command", "") or ""),
                "model": str(getattr(settings, "claude_model", "") or ""),
            },
        },
        "providers": providers,
    }


def _provider_enabled(providers: list[dict[str, Any]], provider_id: str, default: bool) -> bool:
    for item in providers:
        if str(item.get("id")) == provider_id:
            return bool(item.get("enabled", default))
    return default


def persist_model_settings(settings: Any) -> bool:
    """Persist model settings and return whether the file changed."""
    with _config_lock():
        payload = read_config()
        models = model_config_from_settings(settings)
        if payload.get("models") == models:
            return False
        payload["models"] = models
        write_config(payload)
    logger.info("config.file.models_saved", extra={
        "event": "config.file.models_saved", "component": "config-file",
    })
    return True


def _skill_dict(spec: Any) -> dict[str, Any]:
    if isinstance(spec, Mapping):
        value = spec
        get = value.get
    else:
        get = lambda key, default=None: getattr(spec, key, default)
    # Deliberately keep skill content out of this file. The source path and
    # bounded metadata are enough to reconstruct the registry at startup.
    return {
        "id": str(get("skill_id", "") or ""),
        "name": str(get("name", "") or ""),
        "label": str(get("label", "") or ""),
        "path": str(get("path", "") or ""),
        "description": str(get("description", "") or ""),
        "enabled_by_default": bool(get("enabled_by_default", True)),
    }


def persist_skills(specs: Iterable[Any], *, root: str = "") -> bool:
    """Persist only skill paths and basic registry metadata."""
    skills = [_skill_dict(spec) for spec in specs]
    skills = [item for item in skills if item["id"]]
    with _config_lock():
        payload = read_config()
        if payload.get("skills") == skills and (not root or payload.get("skills_root") == root):
            return False
        payload["skills"] = skills
        if root:
            payload["skills_root"] = root
        write_config(payload)
    logger.info("config.file.skills_saved", extra={
        "event": "config.file.skills_saved", "component": "config-file", "count": len(skills),
    })
    return True
