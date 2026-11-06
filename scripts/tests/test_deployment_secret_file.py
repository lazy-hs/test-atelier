import os

import pytest

from scripts.deployment_secret_file import configured_secret, database_url


def test_configured_secret_accepts_file_without_inline_value(tmp_path):
    path = tmp_path / "session"
    path.write_text("s" * 40 + "\n", encoding="utf-8")
    env = {"AI_STUDIO_SESSION_SECRET_FILE": str(path)}
    assert configured_secret("AI_STUDIO_SESSION_SECRET", env) == "s" * 40
    with pytest.raises(ValueError):
        configured_secret("AI_STUDIO_SESSION_SECRET", {**env, "AI_STUDIO_SESSION_SECRET": "x" * 40})


def test_configured_secret_accepts_one_windows_line_ending_but_not_an_embedded_line(tmp_path):
    path = tmp_path / "session"
    env = {"AI_STUDIO_SESSION_SECRET_FILE": str(path)}
    path.write_bytes(b"s" * 40 + b"\r\n")
    assert configured_secret("AI_STUDIO_SESSION_SECRET", env) == "s" * 40
    path.write_bytes(b"s" * 40 + b"\r\nsecond")
    with pytest.raises(ValueError):
        configured_secret("AI_STUDIO_SESSION_SECRET", env)


@pytest.mark.parametrize("content", ["", "s" * 40 + "\nsecond", "x" * 4097, " secret "])
def test_configured_secret_rejects_invalid_content(tmp_path, content):
    path = tmp_path / "session"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        configured_secret("AI_STUDIO_SESSION_SECRET", {"AI_STUDIO_SESSION_SECRET_FILE": str(path)})


def test_configured_secret_rejects_symlink_and_relative_path(tmp_path):
    path = tmp_path / "session"
    path.write_text("s" * 40, encoding="utf-8")
    if os.name != "nt":
        link = tmp_path / "link"
        link.symlink_to(path)
        with pytest.raises(ValueError):
            configured_secret("AI_STUDIO_SESSION_SECRET", {"AI_STUDIO_SESSION_SECRET_FILE": str(link)})
    with pytest.raises(ValueError):
        configured_secret("AI_STUDIO_SESSION_SECRET", {"AI_STUDIO_SESSION_SECRET_FILE": "relative"})


def test_database_url_builds_a_quoted_postgres_dsn_without_an_inline_password(tmp_path):
    from sqlalchemy.engine import make_url

    path = tmp_path / "database-key"
    path.write_text("a@b:secret/password!\n", encoding="utf-8")
    values = {"AI_STUDIO_DATABASE_PASSWORD_FILE": str(path), "DATABASE_URL": ""}
    parsed = make_url(database_url(values))
    assert parsed.password == "a@b:secret/password!"
    assert parsed.host == "db" and parsed.database == "ai_studio"
    assert make_url(database_url(values, database="restored_db")).database == "restored_db"
    with pytest.raises(ValueError):
        database_url({**values, "DATABASE_URL": "sqlite:///somewhere.db"})
    with pytest.raises(ValueError):
        database_url({**values, "POSTGRES_PASSWORD": "duplicate"})
    with pytest.raises(ValueError):
        database_url(values, database="evil/name")
