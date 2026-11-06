"""Read bounded deployment secrets without exposing their contents in logs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

try:
    from .safe_secret_file import read_secret_bytes
except ImportError:
    from safe_secret_file import read_secret_bytes


def configured_secret(name: str, values: Mapping[str, str] | None = None) -> str:
    values = os.environ if values is None else values
    inline = values.get(name, "").strip()
    filename = values.get(f"{name}_FILE", "").strip()
    if not filename:
        return inline
    if inline or not Path(filename).is_absolute():
        raise ValueError("Deployment secret requires one absolute file path")
    path = Path(filename)
    try:
        content = read_secret_bytes(path).removesuffix(b"\r\n").removesuffix(b"\n")
        if not content or len(content) > 4096 or content != content.strip() or any(c in content for c in (b"\r", b"\n", b"\x00")):
            raise ValueError("Deployment secret file is invalid")
        return content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Deployment secret file is unavailable") from exc


def database_url(values: Mapping[str, str] | None = None, *, database: str = "ai_studio") -> str:
    values = os.environ if values is None else values
    filename = values.get("AI_STUDIO_DATABASE_PASSWORD_FILE", "").strip()
    if filename:
        if values.get("DATABASE_URL", "").strip() or values.get("POSTGRES_PASSWORD", "").strip():
            raise ValueError("Database password file conflicts with inline configuration")
        database = values.get("AI_STUDIO_DATABASE_NAME", database).strip() or database
        username = values.get("AI_STUDIO_DATABASE_USER", "ai_studio").strip() or "ai_studio"
        host = values.get("AI_STUDIO_DATABASE_HOST", "db").strip() or "db"
        port = values.get("AI_STUDIO_DATABASE_PORT", "5432").strip() or "5432"
        if not re.fullmatch(r"[A-Za-z0-9_]+", database) or not re.fullmatch(r"[A-Za-z0-9_]+", username):
            raise ValueError("Database name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ValueError("Database host or port is invalid")
        password = configured_secret("AI_STUDIO_DATABASE_PASSWORD", values)
        if len(password) < 12:
            raise ValueError("Database password is too short")
        return f"postgresql+psycopg://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}/{database}"
    return values.get("DATABASE_URL", "").strip() or "sqlite:///./data/ai_studio.db"
