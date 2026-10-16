param(
    [switch]$SkipInstall
)

# One-click local setup and startup for Windows PowerShell.
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot ".."))
$EnvName = "test-atelier"
$LogDir = Join-Path $RootDir ".test-atelier-logs"
$DepMarker = Join-Path $LogDir "dependencies-$EnvName.sha256"
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
if ($envList -notmatch "(?m)^$([regex]::Escape($EnvName))\s") {
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

if (-not $SkipInstall -and $needsInstall) {
    Write-Host "安装 Python 依赖 ..."
    & $CondaExe run --no-capture-output -n $EnvName python -m pip install -r (Join-Path $RootDir "api\requirements-dev.txt")
    Write-Host "安装前端依赖 ..."
    Push-Location (Join-Path $RootDir "web")
    try { & npm ci } finally { Pop-Location }
    Set-Content -NoNewline -Path $DepMarker -Value $dependencyFingerprint
} elseif (-not $SkipInstall) {
    Write-Host "依赖未变化，跳过 Python 和前端依赖安装"
}

$ConfigDir = Join-Path $env:USERPROFILE ".config\ai-studio"
$SecretFile = Join-Path $ConfigDir "session-secret"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if (-not (Test-Path $SecretFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant() | Set-Content -NoNewline -Path $SecretFile
}
$env:AI_STUDIO_SESSION_SECRET = (Get-Content -Raw $SecretFile).Trim()

if (-not $env:AI_STUDIO_ADMIN_PASSWORD) {
    $securePassword = Read-Host "管理员密码（回车使用个人默认值 123456）" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try { $env:AI_STUDIO_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
if (-not $env:AI_STUDIO_ADMIN_PASSWORD) { $env:AI_STUDIO_ADMIN_PASSWORD = "123456" }
if ($env:AI_STUDIO_ADMIN_PASSWORD.Length -lt 6) { Fail "管理员密码至少需要 6 位" }

Write-Host "执行数据库迁移 ..."
Push-Location $RootDir
try { & $CondaExe run --no-capture-output -n $EnvName python -m alembic upgrade head } finally { Pop-Location }

$apiLog = Join-Path $LogDir "api.log"
$apiErrorLog = Join-Path $LogDir "api-error.log"
Write-Host "启动 API（日志：$apiLog）..."
$ApiProcess = Start-Process -FilePath $CondaExe -ArgumentList @(
    "run", "--no-capture-output", "-n", $EnvName, "python", "-m", "uvicorn", "api.main:app",
    "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $RootDir -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -PassThru

$apiReady = $false
for ($i = 0; $i -lt 20; $i++) {
    if ($ApiProcess.HasExited) { break }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/auth/csrf" -UseBasicParsing -TimeoutSec 2 | Out-Null
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

try {
    Write-Host "启动前端：http://127.0.0.1:5173"
    Write-Host "关闭此窗口可停止前端；API 进程会在脚本退出时停止"
    Push-Location (Join-Path $RootDir "web")
    & npm run dev -- --host 127.0.0.1
}
finally {
    Pop-Location
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        & taskkill.exe /PID $ApiProcess.Id /T /F | Out-Null
    }
}
