import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import migrate_converter_history as migration
from api.db import Base, User


def _record(record_id: str, stored: str, preview: str, data: bytes) -> dict:
    return {
        "id": record_id,
        "stored_filename": stored,
        "preview_filename": preview,
        "file_size": len(data),
        "created_at": "2026-10-17T00:00:00+00:00",
        "source_filename": "source.xlsx",
        "output_filename": "output.xlsx",
        "conversion_mode": "excel_to_excel",
        "output_mode": "after",
        "media_type": "application/octet-stream",
        "app_version": "test",
        "row_count": 1,
    }


def _source(root: Path, records: list[tuple[str, bytes]]) -> Path:
    source = root / "history"
    (source / "files").mkdir(parents=True)
    (source / "previews").mkdir()
    index = []
    for record_id, data in records:
        stored = f"{record_id}.bin"
        preview = f"{record_id}.json"
        (source / "files" / stored).write_bytes(data)
        (source / "previews" / preview).write_text('{"row_count": 1}', encoding="utf-8")
        index.append(_record(record_id, stored, preview, data))
    (source / "records.json").write_text(json.dumps(index), encoding="utf-8")
    return source


@pytest.fixture
def migration_context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    sessions = sessionmaker(engine)
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(User(id=1, username="admin", password_hash="hash", role="admin"))
        db.commit()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(migration, "engine", engine)
    monkeypatch.setattr(migration, "SessionLocal", sessions)
    monkeypatch.setattr(migration, "DATA_DIR", data_dir)
    return tmp_path, sessions, data_dir


def test_migration_replaces_target_symlink_without_writing_outside(migration_context):
    root, sessions, data_dir = migration_context
    source = _source(root, [("record-1", b"private artifact")])
    target_dir = data_dir / "1"
    target_dir.mkdir(parents=True)
    outside = root / "outside.txt"
    outside.write_bytes(b"must remain unchanged")
    (target_dir / "record-1.bin").symlink_to(outside)

    assert migration.migrate(source) == 1
    assert (target_dir / "record-1.bin").read_bytes() == b"private artifact"
    assert outside.read_bytes() == b"must remain unchanged"
    assert migration.migrate(source) == 0
    with sessions() as db:
        assert db.query(migration.ConversionRecord).count() == 1
        assert db.query(migration.Project).count() == 1


def test_migration_rejects_symlinked_legacy_source(tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    files = root / "files"
    files.mkdir()
    (files / "linked.bin").symlink_to(outside)
    with pytest.raises(ValueError, match="历史文件"):
        migration._source_file(files, "linked.bin")


def test_migration_rolls_back_files_when_a_copy_fails(migration_context, monkeypatch):
    root, sessions, data_dir = migration_context
    source = _source(root, [("record-1", b"one"), ("record-2", b"two")])
    original_write = migration.write_private_atomic
    calls = 0

    def fail_on_second(path, data, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return original_write(path, data)

    monkeypatch.setattr(migration, "write_private_atomic", fail_on_second)
    with pytest.raises(OSError, match="simulated"):
        migration.migrate(source)
    assert not list(data_dir.rglob("*.bin"))
    with sessions() as db:
        assert db.query(migration.ConversionRecord).count() == 0


def test_migration_cli_json_success(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(migration, "migrate", lambda source, dry_run, quiet: 2)
    assert migration.main([str(tmp_path), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "legacy_migration.completed"
    assert payload["count"] == 2
    assert payload["dry_run"] is True
    assert payload["request_id"] == "-"


def test_migration_cli_json_failure_hides_exception_text(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(migration, "migrate", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("postgresql://app:secret@db/app")))
    assert migration.main([str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "legacy_migration.failed"
    assert payload["error_type"] == "RuntimeError"
    assert "secret" not in json.dumps(payload)


def test_migration_cli_json_serializes_cancellation(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(migration, "migrate", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert migration.main([str(tmp_path), "--json"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "legacy_migration.cancelled"
    assert payload["error_type"] == "KeyboardInterrupt"
    assert payload["duration_ms"] >= 0
def test_legacy_migration_event_drops_unapproved_sensitive_fields():
    payload = migration._event(
        "legacy_migration.test", message="password=secret", prompt="sensitive", count=1,
    )
    assert payload["count"] == 1
    assert "message" not in payload
    assert "prompt" not in payload


def test_legacy_migration_event_includes_configured_release(monkeypatch):
    monkeypatch.setenv("AI_STUDIO_RELEASE", "release-2026.10.18")
    payload = migration._event("legacy_migration.release")
    assert payload["release"] == "release-2026.10.18"


