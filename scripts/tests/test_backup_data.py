import io
import hashlib
import json
import os
import stat
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import scripts.backup_data as backup_data
from scripts.backup_data import (create_backup, event_payload, manifest_summary, postgres_restore,
                                 redacted_url, restore_sqlite, safe_error_message, verify_archive)
from scripts.validate_log_stream import validate_lines


def make_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE alembic_version(version_num TEXT)")
    db.execute("INSERT INTO alembic_version VALUES ('20261021_job_request_correlation')")
    db.commit()
    db.close()


def test_sqlite_backup_verify_and_restore(tmp_path):
    source = tmp_path / "ai_studio.db"
    data = tmp_path / "data" / "documents"
    data.mkdir(parents=True)
    (data / "source.txt").write_text("需求材料", encoding="utf-8")
    (tmp_path / "data" / ".worker-heartbeat").write_text("stale", encoding="ascii")
    make_database(source)

    archive = create_backup(f"sqlite:///{source}", tmp_path / "data", tmp_path / "backups")
    assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
    manifest = verify_archive(archive)
    assert manifest["schema_revision"] == "20261021_job_request_correlation"
    assert manifest["data_file_count"] == 1
    assert not any(item["path"] == "data/.worker-heartbeat" for item in manifest["files"])

    restored_db = tmp_path / "restore" / "ai_studio.db"
    restored_data = tmp_path / "restore" / "data"
    restore_sqlite(archive, restored_db, restored_data, force=False)
    assert sqlite3.connect(restored_db).execute("SELECT version_num FROM alembic_version").fetchone()[0] == manifest["schema_revision"]
    assert (restored_data / "documents" / "source.txt").read_text(encoding="utf-8") == "需求材料"
    assert not (restored_data / ".worker-heartbeat").exists()


def test_sqlite_restore_supports_empty_data_directory(tmp_path):
    source = tmp_path / "source.db"
    data = tmp_path / "data"
    data.mkdir(parents=True)
    make_database(source)

    archive = create_backup(f"sqlite:///{source}", data, tmp_path / "backups")
    restored_db = tmp_path / "restore.db"
    restored_data = tmp_path / "restore-data"
    restore_sqlite(archive, restored_db, restored_data, force=False)

    assert restored_db.is_file()
    assert restored_data.is_dir()
    assert list(restored_data.iterdir()) == []


def test_backup_cli_sqlite_round_trip(tmp_path):
    """Exercise the operator CLI, not only the underlying backup functions."""
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "ai_studio.db"
    data = tmp_path / "data" / "documents"
    data.mkdir(parents=True)
    (data / "source.txt").write_text("before", encoding="utf-8")
    make_database(source)
    env = {**os.environ, "AI_STUDIO_ENV": "test"}

    def run(*arguments):
        return subprocess.run(
            [sys.executable, "scripts/backup_data.py", *arguments],
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )

    backups = tmp_path / "backups"
    created = run(
        "backup", "--database-url", f"sqlite:///{source}",
        "--data-dir", str(tmp_path / "data"), "--output-dir", str(backups),
    )
    assert created.returncode == 0, created.stderr
    created_payload = json.loads(created.stdout)
    archive = backups / created_payload["archive"]
    assert archive.is_file()

    verified = run("verify", str(archive))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["event"] == "backup.verified"

    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE alembic_version SET version_num='mutated'")
        connection.commit()
    (data / "source.txt").write_text("after", encoding="utf-8")
    restored = run(
        "restore", str(archive), "--database-path", str(source),
        "--data-dir", str(tmp_path / "data"), "--force",
    )
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["event"] == "backup.restored"
    check = sqlite3.connect(source)
    try:
        assert check.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261021_job_request_correlation"
    finally:
        check.close()
    assert (data / "source.txt").read_text(encoding="utf-8") == "before"
    count, errors = validate_lines([created.stdout, verified.stdout, restored.stdout])
    assert count == 3
    assert errors == []


def test_sqlite_backup_without_migration_marker_is_unversioned(tmp_path):
    source = tmp_path / "legacy.db"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE records(value TEXT)")
    db.execute("INSERT INTO records VALUES ('legacy')")
    db.commit()
    db.close()
    data = tmp_path / "data"
    data.mkdir()

    archive = create_backup(f"sqlite:///{source}", data, tmp_path / "backups")

    assert verify_archive(archive)["schema_revision"] == "unversioned"


def test_backup_cli_serializes_sqlite_errors(tmp_path, monkeypatch, capsys):
    source = tmp_path / "not-a-database.db"
    source.write_text("not sqlite", encoding="ascii")
    data = tmp_path / "data"
    data.mkdir()
    output = tmp_path / "backups"
    monkeypatch.setattr(
        backup_data.sys,
        "argv",
        ["backup_data.py", "backup", "--database-url", f"sqlite:///{source}",
         "--data-dir", str(data), "--output-dir", str(output)],
    )

    assert backup_data.main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["event"] == "backup.failed"
    assert payload["error_type"] == "DatabaseError"
    assert payload["duration_ms"] >= 0
    assert "Traceback" not in captured.err


def test_backup_cli_serializes_operator_cancellation(monkeypatch, capsys):
    monkeypatch.setattr(backup_data, "create_backup", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(backup_data.sys, "argv", ["backup_data.py", "backup"])

    assert backup_data.main() == 130
    payload = json.loads(capsys.readouterr().err)
    assert payload["event"] == "backup.cancelled"
    assert payload["error_type"] == "KeyboardInterrupt"
    assert payload["duration_ms"] >= 0


def test_backup_event_payload_drops_sensitive_fields_and_redacts_messages():
    payload = event_payload(
        "backup.test", message="password=top-secret", database_url="postgresql://app:secret@db/app",
        prompt="sensitive prompt", archive="test-atelier-safe.tar.gz",
    )
    assert payload["message"] == "password=[REDACTED]"
    assert payload["archive"] == "test-atelier-safe.tar.gz"
    assert "database_url" not in payload
    assert "prompt" not in payload


def test_backup_timeout_ignores_non_finite_environment_value(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_BACKUP_TIMEOUT", "nan")

    assert backup_data.command_timeout() == 120.0


def test_verify_rejects_archive_above_uncompressed_limit(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    data = tmp_path / "source-data"
    data.mkdir()
    (data / "document.txt").write_text("document", encoding="utf-8")
    make_database(source)
    archive = create_backup(f"sqlite:///{source}", data, tmp_path / "backups")

    monkeypatch.setenv("AI_STUDIO_BACKUP_MAX_UNCOMPRESSED_BYTES", "1")
    with pytest.raises(ValueError, match="解压后大小超过限制"):
        verify_archive(archive)


def test_verify_rejects_manifest_size_mismatch(tmp_path):
    payload = {
        "format": 1,
        "database_kind": "sqlite",
        "database_url": "sqlite:///ai_studio.db",
        "schema_revision": "head",
        "data_file_count": 0,
        "files": [{"path": "database.sqlite3", "size": 999, "sha256": hashlib.sha256(b"db").hexdigest()}],
    }
    archive = tmp_path / "mismatch.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        manifest_bytes = json.dumps(payload).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(manifest_bytes)
        stream.addfile(info, io.BytesIO(manifest_bytes))
        info = tarfile.TarInfo("database.sqlite3"); info.size = 2
        stream.addfile(info, io.BytesIO(b"db"))

    with pytest.raises(ValueError, match="大小不一致"):
        verify_archive(archive)


def test_sqlite_restore_replaces_existing_targets_atomically(tmp_path):
    source = tmp_path / "source.db"
    data = tmp_path / "source-data"
    data.mkdir()
    (data / "new.txt").write_text("new", encoding="utf-8")
    make_database(source)
    archive = create_backup(f"sqlite:///{source}", data, tmp_path / "backups")

    target_db = tmp_path / "target.db"
    target_data = tmp_path / "target-data"
    make_database(target_db)
    (target_data / "old").mkdir(parents=True)
    (target_data / "old" / "only-old.txt").write_text("old", encoding="utf-8")
    restore_sqlite(archive, target_db, target_data, force=True)

    assert (target_data / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (target_data / "old").exists()
    assert stat.S_IMODE(target_data.stat().st_mode) == 0o700
    assert stat.S_IMODE(target_db.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".target-data.backup-*"))
    assert not list(tmp_path.glob(".target.db.restore-*"))


def test_sqlite_restore_rolls_back_when_data_install_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    data = tmp_path / "source-data"
    data.mkdir()
    (data / "new.txt").write_text("new", encoding="utf-8")
    make_database(source)
    archive = create_backup(f"sqlite:///{source}", data, tmp_path / "backups")

    target_db = tmp_path / "target.db"
    target_data = tmp_path / "target-data"
    make_database(target_db)
    (target_data / "old").mkdir(parents=True)
    (target_data / "old" / "only-old.txt").write_text("old", encoding="utf-8")
    original_replace = backup_data.os.replace

    def fail_data_install(source_path, destination):
        source_path = Path(source_path)
        if source_path.name == target_data.name and source_path.parent.name.startswith(".target-data.restore-"):
            raise OSError("simulated data install failure")
        return original_replace(source_path, destination)

    monkeypatch.setattr(backup_data.os, "replace", fail_data_install)
    with pytest.raises(OSError, match="simulated data install failure"):
        restore_sqlite(archive, target_db, target_data, force=True)

    assert sqlite3.connect(target_db).execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261021_job_request_correlation"
    assert (target_data / "old" / "only-old.txt").read_text(encoding="utf-8") == "old"
    assert not (target_data / "new.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions are not portable on Windows")
def test_backup_rejects_data_symlink(tmp_path):
    source = tmp_path / "ai_studio.db"
    data = tmp_path / "data"
    data.mkdir()
    make_database(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (data / "linked.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(f"sqlite:///{source}", data, tmp_path / "backups")


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions are not portable on Windows")
def test_backup_rejects_symlinked_data_root(tmp_path):
    source = tmp_path / "ai_studio.db"
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    make_database(source)
    linked_root = tmp_path / "data"
    linked_root.symlink_to(real_data, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(f"sqlite:///{source}", linked_root, tmp_path / "backups")


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_backup_rejects_symlinked_database(tmp_path):
    real_source = tmp_path / "real.db"
    make_database(real_source)
    linked_source = tmp_path / "ai_studio.db"
    linked_source.symlink_to(real_source)
    with pytest.raises(FileNotFoundError):
        create_backup(f"sqlite:///{linked_source}", tmp_path / "data", tmp_path / "backups")


@pytest.mark.skipif(os.name == "nt", reason="symlink permissions are not portable on Windows")
def test_backup_rejects_symlinked_output_root(tmp_path):
    source = tmp_path / "ai_studio.db"
    data = tmp_path / "data"
    data.mkdir()
    make_database(source)
    real_backups = tmp_path / "real-backups"
    real_backups.mkdir()
    linked_backups = tmp_path / "backups"
    linked_backups.symlink_to(real_backups, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(f"sqlite:///{source}", data, linked_backups)


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_prune_backups_ignores_symlinked_archives(tmp_path):
    from scripts.backup_data import prune_backups
    real = tmp_path / "outside.tar.gz"
    real.write_bytes(b"outside")
    linked = tmp_path / "test-atelier-linked.tar.gz"
    linked.symlink_to(real)
    (tmp_path / "test-atelier-one.tar.gz").write_bytes(b"one")
    removed = prune_backups(tmp_path, keep=1)
    assert removed == []
    assert linked.is_symlink() and real.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_private_file_mode_does_not_follow_symlink(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(outside)
    with pytest.raises(OSError):
        backup_data._chmod_private_file(linked)


def test_verify_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        stream.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="不安全"):
        verify_archive(archive)


def test_verify_rejects_duplicate_or_link_members(tmp_path):
    duplicate = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate, "w:gz") as stream:
        for value in (b"one", b"two"):
            info = tarfile.TarInfo("data/file.txt")
            info.size = len(value)
            stream.addfile(info, io.BytesIO(value))
    with pytest.raises(ValueError, match="重复"):
        verify_archive(duplicate)

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as stream:
        info = tarfile.TarInfo("data/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "outside"
        stream.addfile(info)
    with pytest.raises(ValueError, match="链接"):
        verify_archive(linked)


def test_verify_rejects_malformed_manifest_entries(tmp_path):
    archive = tmp_path / "malformed.tar.gz"
    manifest = {
        "format": 1,
        "database_kind": "sqlite",
        "schema_revision": "head",
        "database_url": "sqlite:///./ai_studio.db",
        "files": [{"path": "database.sqlite3", "size": 0, "sha256": "not-a-digest"}],
    }
    with tarfile.open(archive, "w:gz") as stream:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        stream.addfile(info, io.BytesIO(payload))
        info = tarfile.TarInfo("database.sqlite3")
        stream.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="SHA-256"):
        verify_archive(archive)


def test_verify_requires_database_member_for_kind(tmp_path):
    archive = tmp_path / "missing-database.tar.gz"
    payload = json.dumps({
        "format": 1, "database_kind": "sqlite", "schema_revision": "head",
        "database_url": "sqlite:///./ai_studio.db", "files": [],
    }).encode()
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("manifest.json"); info.size = len(payload)
        stream.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="缺少有效数据库文件"):
        verify_archive(archive)


def test_verify_rejects_special_members(tmp_path):
    archive = tmp_path / "special.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("data/fifo")
        info.type = tarfile.FIFOTYPE
        stream.addfile(info)
    with pytest.raises(ValueError, match="特殊文件"):
        verify_archive(archive)


def test_verify_rejects_corrupt_archive_without_traceback(tmp_path):
    archive = tmp_path / "corrupt.tar.gz"
    archive.write_bytes(b"not a tarball")
    with pytest.raises(tarfile.ReadError):
        verify_archive(archive)


@pytest.mark.skipif(os.name == "nt", reason="symlink behavior is not portable on Windows")
def test_verify_rejects_symlinked_archive(tmp_path):
    real_archive = tmp_path / "real.tar.gz"
    real_archive.write_bytes(b"not a tarball")
    linked_archive = tmp_path / "linked.tar.gz"
    linked_archive.symlink_to(real_archive)
    with pytest.raises(FileNotFoundError):
        verify_archive(linked_archive)


def test_redacted_url_hides_query_credentials():
    value = redacted_url("postgresql://app:p%40ss@db:5432/atelier?sslmode=require&password=secret&api_key=another")
    assert "p%40ss" not in value
    assert "secret" not in value and "another" not in value
    assert "sslmode=require" in value
    assert value.count("REDACTED") == 3
    assert redacted_url("sqlite:///./ai_studio.db") == "sqlite:///./ai_studio.db"


def test_safe_error_message_hides_database_and_assignment_credentials():
    value = safe_error_message(
        "postgresql://app:db-secret@db:5432/atelier password=another-secret access_token=token-secret "
        "Authorization: Bearer abc.def sk-1234567890abcdef"
    )
    assert "db-secret" not in value and "another-secret" not in value
    assert "token-secret" not in value
    assert "abc.def" not in value and "sk-1234567890abcdef" not in value
    assert "postgresql://app:[REDACTED]@db:5432/atelier" in value
    assert "password=[REDACTED]" in value


def test_safe_error_message_hides_uppercase_environment_credentials():
    value = safe_error_message("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz AWS_SECRET_ACCESS_KEY=secret")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in value
    assert "AWS_SECRET_ACCESS_KEY=secret" not in value
    assert value.count("[REDACTED]") == 2


def test_backup_events_include_sanitized_release(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "build 2026/10?token=secret")
    payload = event_payload("backup.test")
    assert payload["release"] == "build_2026/10_token_REDACTED_"


def test_safe_error_message_hides_query_credentials_without_losing_other_params():
    value = safe_error_message("provider https://model.test/run?token=secret-token&attempt=2")
    assert "secret-token" not in value
    assert "token=[REDACTED]" in value
    assert "attempt=2" in value


def test_safe_error_message_hides_cookie_headers_and_cookie_queries():
    value = safe_error_message(
        "client Cookie: session=signed-session; csrf=csrf-secret "
        "https://example.test/callback?cookie=tracking-secret&attempt=2"
    )
    assert "signed-session" not in value
    assert "csrf-secret" not in value
    assert "tracking-secret" not in value


def test_safe_error_message_hides_quoted_json_credentials():
    value = safe_error_message('{"token":"session-secret", "password":"p@ss"}')
    assert "session-secret" not in value
    assert "p@ss" not in value
    assert value.count("[REDACTED]") == 2


def test_safe_error_message_hides_cloud_tokens_jwt_and_private_key_material():
    value = safe_error_message(
        "AKIAIOSFODNN7EXAMPLE ghp_123456789012345678901234567890 "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature123456 "
        "-----BEGIN PRIVATE KEY-----\nsecret-key-material\n-----END PRIVATE KEY-----"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in value
    assert "ghp_123456789012345678901234567890" not in value
    assert "eyJhbGciOiJIUzI1NiJ9" not in value
    assert "secret-key-material" not in value
    assert "PRIVATE_KEY_REDACTED" in value


def test_backup_cli_failure_output_is_redacted(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(backup_data, "create_backup", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("connect failed postgresql://app:db-secret@db:5432/atelier password=another-secret")
    ))
    monkeypatch.setattr(backup_data.sys, "argv", [
        "backup_data.py", "backup", "--database-url", "sqlite:///missing.db",
        "--data-dir", str(tmp_path / "data"), "--output-dir", str(tmp_path / "backups"),
    ])
    assert backup_data.main() == 1
    output = capsys.readouterr().err
    assert "db-secret" not in output and "another-secret" not in output
    payload = json.loads(output)
    assert payload["schema_version"] == 1
    assert payload["request_id"] == "-"
    assert payload["service"] == "test-atelier"
    assert payload["component"] == "backup"
    assert "[REDACTED]" in output


def test_backup_cli_invalid_arguments_are_structured(monkeypatch, capsys):
    monkeypatch.setattr(backup_data.sys, "argv", ["backup_data.py", "unknown", "--token", "secret-token"])
    assert backup_data.main() == 2
    output = capsys.readouterr().err
    payload = json.loads(output)
    assert payload["event"] == "backup.failed"
    assert payload["error_type"] == "InvalidArguments"
    assert "secret-token" not in output


def test_backup_cli_bounds_removed_archive_names(monkeypatch, capsys, tmp_path):
    archive = tmp_path / "test-atelier-current.tar.gz"
    archive.write_bytes(b"archive")
    monkeypatch.setattr(backup_data, "create_backup", lambda *_args, **_kwargs: archive)
    monkeypatch.setattr(backup_data, "prune_backups", lambda *_args, **_kwargs: [
        tmp_path / f"test-atelier-old-{index}.tar.gz" for index in range(200)
    ])
    monkeypatch.setattr(backup_data, "verify_archive", lambda *_args, **_kwargs: {
        "database_kind": "sqlite", "schema_revision": "head", "data_file_count": 0, "files": [],
    })
    monkeypatch.setattr(backup_data.sys, "argv", [
        "backup_data.py", "backup", "--output-dir", str(tmp_path), "--data-dir", str(tmp_path / "data"),
    ])
    assert backup_data.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed_count"] == 200
    assert len(payload["removed"]) == 128


def test_manifest_summary_does_not_include_uploaded_paths():
    summary = manifest_summary({
        "database_kind": "sqlite", "schema_revision": "head", "data_file_count": 1,
        "files": [{"path": "data/customer-sensitive-name.txt", "size": 1}],
    })
    assert summary == {
        "database_kind": "sqlite", "schema_revision": "head",
        "data_file_count": 1, "file_count": 1,
    }
    assert "customer-sensitive-name.txt" not in json.dumps(summary)


def test_postgres_restore_uses_explicit_target_and_redacts_password(tmp_path, monkeypatch):
    dump = b"fake postgres dump"
    source = tmp_path / "source.txt"
    source.write_bytes(b"uploaded")
    manifest = {
        "format": 1,
        "database_kind": "postgresql",
        "database_url": "postgresql://app:REDACTED@db:5432/atelier",
        "schema_revision": "external-pg-dump",
        "data_file_count": 1,
        "files": [
            {"path": "database.dump", "size": len(dump), "sha256": hashlib.sha256(dump).hexdigest()},
            {"path": "data/source.txt", "size": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        ],
    }
    archive = tmp_path / "postgres.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(manifest_bytes)
        stream.addfile(info, io.BytesIO(manifest_bytes))
        info = tarfile.TarInfo("database.dump"); info.size = len(dump); stream.addfile(info, io.BytesIO(dump))
        info = tarfile.TarInfo("data/source.txt"); info.size = 8; stream.addfile(info, io.BytesIO(b"uploaded"))

    calls = {}
    def run(args, **kwargs):
        calls["args"] = args; calls["kwargs"] = kwargs
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr("scripts.backup_data.subprocess.run", run)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:application-secret@db/atelier")
    monkeypatch.setenv("AI_STUDIO_REMOTE_API_KEY", "remote-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("PGSERVICE", "unexpected-service")
    monkeypatch.setenv("PGPASSWORD", "unexpected-password")
    target = tmp_path / "restored-data"
    postgres_restore(archive, "postgresql+psycopg://app:p%40ss@db:5432/atelier?sslmode=require&connect_timeout=4", target, force=True)
    assert calls["args"][0] == "pg_restore"
    assert "--single-transaction" in calls["args"]
    assert Path(calls["args"][-1]).name == "database.dump"
    assert calls["kwargs"]["env"]["PGPASSWORD"] == "p@ss"
    assert calls["kwargs"]["env"]["PGSSLMODE"] == "require"
    assert calls["kwargs"]["env"]["PGCONNECT_TIMEOUT"] == "4"
    assert "DATABASE_URL" not in calls["kwargs"]["env"]
    assert "AI_STUDIO_REMOTE_API_KEY" not in calls["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in calls["kwargs"]["env"]
    assert "PGSERVICE" not in calls["kwargs"]["env"]
    assert calls["kwargs"]["env"]["PGPASSWORD"] == "p@ss"
    assert (target / "source.txt").read_bytes() == b"uploaded"


def test_postgres_restore_failure_keeps_existing_data(tmp_path, monkeypatch):
    dump = b"fake postgres dump"
    restored_file = b"new backup content"
    manifest = {
        "format": 1,
        "database_kind": "postgresql",
        "database_url": "postgresql://app:REDACTED@db:5432/atelier",
        "schema_revision": "external-pg-dump",
        "data_file_count": 1,
        "files": [
            {"path": "database.dump", "size": len(dump), "sha256": hashlib.sha256(dump).hexdigest()},
            {"path": "data/new.txt", "size": len(restored_file), "sha256": hashlib.sha256(restored_file).hexdigest()},
        ],
    }
    archive = tmp_path / "postgres.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(manifest_bytes)
        stream.addfile(info, io.BytesIO(manifest_bytes))
        info = tarfile.TarInfo("database.dump"); info.size = len(dump)
        stream.addfile(info, io.BytesIO(dump))
        info = tarfile.TarInfo("data/new.txt"); info.size = len(restored_file)
        stream.addfile(info, io.BytesIO(restored_file))

    target = tmp_path / "restored-data"
    target.mkdir()
    (target / "existing.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr("scripts.backup_data.subprocess.run", lambda *_args, **_kwargs: type(
        "Result", (), {"returncode": 1, "stdout": "", "stderr": "database unavailable"}
    )())
    with pytest.raises(RuntimeError, match="pg_restore 失败"):
        postgres_restore(archive, "postgresql://app:secret@db:5432/atelier", target, force=True)
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep"
    assert not (target / "new.txt").exists()
    assert not list(tmp_path.glob(".restored-data.restore-*"))


def test_postgres_dump_timeout_is_reported(tmp_path, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("pg_dump", 1)

    monkeypatch.setattr("scripts.backup_data.subprocess.run", timeout)
    with pytest.raises(RuntimeError, match="pg_dump.*超时"):
        from scripts.backup_data import postgres_dump
        postgres_dump("postgresql://app:secret@db:5432/atelier", tmp_path / "dump")


def test_postgres_dump_failure_redacts_provider_output(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.backup_data.subprocess.run", lambda *_args, **_kwargs: type(
        "Result", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "connect failed postgresql://app:db-secret@db/atelier password=hunter2",
        }
    )())
    from scripts.backup_data import postgres_dump
    with pytest.raises(RuntimeError) as error:
        postgres_dump("postgresql://app:secret@db:5432/atelier", tmp_path / "dump")
    assert "db-secret" not in str(error.value)
    assert "hunter2" not in str(error.value)
