import json
from pathlib import Path

import pytest

from scripts import verify_harness_compose as check


def combined_config(scope: Path, model: Path, session: Path, admin: Path, database: Path) -> dict:
    metrics = database.with_name("metrics")
    host_token = database.with_name("host-token")
    return {
        "secrets": {
            "harness_scope_secret": {"file": str(scope)},
            "harness_model_api_key": {"file": str(model)},
            "atelier_session_secret": {"file": str(session)},
            "atelier_admin_password": {"file": str(admin)},
            "atelier_database_password": {"file": str(database)},
            "atelier_metrics_token": {"file": str(metrics)},
            "harness_host_api_token": {"file": str(host_token)},
        },
        "services": {
            "db": {"environment": {"POSTGRES_PASSWORD": "", "POSTGRES_PASSWORD_FILE": check.DATABASE_TARGET},
                   "secrets": ["atelier_database_password"]},
            "api": {"environment": {
                "DATABASE_URL": "", "AI_STUDIO_DATABASE_PASSWORD_FILE": check.DATABASE_TARGET,
                "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
                "HARNESS_DSH_PROTOCOL": "acp",
                "TEST_ATELIER_SCOPE_SECRET": "",
                "TEST_ATELIER_SCOPE_SECRET_FILE": check.SCOPE_TARGET,
                "TEST_ATELIER_HARNESS_TOKEN": "", "TEST_ATELIER_HARNESS_TOKEN_FILE": check.HOST_TOKEN_TARGET,
                "AI_STUDIO_SESSION_SECRET": "",
                "AI_STUDIO_SESSION_SECRET_FILE": check.SESSION_TARGET,
                "AI_STUDIO_ADMIN_PASSWORD": "",
                "AI_STUDIO_ADMIN_PASSWORD_FILE": check.ADMIN_TARGET,
                "METRICS_TOKEN": "", "METRICS_TOKEN_FILE": check.METRICS_TARGET,
            }, "secrets": [{"source": "harness_scope_secret"}, "harness_host_api_token", "atelier_session_secret", "atelier_admin_password", "atelier_database_password", "atelier_metrics_token"]},
            "worker": {"environment": {
                "DATABASE_URL": "", "AI_STUDIO_DATABASE_PASSWORD_FILE": check.DATABASE_TARGET,
                "AI_STUDIO_SESSION_SECRET": "", "AI_STUDIO_SESSION_SECRET_FILE": check.SESSION_TARGET,
            }, "secrets": ["atelier_session_secret", "atelier_database_password"]},
            "harness-projector": {"environment": {
                "DATABASE_URL": "", "AI_STUDIO_DATABASE_PASSWORD_FILE": check.DATABASE_TARGET,
                "TEST_ATELIER_HARNESS_URL": "http://harness:8787",
                "TEST_ATELIER_SCOPE_SECRET": "",
                "TEST_ATELIER_SCOPE_SECRET_FILE": check.SCOPE_TARGET,
                "TEST_ATELIER_HARNESS_TOKEN": "", "TEST_ATELIER_HARNESS_TOKEN_FILE": check.HOST_TOKEN_TARGET,
                "AI_STUDIO_SESSION_SECRET": "", "AI_STUDIO_SESSION_SECRET_FILE": check.SESSION_TARGET,
            }, "secrets": ["harness_scope_secret", "harness_host_api_token", "atelier_session_secret", "atelier_database_password"]},
            "harness": {"read_only": True, "cap_drop": ["ALL"],
                        "security_opt": ["no-new-privileges:true"], "environment": {
                "TEST_ATELIER_SCOPE_SECRET_FILE": check.SCOPE_TARGET,
                "TEST_ATELIER_HARNESS_TOKEN": "", "TEST_ATELIER_HARNESS_TOKEN_FILE": check.HOST_TOKEN_TARGET,
                "OPENAI_API_KEY_FILE": check.MODEL_TARGET,
                "HARNESS_DSH_PROTOCOL": "acp", "HARNESS_ANALYSIS_CHECKPOINTS": "1",
                "HARNESS_CASE_CHECKPOINTS": "1", "HARNESS_CONFIRMATION_TRANSPORT": "none",
            }, "secrets": ["harness_scope_secret", "harness_host_api_token", {"source": "harness_model_api_key"}]},
            "web": {"environment": {}, "secrets": []},
        },
    }


@pytest.mark.parametrize("service, field, value", [
    ("api", "HARNESS_DSH_PROTOCOL", "headless"),
    ("harness", "HARNESS_DSH_PROTOCOL", "headless"),
    ("harness", "HARNESS_ANALYSIS_CHECKPOINTS", "invalid"),
    ("harness", "HARNESS_ACP_TRANSPORT", "legacy"),
    ("harness", "HARNESS_CONFIRMATION_TRANSPORT", "stdin-lines"),
])
def test_combined_compose_rejects_incompatible_interactive_modes(tmp_path, service, field, value):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    config["services"][service]["environment"][field] = value
    with pytest.raises(check.DeploymentCheckError):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


def test_combined_compose_allows_explicit_noninteractive_fallback(tmp_path):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    config["services"]["api"]["environment"]["HARNESS_DSH_PROTOCOL"] = "headless"
    host_env = config["services"]["harness"]["environment"]
    host_env.update(HARNESS_DSH_PROTOCOL="headless", HARNESS_ANALYSIS_CHECKPOINTS="0", HARNESS_CASE_CHECKPOINTS="0")
    check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


def test_combined_compose_file_mounts_and_private_host(monkeypatch, tmp_path):
    scope = tmp_path / "scope"
    model = tmp_path / "model"
    session = tmp_path / "session"
    admin = tmp_path / "admin"
    database = tmp_path / "database"
    metrics = tmp_path / "metrics"
    scope.write_bytes(b"s" * 40 + b"\r\n")
    model.write_text("m" * 40, encoding="utf-8")
    session.write_text("t" * 40, encoding="utf-8")
    admin.write_text("p" * 16, encoding="utf-8")
    database.write_text("d" * 20, encoding="utf-8")
    metrics.write_text("n" * 24, encoding="utf-8")
    (tmp_path / "host-token").write_text("h" * 24, encoding="utf-8")
    extension = tmp_path / "extension" / "deploy" / "compose.yaml"
    extension.parent.mkdir(parents=True)
    extension.write_text("services: {}\n", encoding="utf-8")
    config = combined_config(scope, model, session, admin, database)
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps(config)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(check.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(check.subprocess, "run", run)
    env = {"TEST_ATELIER_SCOPE_KEY_FILE": str(scope), "TEST_ATELIER_HARNESS_KEY_FILE": str(model),
           "TEST_ATELIER_SESSION_KEY_FILE": str(session), "TEST_ATELIER_ADMIN_PASSWORD_FILE": str(admin),
           "TEST_ATELIER_DB_PASSWORD_FILE": str(database), "TEST_ATELIER_METRICS_KEY_FILE": str(metrics), "TEST_ATELIER_HOST_TOKEN_FILE": str(tmp_path / "host-token")}
    check.verify(extension, env)
    assert calls[0][0] == ["/usr/bin/docker", "compose", "version"]
    args, options = calls[1]
    assert args[:3] == ["/usr/bin/docker", "compose", "-f"]
    assert args[-3:] == ["config", "--format", "json"]
    assert options["shell"] is False and options["capture_output"] is True
    assert options["env"] == env
    model.write_text("s" * 40 + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="independent"):
        check.verify(extension, env)
    model.write_text("m" * 40, encoding="utf-8")
    env["TEST_ATELIER_HOST_TOKEN_FILE"] = str(scope)
    with pytest.raises(ValueError, match="independent"):
        check.verify(extension, env)
    env["TEST_ATELIER_HOST_TOKEN_FILE"] = str(tmp_path / "host-token")
    env["TEST_ATELIER_HARNESS_TOKEN"] = "inline-token-should-be-rejected"
    with pytest.raises(ValueError, match="Inline deployment secrets"):
        check.verify(extension, env)
    env.pop("TEST_ATELIER_HARNESS_TOKEN")
    env["AI_STUDIO_REMOTE_API_KEY"] = "legacy-remote-key-must-not-be-printed"
    with pytest.raises(ValueError, match="Inline deployment secrets"):
        check.verify(extension, env)
    env.pop("AI_STUDIO_REMOTE_API_KEY")
    config["services"]["harness"]["environment"]["UNEXPECTED_FIELD"] = model.read_text(encoding="utf-8")
    Result.stdout = json.dumps(config)
    with pytest.raises(ValueError, match="appears in the resolved Compose"):
        check.verify(extension, env)


def test_compose_normalizes_secret_targets_to_absolute_mount_paths(tmp_path):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    for service in config["services"].values():
        service["secrets"] = [
            {"source": item if isinstance(item, str) else item["source"],
             "target": f"/run/secrets/{item if isinstance(item, str) else item['source']}"}
            for item in service.get("secrets", [])
        ]
    check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))
    config["services"]["db"]["secrets"][0]["target"] = "/tmp/not-the-database-secret"
    with pytest.raises(ValueError, match="PostgreSQL password"):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


def test_combined_compose_rejects_alias_for_sensitive_secret_source(tmp_path):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    config["secrets"]["harmless_alias"] = {"file": str(model)}
    config["services"]["web"]["secrets"].append("harmless_alias")
    with pytest.raises(ValueError, match="unexpected secret source"):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


def test_combined_compose_requires_web_service_for_bounded_secret_audit(tmp_path):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    del config["services"]["web"]
    with pytest.raises(ValueError, match="missing the Web service"):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


@pytest.mark.parametrize("mount_parent", [False, True])
def test_combined_compose_rejects_bind_mount_exposing_secret_file(tmp_path, mount_parent):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    config["services"]["web"]["volumes"] = [{
        "type": "bind", "source": str(tmp_path if mount_parent else model), "target": "/opt/leaked-secret",
    }]
    with pytest.raises(ValueError, match="exposed through a bind mount"):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


@pytest.mark.parametrize("service,field,value", [
    ("api", "environment", {"TEST_ATELIER_SCOPE_SECRET": "leaked"}),
    ("api", "environment", {"TEST_ATELIER_HARNESS_TOKEN": "leaked"}),
    ("harness", "environment", {"TEST_ATELIER_HARNESS_TOKEN": "leaked"}),
    ("api", "environment", {"TEST_ATELIER_HARNESS_TOKEN_FILE": "/tmp/other"}),
    ("web", "secrets", ["harness_host_api_token"]),
    ("worker", "environment", {"TEST_ATELIER_HARNESS_TOKEN_FILE": check.HOST_TOKEN_TARGET}),
    ("harness-projector", "secrets", []),
    ("harness", "ports", ["8787:8787"]),
    ("harness", "network_mode", "host"),
    ("harness", "network_mode", "service:web"),
    ("harness", "privileged", True),
    ("harness", "read_only", False),
    ("harness", "cap_drop", []),
    ("harness", "security_opt", []),
    ("db", "ports", ["5432:5432"]),
    ("api", "ports", ["8000:8000"]),
    ("worker", "network_mode", "host"),
    ("harness-projector", "network_mode", "host"),
    ("harness", "environment", {"OPENAI_API_KEY": "leaked"}),
    ("worker", "environment", {"AI_STUDIO_SESSION_SECRET": "leaked"}),
    ("api", "secrets", []),
    ("harness-projector", "secrets", ["harness_scope_secret"]),
    ("harness", "secrets", ["harness_scope_secret", "harness_model_api_key", "atelier_session_secret"]),
    ("worker", "secrets", ["atelier_session_secret", "harness_model_api_key"]),
    ("db", "environment", {"POSTGRES_PASSWORD": "leaked"}),
    ("api", "environment", {"DATABASE_URL": "postgresql://u:secret@db/app"}),
    ("worker", "secrets", ["atelier_session_secret"]),
    ("api", "environment", {"METRICS_TOKEN": "leaked"}),
    ("worker", "secrets", ["atelier_session_secret", "atelier_database_password", "atelier_metrics_token"]),
    ("harness", "environment", {"METRICS_TOKEN_FILE": "/run/secrets/atelier_metrics_token"}),
    ("web", "secrets", ["atelier_metrics_token"]),
    ("web", "environment", {"METRICS_TOKEN_FILE": check.METRICS_TARGET}),
    ("web", "secrets", ["harness_model_api_key"]),
    ("web", "secrets", [{"source": "harness_model_api_key", "target": "/tmp/model-secret"}]),
    ("web", "environment", {"TEST_ATELIER_SCOPE_SECRET_FILE": check.SCOPE_TARGET}),
    ("db", "secrets", ["atelier_database_password", "harness_scope_secret"]),
    ("db", "environment", {"OPENAI_API_KEY": "leaked"}),
    ("web", "environment", {"AI_STUDIO_SESSION_SECRET": "leaked"}),
    ("web", "environment", {"AI_STUDIO_ADMIN_PASSWORD_FILE": check.ADMIN_TARGET}),
    ("web", "environment", {"DATABASE_URL": "postgresql://u:secret@db/app"}),
    ("api", "environment", {"AI_STUDIO_REMOTE_API_KEY": "legacy-key-must-not-be-interpolated"}),
    ("web", "environment", {"AI_STUDIO_REMOTE_API_KEY": "legacy-key-must-not-reach-web"}),
])
def test_combined_compose_rejects_secret_leaks_and_missing_mounts(tmp_path, service, field, value):
    scope, model, session, admin, database = (tmp_path / name for name in ("scope", "model", "session", "admin", "database"))
    config = combined_config(scope, model, session, admin, database)
    if isinstance(value, dict):
        config["services"][service][field].update(value)
    else:
        config["services"][service][field] = value
    with pytest.raises(ValueError):
        check.validate_config(config, scope, model, session, admin, database, database.with_name("metrics"), database.with_name("host-token"))


def test_combined_compose_rejects_same_key_and_checkout_files_without_echoing_key(monkeypatch, tmp_path, capsys):
    key = tmp_path / "scope"
    key.write_text("private-key-material-" + "x" * 32, encoding="utf-8")
    session, admin = tmp_path / "session", tmp_path / "admin"
    session.write_text("t" * 40, encoding="utf-8")
    admin.write_text("p" * 16, encoding="utf-8")
    database = tmp_path / "database"
    database.write_text("d" * 20, encoding="utf-8")
    metrics = tmp_path / "metrics"
    metrics.write_text("n" * 24, encoding="utf-8")
    (tmp_path / "host-token").write_text("h" * 24, encoding="utf-8")
    extension = tmp_path / "extension" / "deploy" / "compose.yaml"
    extension.parent.mkdir(parents=True)
    extension.write_text("services: {}\n", encoding="utf-8")
    env = {"TEST_ATELIER_SCOPE_KEY_FILE": str(key), "TEST_ATELIER_HARNESS_KEY_FILE": str(key),
           "TEST_ATELIER_SESSION_KEY_FILE": str(session), "TEST_ATELIER_ADMIN_PASSWORD_FILE": str(admin),
           "TEST_ATELIER_DB_PASSWORD_FILE": str(database), "TEST_ATELIER_METRICS_KEY_FILE": str(metrics), "TEST_ATELIER_HOST_TOKEN_FILE": str(tmp_path / "host-token")}
    with pytest.raises(ValueError):
        check.verify(extension, env)
    monkeypatch.setenv("TEST_ATELIER_SCOPE_KEY_FILE", str(key))
    monkeypatch.setenv("TEST_ATELIER_HARNESS_KEY_FILE", str(key))
    monkeypatch.setenv("TEST_ATELIER_SESSION_KEY_FILE", str(session))
    monkeypatch.setenv("TEST_ATELIER_ADMIN_PASSWORD_FILE", str(admin))
    monkeypatch.setenv("TEST_ATELIER_DB_PASSWORD_FILE", str(database))
    monkeypatch.setenv("TEST_ATELIER_METRICS_KEY_FILE", str(metrics))
    monkeypatch.setenv("TEST_ATELIER_HOST_TOKEN_FILE", str(tmp_path / "host-token"))
    monkeypatch.delenv("AI_STUDIO_SESSION_SECRET", raising=False)
    monkeypatch.delenv("AI_STUDIO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert check.main(["--extension-compose", str(extension)]) == 78
    assert key.read_text(encoding="utf-8") not in capsys.readouterr().err
    inside_checkout = extension.parent / "scope"
    inside_checkout.write_text("s" * 40, encoding="utf-8")
    model = tmp_path / "model"
    model.write_text("m" * 40, encoding="utf-8")
    with pytest.raises(ValueError, match="outside checkouts"):
        check.verify(extension, {"TEST_ATELIER_SCOPE_KEY_FILE": str(inside_checkout),
                                 "TEST_ATELIER_HARNESS_KEY_FILE": str(model),
                                 "TEST_ATELIER_SESSION_KEY_FILE": str(session),
                                 "TEST_ATELIER_ADMIN_PASSWORD_FILE": str(admin),
                                 "TEST_ATELIER_DB_PASSWORD_FILE": str(database),
                                 "TEST_ATELIER_METRICS_KEY_FILE": str(metrics), "TEST_ATELIER_HOST_TOKEN_FILE": str(tmp_path / "host-token")})


def test_combined_compose_cli_failure_does_not_echo_interpolated_credentials(monkeypatch, tmp_path, capsys):
    scope = tmp_path / "scope"
    model = tmp_path / "model"
    session, admin = tmp_path / "session", tmp_path / "admin"
    database = tmp_path / "database"
    metrics = tmp_path / "metrics"
    scope.write_text("s" * 40, encoding="utf-8")
    model.write_text("m" * 40, encoding="utf-8")
    session.write_text("t" * 40, encoding="utf-8")
    admin.write_text("p" * 16, encoding="utf-8")
    database.write_text("d" * 20, encoding="utf-8")
    metrics.write_text("n" * 24, encoding="utf-8")
    (tmp_path / "host-token").write_text("h" * 24, encoding="utf-8")
    extension = tmp_path / "extension" / "deploy" / "compose.yaml"
    extension.parent.mkdir(parents=True)
    extension.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("TEST_ATELIER_SCOPE_KEY_FILE", str(scope))
    monkeypatch.setenv("TEST_ATELIER_HARNESS_KEY_FILE", str(model))
    monkeypatch.setenv("TEST_ATELIER_SESSION_KEY_FILE", str(session))
    monkeypatch.setenv("TEST_ATELIER_ADMIN_PASSWORD_FILE", str(admin))
    monkeypatch.setenv("TEST_ATELIER_DB_PASSWORD_FILE", str(database))
    monkeypatch.setenv("TEST_ATELIER_METRICS_KEY_FILE", str(metrics))
    monkeypatch.setenv("TEST_ATELIER_HOST_TOKEN_FILE", str(tmp_path / "host-token"))
    monkeypatch.delenv("AI_STUDIO_SESSION_SECRET", raising=False)
    monkeypatch.delenv("AI_STUDIO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_ATELIER_SCOPE_SECRET", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(check.shutil, "which", lambda name: "/usr/bin/docker")

    class Result:
        returncode = 1
        stderr = "POSTGRES_PASSWORD=private-interpolated-value"

    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return Result() if len(calls) > 1 else type("Plugin", (), {"returncode": 0})()

    monkeypatch.setattr(check.subprocess, "run", run)
    assert check.main(["--extension-compose", str(extension)]) == 78
    assert len(calls) == 2
    assert "private-interpolated-value" not in capsys.readouterr().err
