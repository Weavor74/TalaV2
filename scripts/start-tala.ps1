$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Starting Tala Cognitive Engine Room" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = Split-Path -Parent $ScriptDir

Push-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython) -or -not (Test-Path (Join-Path $Root ".env"))) {
    Write-Host "First-run setup is incomplete. Running setup now..." -ForegroundColor Yellow
    uv run python scripts/first_run.py
}

try {
    Write-Host "Starting Tala Electron UI..."
    # Electron owns backend startup, restart and shutdown. Starting another
    # harness here races its port cleanup and creates two competing owners.
    & npm.cmd start
    $TalaExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $TalaExitCode
