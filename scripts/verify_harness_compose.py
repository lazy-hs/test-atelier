#!/usr/bin/env python3
"""Validate the three-Compose-file Harness deployment without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE_TARGET = "/run/secrets/harness_scope_secret"
MODEL_TARGET = "/run/secrets/harness_model_api_key"
SESSION_TARGET = "/run/secrets/atelier_session_secret"
ADMIN_TARGET = "/run/secrets/atelier_admin_password"
DATABASE_TARGET = "/run/secrets/postgres_password"
METRICS_TARGET = "/run/secrets/atelier_metrics_token"
HOST_TOKEN_TARGET = "/run/secrets/harness_host_api_token"


class DeploymentCheckError(ValueError):
    """A diagnostic with a fixed message safe for an operator console."""


def _secret_file(filename: str, minimum: int, checkouts: tuple[Path, ...]) -> Path:
    path = Path(filename)
    if not filename or not path.is_absolute():
        raise DeploymentCheckError("Secret path must be absolute")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
        raise DeploymentCheckError("Secret source is not a bounded regular file")
    if any(path.resolve().is_relative_to(checkout.resolve()) for checkout in checkouts):
        raise DeploymentCheckError("Secret source must be outside checkouts")
    with path.open("rb") as source:
        content = source.read(4097).removesuffix(b"\r\n").removesuffix(b"\n")
    if not (minimum <= len(content) <= 4096) or content != content.strip() or any(c in content for c in (b"\r", b"\n", b"\x00")):
        raise DeploymentCheckError("Secret source is not a bounded single-line value")
    content.decode("utf-8")
    return path


def _mounted(service: dict, name: str) -> bool:
    return any(
        item == name or (
            isinstance(item, dict)
            and item.get("source") == name
            and item.get("target", name) in {name, f"/run/secrets/{name}"}
        )
        for item in service.get("secrets", [])
    )


def _has_secret(service: dict, name: str) -> bool:
    return any(item == name or (isinstance(item, dict) and item.get("source") == name)
               for item in service.get("secrets", []))


def _runtime_mounted(service: dict) -> bool:
    return any(isinstance(item, dict) and item.get("target") == "/app/config/runtime.json"
               for item in service.get("volumes", []))


def validate_config(config: dict, scope_path: Path, model_path: Path,
                    session_path: Path, admin_path: Path, database_path: Path, metrics_path: Path,
                    host_token_path: Path) -> None:
    services = config["services"]
    db, api, worker, projector, host = (services[name] for name in ("db", "api", "worker", "harness-projector", "harness"))
    legacy_mode = any("TEST_ATELIER_HARNESS_URL" in (service.get("environment") or {})
                      for service in (api, projector, host))
    if "web" not in services:
        raise DeploymentCheckError("Combined Compose is missing the Web service")
    if any(service.get("ports") or service.get("network_mode") for service in (db, api, worker, projector, host)):
        raise DeploymentCheckError("Internal services must remain on the private Compose network")
    source_secrets = config["secrets"]
    expected_common = {
        "harness_scope_secret", "harness_model_api_key", "atelier_session_secret",
        "atelier_admin_password", "atelier_metrics_token", "harness_host_api_token",
    }
    database_secret_name = "postgres_password" if "postgres_password" in source_secrets else "atelier_database_password"
    if set(source_secrets) != expected_common | {database_secret_name}:
        raise DeploymentCheckError("Combined Compose has an unexpected secret source")
    if Path(source_secrets["harness_scope_secret"]["file"]) != scope_path:
        raise DeploymentCheckError("Scope file does not match the configured source")
    if Path(source_secrets["harness_model_api_key"]["file"]) != model_path:
        raise DeploymentCheckError("Model file does not match the configured source")
    if Path(source_secrets["atelier_session_secret"]["file"]) != session_path:
        raise DeploymentCheckError("Session file does not match the configured source")
    if Path(source_secrets["atelier_admin_password"]["file"]) != admin_path:
        raise DeploymentCheckError("Admin file does not match the configured source")
    if Path(source_secrets[database_secret_name]["file"]) != database_path:
        raise DeploymentCheckError("Database file does not match the configured source")
    if Path(source_secrets["atelier_metrics_token"]["file"]) != metrics_path:
        raise DeploymentCheckError("Metrics file does not match the configured source")
    if Path(source_secrets["harness_host_api_token"]["file"]) != host_token_path:
        raise DeploymentCheckError("Host API token file does not match the configured source")
    if (db["environment"].get("POSTGRES_PASSWORD")
            or db["environment"].get("POSTGRES_PASSWORD_FILE") != DATABASE_TARGET
            or not _mounted(db, database_secret_name)):
        raise DeploymentCheckError("PostgreSQL password is missing or exposed")
    for service in (api, worker, projector):
        environment = service.get("environment") or {}
        if (environment.get("DATABASE_URL") or environment.get("POSTGRES_PASSWORD")
                    or not _mounted(service, database_secret_name)
                    or (not legacy_mode and not _runtime_mounted(service))):
            raise DeploymentCheckError("Application database password is missing or exposed")
        if not _mounted(service, "atelier_session_secret"):
            raise DeploymentCheckError("Browser session secret must be mounted in every application service")
    if (api.get("environment", {}).get("AI_STUDIO_ADMIN_PASSWORD")
            or not _mounted(api, "atelier_admin_password")):
        raise DeploymentCheckError("Bootstrap admin password is missing or exposed")
    if (api.get("environment", {}).get("METRICS_TOKEN")
            or not _mounted(api, "atelier_metrics_token")):
        raise DeploymentCheckError("Metrics token is missing or exposed")
    if any((service.get("environment") or {}).get("METRICS_TOKEN")
           or _mounted(service, "atelier_metrics_token")
           for service in (db, worker, projector, host)):
        raise DeploymentCheckError("Metrics token must be confined to the API")
    if any((service.get("environment") or {}).get("AI_STUDIO_ADMIN_PASSWORD")
           or (service.get("environment") or {}).get("AI_STUDIO_ADMIN_PASSWORD_FILE")
           or _mounted(service, "atelier_admin_password")
           for service in (worker, projector, host)):
        raise DeploymentCheckError("Bootstrap admin password must be confined to the API")
    if (_mounted(host, "atelier_session_secret")):
        raise DeploymentCheckError("Browser session secret must not reach the Host")
    if (_mounted(host, database_secret_name)):
        raise DeploymentCheckError("Database password must not reach the Host")
    if any(_mounted(service, "harness_model_api_key") for service in (api, worker, projector)):
        raise DeploymentCheckError("Model key must be confined to the Host")
    if _mounted(worker, "harness_scope_secret"):
        raise DeploymentCheckError("Scope key must not reach the worker")
    for service in services.values():
        environment = service.get("environment") or {}
        if environment.get("TEST_ATELIER_SCOPE_SECRET") or environment.get("OPENAI_API_KEY"):
            raise DeploymentCheckError("Inline Harness secret is exposed in a service environment")
        if environment.get("AI_STUDIO_SESSION_SECRET") or environment.get("AI_STUDIO_ADMIN_PASSWORD"):
            raise DeploymentCheckError("Inline application secret is exposed in a service environment")
    if legacy_mode:
        for service in (api, projector, host):
            environment = service.get("environment") or {}
            if environment.get("TEST_ATELIER_SCOPE_SECRET_FILE") != SCOPE_TARGET:
                raise DeploymentCheckError("Scope secret path is invalid")
            if environment.get("TEST_ATELIER_HARNESS_TOKEN_FILE") != HOST_TOKEN_TARGET:
                raise DeploymentCheckError("Host token path is invalid")
        host_environment = host.get("environment") or {}
        protocol = host_environment.get("HARNESS_DSH_PROTOCOL")
        analysis_checkpoints = host_environment.get("HARNESS_ANALYSIS_CHECKPOINTS")
        case_checkpoints = host_environment.get("HARNESS_CASE_CHECKPOINTS")
        if protocol not in {"acp", "headless"} or analysis_checkpoints not in {"0", "1"} or case_checkpoints not in {"0", "1"}:
            raise DeploymentCheckError("Harness protocol/checkpoint configuration is invalid")
        if host_environment.get("HARNESS_ACP_TRANSPORT") == "legacy":
            raise DeploymentCheckError("ACP checkpoints require the SDK transport")
    for service in (api, projector, host):
        environment = service.get("environment") or {}
        if (environment.get("TEST_ATELIER_HARNESS_TOKEN")
                or not _mounted(service, "harness_host_api_token")
                or (not legacy_mode and not _runtime_mounted(service))):
            raise DeploymentCheckError("Host API token must use the dedicated file in each Harness service")
        if environment.get("OPENAI_API_KEY"):
            raise DeploymentCheckError("Model secret is exposed in a service environment")
        if not _mounted(service, "harness_scope_secret"):
            raise DeploymentCheckError("Scope secret is not mounted in every Harness service")
    if not legacy_mode and not _runtime_mounted(api):
        raise DeploymentCheckError("API runtime config is not mounted")
    if any(service.get("environment", {}).get("AI_STUDIO_REMOTE_API_KEY") for service in services.values()):
        raise DeploymentCheckError("Legacy remote model key must not be interpolated into combined Compose")
    # Protocol, Host URL and Secret paths are declared in runtime.json and
    # loaded by each process; Compose must not duplicate them as environment
    # values. Keep the old explicit checks for legacy CI fixtures.
    if legacy_mode:
        if api.get("environment", {}).get("TEST_ATELIER_HARNESS_URL") != "http://harness:8787":
            raise DeploymentCheckError("API does not target the internal Host")
        protocol = (host.get("environment") or {}).get("HARNESS_DSH_PROTOCOL")
        if protocol not in {"acp", "headless"} or api.get("environment", {}).get("HARNESS_DSH_PROTOCOL") != protocol:
            raise DeploymentCheckError("API and Host must use the same supported Harness protocol")
        if protocol == "acp" and (host.get("environment") or {}).get("HARNESS_CONFIRMATION_TRANSPORT") != "none":
            raise DeploymentCheckError("ACP confirmations must use the ACP transport")
    if not _mounted(host, "harness_model_api_key"):
        raise DeploymentCheckError("Host model secret is not mounted")
    if (host.get("privileged") or host.get("read_only") is not True
            or "ALL" not in host.get("cap_drop", [])
            or "no-new-privileges:true" not in host.get("security_opt", [])):
        raise DeploymentCheckError("Host container isolation is invalid")
    secret_boundaries = (
        ("harness_model_api_key", {"harness"}, ("OPENAI_API_KEY", "OPENAI_API_KEY_FILE")),
        ("harness_scope_secret", {"api", "harness-projector", "harness"},
         ("TEST_ATELIER_SCOPE_SECRET", "TEST_ATELIER_SCOPE_SECRET_FILE")),
        ("harness_host_api_token", {"api", "harness-projector", "harness"},
         ("TEST_ATELIER_HARNESS_TOKEN", "TEST_ATELIER_HARNESS_TOKEN_FILE")),
        ("atelier_session_secret", {"api", "worker", "harness-projector"},
         ("AI_STUDIO_SESSION_SECRET", "AI_STUDIO_SESSION_SECRET_FILE")),
        ("atelier_admin_password", {"api"},
         ("AI_STUDIO_ADMIN_PASSWORD", "AI_STUDIO_ADMIN_PASSWORD_FILE")),
        ("atelier_metrics_token", {"api"}, ("METRICS_TOKEN", "METRICS_TOKEN_FILE")),
        (database_secret_name, {"db", "api", "worker", "harness-projector"},
         ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", "AI_STUDIO_DATABASE_PASSWORD_FILE", "DATABASE_URL")),
    )
    protected_paths = tuple(path.resolve() for path in (
        scope_path, model_path, session_path, admin_path, database_path, metrics_path, host_token_path
    ))
    for name, service in services.items():
        environment = service.get("environment") or {}
        for secret, recipients, variables in secret_boundaries:
            if name not in recipients and (_has_secret(service, secret) or any(environment.get(key) for key in variables)):
                raise DeploymentCheckError("Deployment secret reached an unauthorized service")
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source = volume.get("source")
            if isinstance(source, str) and Path(source).is_absolute():
                mounted_path = Path(source).resolve()
                if any(path == mounted_path or path.is_relative_to(mounted_path) for path in protected_paths):
                    raise DeploymentCheckError("Deployment secret is exposed through a bind mount")


def verify(extension_compose: Path, values: dict[str, str] | None = None) -> None:
    # Production source paths are declared by the checked-out JSON bundle; the
    # legacy environment aliases remain accepted only for older CI callers.
    values = dict(os.environ if values is None else values)
    secret_dir = ROOT / "config" / "secrets"
    defaults = {
        "TEST_ATELIER_SCOPE_KEY_FILE": str(secret_dir / "harness_scope_secret"),
        "TEST_ATELIER_HARNESS_KEY_FILE": str(secret_dir / "model_api_key"),
        "TEST_ATELIER_SESSION_KEY_FILE": str(secret_dir / "atelier_session_secret"),
        "TEST_ATELIER_ADMIN_PASSWORD_FILE": str(secret_dir / "atelier_admin_password"),
        "TEST_ATELIER_DB_PASSWORD_FILE": str(secret_dir / "postgres_password"),
        "TEST_ATELIER_METRICS_KEY_FILE": str(secret_dir / "atelier_metrics_token"),
        "TEST_ATELIER_HOST_TOKEN_FILE": str(secret_dir / "harness_host_api_token"),
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    extension_compose = extension_compose.resolve(strict=True)
    if not extension_compose.is_file():
        raise DeploymentCheckError("Extension Compose contract is not a regular file")
    checkouts = (ROOT, extension_compose.parent.parent, ROOT.parent / "deepseek-harness")
    scope_path = _secret_file(values.get("TEST_ATELIER_SCOPE_KEY_FILE", ""), 32, checkouts)
    model_path = _secret_file(values.get("TEST_ATELIER_HARNESS_KEY_FILE", ""), 16, checkouts)
    session_path = _secret_file(values.get("TEST_ATELIER_SESSION_KEY_FILE", ""), 32, checkouts)
    admin_path = _secret_file(values.get("TEST_ATELIER_ADMIN_PASSWORD_FILE", ""), 6, checkouts)
    database_path = _secret_file(values.get("TEST_ATELIER_DB_PASSWORD_FILE", ""), 12, checkouts)
    metrics_path = _secret_file(values.get("TEST_ATELIER_METRICS_KEY_FILE", ""), 16, checkouts)
    host_token_path = _secret_file(values.get("TEST_ATELIER_HOST_TOKEN_FILE", ""), 16, checkouts)
    paths = (scope_path, model_path, session_path, admin_path, database_path, metrics_path, host_token_path)
    secrets = tuple(path.read_text(encoding="utf-8").removesuffix("\r\n").removesuffix("\n") for path in paths)
    if len(set(paths)) != len(paths) or len(set(secrets)) != len(secrets):
        raise DeploymentCheckError("Deployment secrets must be independent")
    if any(values.get(name) for name in ("TEST_ATELIER_SCOPE_SECRET", "TEST_ATELIER_HARNESS_TOKEN", "OPENAI_API_KEY",
                                        "AI_STUDIO_SESSION_SECRET", "AI_STUDIO_ADMIN_PASSWORD",
                                        "POSTGRES_PASSWORD", "DATABASE_URL", "METRICS_TOKEN",
                                        "AI_STUDIO_REMOTE_API_KEY")):
        raise DeploymentCheckError("Inline deployment secrets must be unset for combined Compose")
    docker = shutil.which("docker")
    if not docker:
        raise DeploymentCheckError("Docker Compose CLI is unavailable")
    try:
        plugin = subprocess.run([docker, "compose", "version"], capture_output=True,
                                text=True, check=False, timeout=5, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentCheckError("Docker Compose plugin is unavailable") from exc
    if plugin.returncode:
        raise DeploymentCheckError("Docker Compose plugin is unavailable")
    command = [docker, "compose", "-f", str(ROOT / "compose.yaml"), "-f", str(extension_compose),
               "-f", str(ROOT / "compose.harness-overlay.yaml"), "config", "--format", "json"]
    try:
        result = subprocess.run(command, cwd=ROOT, env=values, capture_output=True,
                                text=True, check=False, timeout=15, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentCheckError("Combined Compose check could not run") from exc
    if result.returncode:
        raise DeploymentCheckError("Combined Compose configuration is invalid")
    try:
        config = json.loads(result.stdout)
        if any(secret in result.stdout or json.dumps(secret) in result.stdout for secret in secrets):
            raise DeploymentCheckError("A deployment secret appears in the resolved Compose configuration")
        validate_config(config, scope_path, model_path, session_path, admin_path, database_path, metrics_path, host_token_path)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DeploymentCheckError("Combined Compose configuration is incomplete") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-compose", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        verify(args.extension_compose)
    except DeploymentCheckError as exc:
        print(f"Harness Compose preflight failed: {exc}", file=sys.stderr)
        return 78
    except (OSError, UnicodeError, ValueError):
        print("Harness Compose preflight failed: invalid file or configuration", file=sys.stderr)
        return 78
    print("Harness Compose configuration and secret mounts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
