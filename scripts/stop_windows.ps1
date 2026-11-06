param()

# Stop the API started by setup_and_start_windows.ps1 without touching unrelated processes.
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$PidFile = Join-Path $RootDir ".test-atelier-logs\api.pid"
$ProjectorPidFile = Join-Path $RootDir ".test-atelier-logs\harness-projector.pid"

function Stop-RecordedProcess([string]$Path, [string]$Marker, [string]$Label) {
    if (-not (Test-Path $Path)) {
        Write-Host "Test Atelier $Label 未发现运行中的 PID 文件"
        return $true
    }
    $rawPid = (Get-Content -Raw $Path).Trim()
    $processId = 0
    if (-not [int]::TryParse($rawPid, [ref]$processId) -or $processId -le 0) {
        Remove-Item -Force -ErrorAction SilentlyContinue $Path
        Write-Host "Test Atelier $Label PID 文件无效，已清理"
        return $true
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -Force -ErrorAction SilentlyContinue $Path
        Write-Host "Test Atelier $Label 已停止（清理过期 PID）"
        return $true
    }
    $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue).CommandLine
    if ($commandLine -notlike "*$Marker*") {
        Write-Warning "PID $processId 不是由 Test Atelier 启动的 $Label，未执行停止"
        return $false
    }
    & taskkill.exe /PID $processId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-Warning "Test Atelier $Label 无法停止 PID $processId，保留 PID 文件"
        return $false
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $Path
    Write-Host "Test Atelier $Label 已停止"
    return $true
}

$projectorStopped = Stop-RecordedProcess $ProjectorPidFile "api.harness_projector" "Harness Projector"
$apiStopped = Stop-RecordedProcess $PidFile "api.uvicorn_entrypoint" "API"
if (-not $projectorStopped -or -not $apiStopped) { exit 1 }
