param(
    [switch]$SkipInstall
)

# Configure the local Conda and Node environment without starting services.
$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "setup_and_start_windows.ps1"
& $launcher -ConfigureOnly -SkipInstall:$SkipInstall
exit $LASTEXITCODE
