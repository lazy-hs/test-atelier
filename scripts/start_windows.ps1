param()

# Start services after configure_environment_windows.ps1 has completed.
$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "setup_and_start_windows.ps1"
& $launcher -SkipConfigure
exit $LASTEXITCODE
