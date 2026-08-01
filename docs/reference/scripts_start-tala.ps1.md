# File: `start-tala.ps1`

## Overview

Windows launch script for starting Tala.

## File Contents

```powershell
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

Write-Host "Starting Tala Python Backend..."
$BackendProcess = Start-Process -NoNewWindow -PassThru -FilePath $VenvPython -ArgumentList "-m", "python.harness.server"

try {
    Write-Host "Starting Tala Electron UI..."
    # Use npm start for electron
    $FrontendProcess = Start-Process -NoNewWindow -PassThru -FilePath "npm.cmd" -ArgumentList "start"

    Write-Host "Tala is running. Press Ctrl+C to terminate." -ForegroundColor Green

    # Wait for frontend to exit, then kill backend
    Wait-Process -Id $FrontendProcess.Id
} finally {
    Write-Host "UI closed or interrupted. Shutting down backend..."
    if ($BackendProcess -and -not $BackendProcess.HasExited) {
        Stop-Process -Id $BackendProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
}

```
