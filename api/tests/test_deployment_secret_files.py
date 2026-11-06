import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_file_backed_session_encryption_and_admin_bootstrap(tmp_path):
    session = tmp_path / "session-secret"
    admin = tmp_path / "admin-password"
    session.write_text("s" * 40 + "\n", encoding="utf-8")
    admin.write_text("p" * 16 + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("AI_STUDIO_SESSION_SECRET", None)
    env.pop("AI_STUDIO_ADMIN_PASSWORD", None)
    env.update({
        "AI_STUDIO_SESSION_SECRET_FILE": str(session),
        "AI_STUDIO_ADMIN_PASSWORD_FILE": str(admin),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'atelier.db'}",
        "AI_STUDIO_DATA_DIR": str(tmp_path / "uploads"),
        "AI_STUDIO_ENV": "development", "AI_STUDIO_REQUIRE_MIGRATIONS": "0",
        "AI_STUDIO_EXTERNAL_WORKER": "0",
    })
    script = """
from fastapi.testclient import TestClient
from api.main import app, secret
from api.ai_engine import decrypt_secret, encrypt_secret
from cryptography.fernet import Fernet
import base64, hashlib
assert secret == 's' * 40
assert decrypt_secret(encrypt_secret('provider-token')) == 'provider-token'
legacy_cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())).encrypt(b'historical-value').decode()
assert decrypt_secret(legacy_cipher) == 'historical-value'
with TestClient(app) as client:
    csrf = client.get('/api/v1/auth/csrf').json()['token']
    response = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'p' * 16}, headers={'x-csrf-token': csrf})
    assert response.status_code == 200, response.status_code
print('file-backed authentication passed')
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-1200:]
    assert "file-backed authentication passed" in result.stdout
    assert "s" * 40 not in result.stderr and "p" * 16 not in result.stderr

    env["AI_STUDIO_SESSION_SECRET"] = "x" * 40
    result = subprocess.run([sys.executable, "-c", "import api.main"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "x" * 40 not in result.stderr and "s" * 40 not in result.stderr


def test_app_engine_reads_database_password_file_without_inline_url(tmp_path):
    secret = tmp_path / "database-password"
    secret.write_text("db@secret/with:symbols\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("POSTGRES_PASSWORD", None)
    env["DATABASE_URL"] = ""
    env["AI_STUDIO_DATABASE_PASSWORD_FILE"] = str(secret)
    script = "from api.db import engine; assert engine.url.password == 'db@secret/with:symbols'; assert engine.url.host == 'db'; print('db file configuration passed')"
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-600:]
    assert "db file configuration passed" in result.stdout
    assert "db@secret" not in result.stderr
