param(
    [switch]$SkipInstall,
    [switch]$ConfigureOnly,
    [switch]$SkipConfigure
)

# One-click local setup and startup for Windows PowerShell.
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$EnvName = "test-atelier"
$LogDir = Join-Path $RootDir ".test-atelier-logs"
$DepMarker = Join-Path $LogDir "dependencies-$EnvName.sha256"
$ApiPidFile = Join-Path $LogDir "api.pid"
$ProjectorPidFile = Join-Path $LogDir "harness-projector.pid"
$BootstrapMarker = Join-Path $LogDir "admin-initialized"
$ApiProcess = $null
$ProjectorProcess = $null
$RuntimeConfig = Join-Path $RootDir "config\runtime.json"

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if (-not (Test-Path (Join-Path $RootDir "api")) -or -not (Test-Path (Join-Path $RootDir "web"))) {
    Fail "请从 test-atelier 工程目录运行此脚本"
}
if (-not (Test-Path $RuntimeConfig -PathType Leaf)) { Fail "未找到配置文件 $RuntimeConfig，请先运行 scripts\configure_full_runtime.py" }

$Npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $Npm) { Fail "未找到 npm，请先安装 Node.js 20+" }

$Conda = Get-Command conda -ErrorAction SilentlyContinue
if (-not $Conda) {
    $Candidates = @(
        (Join-Path $env:USERPROFILE "miniconda3\Scripts\conda.exe"),
        (Join-Path $env:USERPROFILE "anaconda3\Scripts\conda.exe"),
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "C:\ProgramData\Anaconda3\Scripts\conda.exe"
    )
    $CondaPath = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($CondaPath) { $Conda = @{ Source = $CondaPath } }
}
if (-not $Conda) { Fail "未找到 Conda，请先安装 Miniconda 或 Anaconda" }
$CondaExe = $Conda.Source
if (-not $CondaExe) { $CondaExe = $Conda.Path }
if (-not $CondaExe) { Fail "无法解析 Conda 可执行文件路径" }

$envList = & $CondaExe env list | Out-String
$envCreated = $false
if ($SkipConfigure) {
    if ($envList -notmatch "(?m)^$([regex]::Escape($EnvName))\s") {
        Fail "未找到 Conda 环境 $EnvName，请先运行 configure_environment_windows.ps1"
    }
} elseif ($envList -notmatch "(?m)^$([regex]::Escape($EnvName))\s") {
    Write-Host "创建 Conda 环境 $EnvName ..."
    & $CondaExe create -n $EnvName python=3.12 -y
    $envCreated = $true
}

$LogDir = New-Item -ItemType Directory -Force -Path $LogDir

$requirementsHash = (Get-FileHash (Join-Path $RootDir "api\requirements-dev.txt") -Algorithm SHA256).Hash
$packageLockHash = (Get-FileHash (Join-Path $RootDir "web\package-lock.json") -Algorithm SHA256).Hash
$dependencyFingerprint = [BitConverter]::ToString(
    [Security.Cryptography.SHA256]::Create().ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($requirementsHash + $packageLockHash)
    )
).Replace('-', '').ToLowerInvariant()
$markerValue = if (Test-Path $DepMarker) { (Get-Content -Raw $DepMarker).Trim() } else { "" }
$needsInstall = $envCreated -or $markerValue -ne $dependencyFingerprint -or -not (Test-Path (Join-Path $RootDir "web\node_modules"))

if (-not $SkipConfigure -and -not $SkipInstall -and $needsInstall) {
    Write-Host "安装 Python 依赖 ..."
    & $CondaExe run --no-capture-output -n $EnvName python -m pip install -r (Join-Path $RootDir "api\requirements-dev.txt")
    Write-Host "安装前端依赖 ..."
    Push-Location (Join-Path $RootDir "web")
    try { & npm ci } finally { Pop-Location }
    Set-Content -NoNewline -Path $DepMarker -Value $dependencyFingerprint
} elseif (-not $SkipConfigure -and -not $SkipInstall) {
    Write-Host "依赖未变化，跳过 Python 和前端依赖安装"
} elseif ($SkipConfigure -and -not (Test-Path (Join-Path $RootDir "web\node_modules"))) {
    Fail "未找到前端依赖，请先运行 configure_environment_windows.ps1"
}

$runtimeDocument = Get-Content -Raw -Path $RuntimeConfig | ConvertFrom-Json
$ConfigDir = Split-Path -Parent ([Environment]::ExpandEnvironmentVariables([string]$runtimeDocument.test_atelier.session_secret_file))
$SecretFile = [Environment]::ExpandEnvironmentVariables([string]$runtimeDocument.test_atelier.session_secret_file)
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if ($SkipConfigure -and -not (Test-Path $SecretFile)) {
    Fail "未找到会话密钥，请先运行 configure_environment_windows.ps1"
}
if (-not $SkipConfigure -and -not (Test-Path $SecretFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant() | Set-Content -NoNewline -Path $SecretFile
}
if (-not $ConfigureOnly -and ($envCreated -or -not (Test-Path $BootstrapMarker))) {
    $adminPasswordFile = [Environment]::ExpandEnvironmentVariables([string]$runtimeDocument.test_atelier.admin_password_file)
    if (-not (Test-Path $adminPasswordFile -PathType Leaf)) { Fail "未找到管理员密码文件，请运行 scripts\configure_full_runtime.py" }
}

if (-not $SkipConfigure) {
    Write-Host "执行数据库迁移 ..."
    Push-Location $RootDir
    try { & $CondaExe run --no-capture-output -n $EnvName python -m alembic upgrade head } finally { Pop-Location }
}

if ($ConfigureOnly) {
    Write-Host "运行环境配置完成；请执行 scripts\start_windows.ps1 启动服务"
    exit 0
}

$portsOutput = & $CondaExe run --no-capture-output -n $EnvName python (Join-Path $RootDir "scripts\read_server_ports.py")
$ports = ($portsOutput | Select-Object -Last 1).Trim() -split '\s+'
if ($ports.Count -ne 2 -or $ports[0] -notmatch '^[0-9]+$' -or $ports[1] -notmatch '^[0-9]+$') {
    Fail "settings.json 中的端口配置无效"
}
$ApiPort = [int]$ports[0]
$WebPort = [int]$ports[1]
if ($ApiPort -lt 1 -or $ApiPort -gt 65535 -or $WebPort -lt 1 -or $WebPort -gt 65535) {
    Fail "settings.json 中的端口配置无效"
}
$runtimeValues = & $CondaExe run --no-capture-output -n $EnvName python -c "from api.runtime_config import runtime_environment; import json; print(json.dumps(runtime_environment()))" | ConvertFrom-Json
if ($runtimeValues.TEST_ATELIER_HARNESS_URL) {
    & $CondaExe run --no-capture-output -n $EnvName python (Join-Path $RootDir "scripts/harness_secret_file.py") --config $RuntimeConfig
    if ($LASTEXITCODE -ne 0) { Fail "已配置 Harness Host，但作用域密钥无效或不可读取" }
} elseif ($runtimeValues.TEST_ATELIER_HARNESS_ONLY -eq "1") {
    Fail "Harness-only 模式必须先配置 TEST_ATELIER_HARNESS_URL"
}

$apiLog = Join-Path $LogDir "api.log"
$apiErrorLog = Join-Path $LogDir "api-error.log"
foreach ($logPath in @($apiLog, $apiErrorLog)) {
    if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 50MB)) {
        Move-Item -Force $logPath "$logPath.1"
    }
}
Write-Host "启动 API（日志：$apiLog）..."
$ApiProcess = Start-Process -FilePath $CondaExe -ArgumentList @(
    "run", "--no-capture-output", "-n", $EnvName, "python", "-m", "api.uvicorn_entrypoint",
    "--host", "127.0.0.1", "--port", "$ApiPort"
) -WorkingDirectory $RootDir -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -PassThru
$ApiProcess.Id | Set-Content -NoNewline -Path $ApiPidFile

$apiReady = $false
for ($i = 0; $i -lt 20; $i++) {
    if ($ApiProcess.HasExited) { break }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$ApiPort/api/v1/health/ready" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $apiReady = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $apiReady) {
    if (Test-Path $apiLog) { Get-Content $apiLog }
    if (Test-Path $apiErrorLog) { Get-Content $apiErrorLog }
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        & taskkill.exe /PID $ApiProcess.Id /T /F | Out-Null
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $ApiPidFile
    Fail "API 未在 20 秒内就绪"
}
New-Item -ItemType File -Force -Path $BootstrapMarker | Out-Null

try {
    if ($runtimeValues.TEST_ATELIER_HARNESS_URL) {
        # Use runtime.json rather than assuming the legacy harness-projector-heartbeat log location.
        $projectorHeartbeat = [Environment]::ExpandEnvironmentVariables([string]$runtimeValues.TEST_ATELIER_HARNESS_PROJECTOR_HEARTBEAT)
        if (-not $projectorHeartbeat) { throw "runtime.json 未配置 Projector 心跳路径" }
        $projectorHeartbeat = [System.IO.Path]::GetFullPath($projectorHeartbeat)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $projectorHeartbeat) | Out-Null
        Remove-Item -Force -ErrorAction SilentlyContinue $projectorHeartbeat
        $projectorLog = Join-Path $LogDir "harness-projector.log"
        $projectorErrorLog = Join-Path $LogDir "harness-projector-error.log"
        foreach ($logPath in @($projectorLog, $projectorErrorLog)) {
            if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 50MB)) {
                Move-Item -Force $logPath "$logPath.1"
            }
        }
        Write-Host "启动 Harness Projector（日志：$projectorLog）..."
        $ProjectorProcess = Start-Process -FilePath $CondaExe -ArgumentList @(
            "run", "--no-capture-output", "-n", $EnvName, "python", "-m", "api.harness_projector"
        ) -WorkingDirectory $RootDir -RedirectStandardOutput $projectorLog -RedirectStandardError $projectorErrorLog -PassThru
        $ProjectorProcess.Id | Set-Content -NoNewline -Path $ProjectorPidFile
        for ($i = 0; $i -lt 20; $i++) {
            if (Test-Path $projectorHeartbeat) { break }
            if ($ProjectorProcess.HasExited) { break }
            Start-Sleep -Seconds 1
        }
        if ($ProjectorProcess.HasExited -or -not (Test-Path $projectorHeartbeat)) {
            throw "Harness Projector 未在 20 秒内就绪，请查看 $projectorLog 和 $projectorErrorLog"
        }
    }
    Write-Host "启动前端：http://127.0.0.1:$WebPort"
    Write-Host "关闭此窗口可停止前端；API 与 Harness Projector 会在脚本退出时停止"
    # Keep Vite's development proxy aligned with the API process port.
    $env:AI_STUDIO_API_TARGET = "http://127.0.0.1:$ApiPort"
    Push-Location (Join-Path $RootDir "web")
    try { & npm run dev -- --host 127.0.0.1 --port $WebPort }
    finally { Pop-Location }
}
finally {
    if ($projectorHeartbeat) {
        Remove-Item -Force -ErrorAction SilentlyContinue $projectorHeartbeat
    }
    if ($ProjectorProcess -and -not $ProjectorProcess.HasExited) {
        & taskkill.exe /PID $ProjectorProcess.Id /T /F | Out-Null
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $ProjectorPidFile
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        & taskkill.exe /PID $ApiProcess.Id /T /F | Out-Null
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $ApiPidFile
}
