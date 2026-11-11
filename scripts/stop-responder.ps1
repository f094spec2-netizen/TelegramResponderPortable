$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $projectRoot ".state\responder.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "Responder is not recorded as running."
    exit 0
}

$responderPid = Get-Content -LiteralPath $pidFile -ErrorAction Stop
if ($responderPid -notmatch '^\d+$') {
    throw "Invalid PID file: $pidFile"
}

$process = Get-Process -Id $responderPid -ErrorAction SilentlyContinue
if ($process) {
    $expectedPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
    $actualPath = $process.Path
    if (-not $actualPath -or -not $actualPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "PID $responderPid is not the expected responder process; refusing to stop it."
    }
    Stop-Process -Id $responderPid
    Write-Output "Responder PID $responderPid stopped."
}
else {
    Write-Output "Recorded responder PID $responderPid is no longer running."
}

Remove-Item -LiteralPath $pidFile -Force
