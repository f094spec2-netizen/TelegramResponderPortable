param(
    [ValidateRange(0, 200)]
    [int]$Tail = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pidFile = Join-Path $projectRoot ".state\responder.pid"
$statusFile = Join-Path $projectRoot ".state\status.json"
$logFile = Join-Path $projectRoot "logs\responder.log"
$expectedPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"

$running = $false
$responderPid = $null
if (Test-Path -LiteralPath $pidFile) {
    $responderPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($responderPid -match '^\d+$') {
        $process = Get-Process -Id $responderPid -ErrorAction SilentlyContinue
        if ($process -and $process.Path -and
            $process.Path.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            $running = $true
        }
    }
}

$health = "STOPPED"
$status = $null
$heartbeatAge = $null
if (Test-Path -LiteralPath $statusFile) {
    try {
        $status = Get-Content -Raw -LiteralPath $statusFile | ConvertFrom-Json
        $updated = [DateTimeOffset]::Parse($status.updated_at)
        $heartbeatAge = [math]::Round(
            ([DateTimeOffset]::UtcNow - $updated.ToUniversalTime()).TotalSeconds,
            1
        )
        $freshLimit = ([int]$status.heartbeat_seconds * 2) + 10
        if ($running -and $status.connected -and $heartbeatAge -le $freshLimit) {
            $health = "HEALTHY"
        }
        elseif ($running) {
            $health = "UNHEALTHY"
        }
    }
    catch {
        if ($running) {
            $health = "UNHEALTHY"
        }
    }
}
elseif ($running) {
    $health = "STARTING"
}

Write-Output "Health: $health"
Write-Output "Process: $(if ($running) { "running (PID $responderPid)" } else { 'not running' })"
if ($status) {
    Write-Output "Telegram connected: $($status.connected)"
    Write-Output "Mode: $(if ($status.dry_run) { 'DRY-RUN (no messages sent)' } else { 'LIVE SEND' })"
    Write-Output "Phase: $($status.phase)"
    Write-Output "Last heartbeat: $($status.updated_at) ($heartbeatAge seconds ago)"
    Write-Output "Pending replies: $($status.pending_replies)"
    Write-Output "Last received: $($status.last_received_at) message=$($status.last_received_message_id)"
    Write-Output "Last sent: $($status.last_reply_at) message=$($status.last_replied_message_id)"
    if ($status.last_error) {
        Write-Output "Last error: $($status.last_error)"
    }
}

if ($Tail -gt 0) {
    Write-Output ""
    Write-Output "Last $Tail log lines:"
    if (Test-Path -LiteralPath $logFile) {
        Get-Content -LiteralPath $logFile -Tail $Tail
    }
    else {
        Write-Output "No log file yet: $logFile"
    }
}

if ($health -notin @("HEALTHY", "STARTING")) {
    exit 1
}
