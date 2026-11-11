$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$entry = Join-Path $projectRoot "src\telegram_test_responder\gui.py"
$dist = Join-Path $projectRoot "dist"
$portable = Join-Path $dist "TelegramResponderPortable"
$work = Join-Path $projectRoot "build\pyinstaller"
$spec = Join-Path $projectRoot "build\spec"
$portableExe = Join-Path $portable "TelegramResponder.exe"
$portableZip = Join-Path $dist "TelegramResponderPortable.zip"

New-Item -ItemType Directory -Force -Path $dist, $work, $spec | Out-Null
$distFull = [IO.Path]::GetFullPath($dist).TrimEnd('\') + '\'
$portableFull = [IO.Path]::GetFullPath($portable)
if (-not $portableFull.StartsWith($distFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable output escaped dist directory: $portableFull"
}
if (Test-Path -LiteralPath $portableFull) {
    Remove-Item -LiteralPath $portableFull -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $portableFull | Out-Null
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "TelegramResponder" `
    --distpath $portable `
    --workpath $work `
    --specpath $spec `
    $entry

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$marker = @{ format = 1; app = "TelegramResponder" } | ConvertTo-Json -Compress
Set-Content -LiteralPath (Join-Path $portable "portable.json") -Value $marker -Encoding utf8NoBOM
Copy-Item `
    -LiteralPath (Join-Path $projectRoot "assets\PORTABLE_README.txt") `
    -Destination (Join-Path $portable "使用说明.txt") `
    -Force
Compress-Archive -LiteralPath $portable -DestinationPath $portableZip -Force

Write-Output "Built EXE: $portableExe"
Write-Output "Built ZIP: $portableZip"
