from pathlib import Path
import json
import os
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_nginx_logs_are_visible_to_container_runtime():
    config = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")

    assert "access_log /dev/stdout test_atelier_json;" in config
    assert "error_log /dev/stderr warn;" in config
    assert '"schema_version":1' in config
    assert '"environment":"production"' in config
    assert '"event":"request.completed"' in config
    assert 'map $uri $log_uri' in config
    assert 'path":"$log_uri"' in config
    assert 'upstream_status":"$upstream_status' in config
    assert 'upstream_duration_seconds":"$upstream_response_time' in config
    assert 'request_bytes":$request_length' in config
    assert 'token|password|secret' in config
    assert "limit_conn_zone $binary_remote_addr zone=test_atelier_connections:10m;" in config
    assert "limit_conn test_atelier_connections 40;" in config
    assert "server_tokens off;" in config
    assert 'https "max-age=31536000; includeSubDomains"' in config
    assert "add_header Strict-Transport-Security $hsts_header always;" in config
    assert "add_header Content-Security-Policy" in config
    assert "script-src 'self'" in config
    assert "object-src 'none'" in config
    assert "listen 8080;" in config
    assert "proxy_temp_path /tmp/nginx-proxy" in config
    assert config.count("proxy_hide_header Content-Security-Policy;") == 2


def test_web_image_keeps_nginx_logs_on_foreground_json_stream():
    dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/usr/local/bin/test-atelier-nginx"]' in dockerfile
    assert "COPY web/nginx-entrypoint.sh /usr/local/bin/test-atelier-nginx" in dockerfile
    assert 'CMD ["-g", "daemon off;"]' in dockerfile
    assert "USER nginx" in dockerfile
    assert "COPY web/nginx-main.conf /etc/nginx/nginx.conf" in dockerfile


def test_unprivileged_nginx_main_config_owns_pid_and_http_context():
    config = (ROOT / "web" / "nginx-main.conf").read_text(encoding="utf-8")
    assert "pid /tmp/nginx.pid;" in config
    assert "error_log /dev/stderr warn;" in config
    assert "include /etc/nginx/conf.d/*.conf;" in config
    assert "events {" in config


def test_nginx_entrypoint_normalizes_and_redacts_error_stream(tmp_path):
    fake_nginx = tmp_path / "nginx"
    fake_nginx.write_text(
        "#!/bin/sh\n"
        "echo '2026/10/18 [error] upstream failed password=hunter2 https://user:secret@example.test/?token=abc' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_nginx.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "web" / "nginx-entrypoint.sh")],
        env={**os.environ, "NGINX_BIN": str(fake_nginx)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    events = [json.loads(line) for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    assert [event["event"] for event in events] == ["nginx.starting", "nginx.error", "nginx.stopped"]
    error = events[1]
    assert error["level"] == "ERROR"
    assert "hunter2" not in error["message"]
    assert "secret" not in error["message"]
    assert "token=abc" not in error["message"]
    assert "[REDACTED]" in error["message"]
