import subprocess
from pathlib import Path

from scripts.validate_log_stream import validate_lines


ROOT = Path(__file__).resolve().parents[2]


def test_nginx_entrypoint_is_valid_shell_and_emits_structured_errors():
    script = ROOT / "web" / "nginx-entrypoint.sh"
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    content = script.read_text(encoding="utf-8")
    assert '"schema_version":1' in content
    assert 'emit_event "INFO" "nginx.starting"' in content
    assert '"event":"nginx.error"' in content
    assert 'emit_event "INFO" "nginx.stopped"' in content
    assert "trap" in content


def test_web_image_uses_the_json_log_entrypoint():
    dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY web/nginx-entrypoint.sh /usr/local/bin/test-atelier-nginx" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/test-atelier-nginx"]' in dockerfile
    assert "STOPSIGNAL SIGQUIT" in dockerfile


def test_nginx_entrypoint_normalizes_error_output(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'nginx: [warn] upstream unavailable?token=secret-value' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert '"event":"nginx.error"' in result.stdout
    assert '"event":"nginx.starting"' in result.stdout
    assert '"event":"nginx.stopped"' in result.stdout
    assert '"level":"WARNING"' in result.stdout
    assert "upstream unavailable" in result.stdout
    assert "secret-value" not in result.stdout
    assert "token=[REDACTED]" in result.stdout
    count, errors = validate_lines(result.stdout.splitlines())
    assert count == 3
    assert errors == []


def test_nginx_entrypoint_escapes_carriage_returns_for_json(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\r\\n' 'nginx: [warn] upstream unavailable' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "\\r" not in result.stdout
    assert '"event":"nginx.error"' in result.stdout


def test_nginx_entrypoint_redacts_common_cloud_tokens(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'nginx: [error] sk-1234567890abcdef AKIAIOSFODNN7EXAMPLE ghp_123456789012345678901234567890 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature123456' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "sk-1234567890abcdef" not in result.stdout
    assert "AKIAIOSFODNN7EXAMPLE" not in result.stdout
    assert "ghp_123456789012345678901234567890" not in result.stdout
    assert "eyJhbGciOiJIUzI1NiJ9" not in result.stdout


def test_nginx_entrypoint_redacts_uppercase_environment_credentials(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'nginx: [error] OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz AWS_SECRET_ACCESS_KEY=secret' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.stdout
    assert "AWS_SECRET_ACCESS_KEY=secret" not in result.stdout
    count, errors = validate_lines(result.stdout.splitlines())
    assert count == 3
    assert errors == []


def test_nginx_entrypoint_redacts_credentials_in_non_http_urls(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'nginx: [error] upstream postgresql+psycopg://app:db-secret@db:5432/atelier' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "db-secret" not in result.stdout
    assert "postgresql+psycopg://app:[REDACTED]@db:5432/atelier" in result.stdout
    count, errors = validate_lines(result.stdout.splitlines())
    assert count == 3
    assert errors == []


def test_nginx_entrypoint_redacts_cookie_headers_and_query_values(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'nginx: [error] Cookie: session=signed-session; csrf=csrf-secret https://upstream.test/?cookie=tracking-secret&attempt=2' >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "signed-session" not in result.stdout
    assert "csrf-secret" not in result.stdout
    assert "tracking-secret" not in result.stdout
    count, errors = validate_lines(result.stdout.splitlines())
    assert count == 3
    assert errors == []


def test_nginx_entrypoint_bounds_native_error_messages(tmp_path):
    fake = tmp_path / "fake-nginx"
    fake.write_text(
        "#!/bin/sh\nprintf 'nginx: [error] %s\\n' \"$(awk 'BEGIN { for (i = 0; i < 12000; i++) printf \\\"x\\\" }')\" >&2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh"), "-t"],
        env={"PATH": "/usr/bin:/bin", "NGINX_BIN": str(fake)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    # The envelope and message prefix add a small amount around the bounded
    # diagnostic, but the record remains far below the 64 KiB contract limit.
    assert len(result.stdout.encode("utf-8")) < 8_192
