param(
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonWindowless = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$envFile = Join-Path $projectRoot ".env"
$logFile = Join-Path $projectRoot "logs\responder.log"
$pidFile = Join-Path $projectRoot ".state\responder.pid"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Follow README.md installation first."
}
if (-not (Test-Path -LiteralPath $envFile)) {
    throw ".env not found. Copy .env.example to .env and configure the test account first."
}

if (-not $Background) {
    Push-Location $projectRoot
    try {
        & $python -m telegram_test_responder.cli --env-file $envFile run
    }
    finally {
        Pop-Location
    }
    exit $LASTEXITCODE
}

$stateDir = Split-Path -Parent $pidFile
$logDir = Split-Path -Parent $logFile
New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        throw "Responder is already running with PID $existingPid."
    }
}

$arguments = @(
    "-m",
    "telegram_test_responder.cli",
    "--env-file",
    ".env",
    "--log-file",
    "logs/responder.log",
    "run"
)
$process = Start-Process `
    -FilePath $pythonWindowless `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
$process.Id | Set-Content -LiteralPath $pidFile -Encoding ascii

Start-Sleep -Milliseconds 500
if ($process.HasExited) {
    Remove-Item -LiteralPath $pidFile -Force
    throw "Responder exited during startup. Check $logFile."
}

Write-Output "Responder started in background with PID $($process.Id)."
Write-Output "Log: $logFile"
