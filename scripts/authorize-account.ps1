$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$responder = Join-Path $projectRoot ".venv\Scripts\telegram-test-responder.exe"
$envFile = Join-Path $projectRoot ".env"

Set-Location -LiteralPath $projectRoot
Write-Host "Telegram test account authorization" -ForegroundColor Cyan
Write-Host "Enter the phone number, login code, and 2FA password only in this window."
Write-Host ""

& $responder --env-file $envFile inspect
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "Authorization completed. Return to Codex and say: authorized" -ForegroundColor Green
}
else {
    Write-Host "Authorization failed with exit code $exitCode." -ForegroundColor Red
}
Read-Host "Press Enter to close this window"
exit $exitCode
