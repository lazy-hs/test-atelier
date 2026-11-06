import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_local_launchers_use_runtime_json_for_worker_mode():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")

    for launcher in (macos, windows, linux):
        assert "config\\runtime.json" in launcher or "config/runtime.json" in launcher
        assert "AI_STUDIO_EXTERNAL_WORKER=0" not in launcher


def test_local_launchers_use_structured_uvicorn_wrapper():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")

    assert "python -m api.uvicorn_entrypoint" in macos
    assert '"python", "-m", "api.uvicorn_entrypoint"' in windows
    assert "python -m api.uvicorn_entrypoint" in linux


def test_local_launchers_read_ports_from_settings():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")

    assert "read_server_ports.py" in macos and '"$API_PORT"' in macos
    assert "read_server_ports.py" in linux and '"$API_PORT"' in linux
    assert "read_server_ports.py" in windows and "$ApiPort" in windows


def test_local_launchers_pin_vite_proxy_to_api_port():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")

    assert 'AI_STUDIO_API_TARGET="http://127.0.0.1:$API_PORT"' in macos
    assert 'AI_STUDIO_API_TARGET="http://127.0.0.1:$API_PORT"' in linux
    assert '$env:AI_STUDIO_API_TARGET = "http://127.0.0.1:$ApiPort"' in windows


def test_linux_launcher_uses_linux_shell_tools_and_is_executable():
    launcher = ROOT / "scripts" / "setup_and_start_linux.sh"
    content = launcher.read_text(encoding="utf-8")

    assert content.startswith("#!/usr/bin/env bash")
    assert "sha256sum" in content
    assert "stat -c%s" in content
    assert launcher.stat().st_mode & 0o111


def test_environment_configuration_and_startup_are_separate_per_platform():
    mac_config = (ROOT / "scripts" / "configure_environment_macos.command").read_text(encoding="utf-8")
    mac_start = (ROOT / "scripts" / "start_macos.command").read_text(encoding="utf-8")
    linux_config = (ROOT / "scripts" / "configure_environment_linux.sh").read_text(encoding="utf-8")
    linux_start = (ROOT / "scripts" / "start_linux.sh").read_text(encoding="utf-8")
    windows_config = (ROOT / "scripts" / "configure_environment_windows.ps1").read_text(encoding="utf-8")
    windows_start = (ROOT / "scripts" / "start_windows.ps1").read_text(encoding="utf-8")

    assert '--config-only' in mac_config
    assert '--skip-config' in mac_start
    assert '--config-only' in linux_config
    assert '--skip-config' in linux_start
    assert "-ConfigureOnly" in windows_config
    assert "-SkipConfigure" in windows_start
    for name in ("configure_environment_macos.command", "start_macos.command", "configure_environment_linux.sh", "start_linux.sh"):
        assert (ROOT / "scripts" / name).stat().st_mode & 0o111


def test_startup_path_does_not_install_or_migrate():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")

    # The split launchers use SKIP_CONFIG/SkipConfigure, so migrations and
    # dependency installation remain configuration-stage operations only.
    assert 'if [[ "$SKIP_CONFIG" != "1" ]]; then\n  print "执行数据库迁移 ..."' in macos
    assert 'if [[ "$SKIP_CONFIG" != "1" ]]; then\n  printf \'执行数据库迁移 ...\\n\'' in linux
    assert 'if (-not $SkipConfigure) {\n    Write-Host "执行数据库迁移 ..."' in windows


def test_startup_launchers_publish_api_pid_file_and_cleanup_it():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    assert 'API_PID_FILE="$LOG_DIR/api.pid"' in macos
    assert 'API_PID_FILE="$LOG_DIR/api.pid"' in linux
    assert '$ApiPidFile = Join-Path $LogDir "api.pid"' in windows
    assert 'rm -f "$API_PID_FILE"' in macos and 'rm -f "$API_PID_FILE"' in linux
    assert 'Remove-Item -Force -ErrorAction SilentlyContinue $ApiPidFile' in windows


def test_stop_launchers_are_scoped_to_recorded_api_process():
    linux = (ROOT / "scripts" / "stop_linux.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "stop_windows.ps1").read_text(encoding="utf-8")
    assert 'api.uvicorn_entrypoint' in linux
    assert 'api.uvicorn_entrypoint' in windows
    assert '$RootDir = (Resolve-Path (Join-Path $PSScriptRoot ".."))' in windows
    assert '$PidFile = Join-Path $RootDir ".test-atelier-logs\\api.pid"' in windows
    assert 'Stop-RecordedProcess $PidFile "api.uvicorn_entrypoint" "API"' in windows
    assert 'Stop-RecordedProcess $ProjectorPidFile "api.harness_projector" "Harness Projector"' in windows
    for name in ("stop_linux.sh", "stop_macos.command"):
        assert (ROOT / "scripts" / name).stat().st_mode & 0o111


def test_windows_start_wrapper_propagates_script_failure_without_stale_native_exit_code():
    start = (ROOT / "scripts" / "start_windows.ps1").read_text(encoding="utf-8")
    assert '& $launcher -SkipConfigure' in start
    assert 'if (-not $?) { exit 1 }' in start
    assert 'exit $LASTEXITCODE' not in start


def test_platform_support_documents_stop_contract():
    support = (ROOT / "docs" / "platform-support.md").read_text(encoding="utf-8")
    for name in ("stop_macos.command", "stop_windows.ps1", "stop_windows.bat", "stop_linux.sh"):
        assert name in support
    assert ".test-atelier-logs/api.pid" in support


def test_local_launchers_start_a_separate_projector_when_harness_is_configured():
    for name in ("setup_and_start_macos.command", "setup_and_start_linux.sh", "setup_and_start_windows.ps1"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "api.harness_projector" in source
        assert "runtime_environment" in source or "runtimeValues" in source
        assert "TEST_ATELIER_HARNESS_URL" in source
        assert "harness_secret_file.py" in source
        assert "harness-projector.pid" in source
        assert "harness-projector-heartbeat" in source


def test_macos_web_shell_selects_profile_without_forwarding_web_argument():
    source = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    assert "--profile test-atelier-web " + "\\" in source
    assert "--profile test-atelier-web web" not in source


def test_web_shell_profile_install_is_cached_after_first_successful_install():
    setup = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    full = (ROOT / "scripts" / "start_full_macos.command").read_text(encoding="utf-8")
    for source in (setup, full):
        assert "test-atelier-web-profile-installed" in source
        assert "profiles/test-atelier-web" in source
        assert "首次安装 Harness Web Shell Profile" in source or "install_profile.py --with-web" in source
        assert "跳过安装" in source
        assert "touch \"$WEB_PROFILE_MARKER\"" in source


def test_macos_launcher_reuses_only_a_healthy_api_from_this_checkout():
    source = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    assert "EXISTING_API_COMMAND" in source
    assert "api.uvicorn_entrypoint" in source
    assert "/api/v1/health/ready" in source
    assert "端口 $API_PORT 已被其他进程占用" in source


def test_macos_web_shell_readiness_accepts_authenticated_http_response():
    source = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    assert "web_shell_reachable()" in source
    assert "only a connection failure means the server is not listening yet" in source
    assert "curl -sS -o /dev/null -w '%{http_code}'" in source


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-control smoke")
@pytest.mark.parametrize("marker,should_stop", [("api.harness_projector", True), ("unrelated-process", False)])
def test_linux_stop_scopes_projector_to_its_recorded_command(tmp_path, marker, should_stop):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "stop_linux.sh", scripts / "stop_linux.sh")
    logs = tmp_path / ".test-atelier-logs"
    logs.mkdir()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    try:
        (logs / "harness-projector.pid").write_text(str(process.pid), encoding="ascii")
        stopped = subprocess.run(["bash", str(scripts / "stop_linux.sh")], capture_output=True, text=True, timeout=12)
        assert stopped.returncode == (0 if should_stop else 1), stopped.stderr
        assert (process.poll() is not None) == should_stop
        assert (logs / "harness-projector.pid").exists() != should_stop
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
