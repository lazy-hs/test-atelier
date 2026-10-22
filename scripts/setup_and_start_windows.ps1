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
$BootstrapMarker = Join-Path $LogDir "admin-initialized"
$ApiProcess = $null

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if (-not (Test-Path (Join-Path $RootDir "api")) -or -not (Test-Path (Join-Path $RootDir "web"))) {
    Fail "请从 test-atelier 工程目录运行此脚本"
}

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
$needsInstall = $envCreated -or $env:TEST_ATELIER_FORCE_INSTALL -eq "1" -or $markerValue -ne $dependencyFingerprint -or -not (Test-Path (Join-Path $RootDir "web\node_modules"))

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

$ConfigDir = Join-Path $env:USERPROFILE ".config\ai-studio"
$SecretFile = Join-Path $ConfigDir "session-secret"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if ($SkipConfigure -and -not (Test-Path $SecretFile)) {
    Fail "未找到会话密钥，请先运行 configure_environment_windows.ps1"
}
if (-not $SkipConfigure -and -not (Test-Path $SecretFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant() | Set-Content -NoNewline -Path $SecretFile
}
$env:AI_STUDIO_SESSION_SECRET = (Get-Content -Raw $SecretFile).Trim()
$env:AI_STUDIO_REQUIRE_MIGRATIONS = "1"
$env:AI_STUDIO_MIGRATION_HEAD = "20261032_case_timestamps"
# This launcher starts only the API and Vite. Do not inherit a production
# external-worker flag or queued AI jobs would have no consumer.
$env:AI_STUDIO_EXTERNAL_WORKER = "0"
$env:AI_STUDIO_ENV = "development"

if (-not $ConfigureOnly -and ($envCreated -or -not (Test-Path $BootstrapMarker))) {
    if (-not $env:AI_STUDIO_ADMIN_PASSWORD) {
        $securePassword = Read-Host "管理员密码（至少 6 位，直接回车使用默认密码 123456）" -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        try { $env:AI_STUDIO_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    }
    if (-not $env:AI_STUDIO_ADMIN_PASSWORD) { $env:AI_STUDIO_ADMIN_PASSWORD = "123456" }
    if ($env:AI_STUDIO_ADMIN_PASSWORD.Length -lt 6) { Fail "管理员密码至少需要 6 位" }
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
if (-not $env:AI_STUDIO_API_TARGET) { $env:AI_STUDIO_API_TARGET = "http://127.0.0.1:$ApiPort" }

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
    Fail "API 未在 20 秒内就绪"
}
New-Item -ItemType File -Force -Path $BootstrapMarker | Out-Null

try {
    Write-Host "启动前端：http://127.0.0.1:$WebPort"
    Write-Host "关闭此窗口可停止前端；API 进程会在脚本退出时停止"
    Push-Location (Join-Path $RootDir "web")
    & npm run dev -- --host 127.0.0.1 --port $WebPort
}
finally {
    Pop-Location
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        & taskkill.exe /PID $ApiProcess.Id /T /F | Out-Null
    }
}
