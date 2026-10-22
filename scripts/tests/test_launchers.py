from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_local_launchers_do_not_inherit_an_unstarted_external_worker():
    macos = (ROOT / "scripts" / "setup_and_start_macos.command").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "setup_and_start_windows.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "setup_and_start_linux.sh").read_text(encoding="utf-8")

    assert "export AI_STUDIO_EXTERNAL_WORKER=0" in macos
    assert '$env:AI_STUDIO_EXTERNAL_WORKER = "0"' in windows
    assert "export AI_STUDIO_EXTERNAL_WORKER=0" in linux


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

    assert "TEST_ATELIER_CONFIG_ONLY=1 TEST_ATELIER_SKIP_CONFIG=0" in mac_config
    assert "TEST_ATELIER_CONFIG_ONLY=0 TEST_ATELIER_SKIP_CONFIG=1" in mac_start
    assert "TEST_ATELIER_CONFIG_ONLY=1 TEST_ATELIER_SKIP_CONFIG=0" in linux_config
    assert "TEST_ATELIER_CONFIG_ONLY=0 TEST_ATELIER_SKIP_CONFIG=1" in linux_start
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
