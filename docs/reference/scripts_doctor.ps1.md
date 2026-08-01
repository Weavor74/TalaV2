# File: `doctor.ps1`

## Overview

Windows diagnostic script for environment validation.

## File Contents

```powershell
param (
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = Split-Path -Parent $ScriptDir

function Write-DoctorItem {
    param([string]$Name, [string]$Status, [string]$Details, [bool]$IsError)
    $Color = if ($IsError) { "Red" } elseif ($Status -eq "OK") { "Green" } else { "Yellow" }
    Write-Host "[$Status] " -ForegroundColor $Color -NoNewline
    Write-Host "$Name" -NoNewline
    if ($Details) {
        Write-Host " - $Details" -ForegroundColor DarkGray
    } else {
        Write-Host ""
    }
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Tala Doctor - Windows Environment Check" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$HasErrors = $false
$HasCriticalErrors = $false

# 1. OS Info
$OS = Get-CimInstance Win32_OperatingSystem
Write-DoctorItem "OS Version" "OK" "$($OS.Caption) ($($OS.OSArchitecture))" $false

# 2. Check Admin Rights
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($IsAdmin) {
    Write-DoctorItem "Admin Rights" "OK" "Running as Administrator" $false
} else {
    Write-DoctorItem "Admin Rights" "WARN" "Not running as Administrator. Some installations may fail." $false
}

# 3. Check Node.js
try {
    $NodeVer = (node -v 2>$null).Trim()
    if ($LASTEXITCODE -eq 0) {
        Write-DoctorItem "Node.js" "OK" "Found version $NodeVer" $false
    } else {
        throw "Node failed"
    }
} catch {
    Write-DoctorItem "Node.js" "FAIL" "Node.js not found in PATH" $true
    $HasErrors = $true
}

# 4. Check uv
try {
    $UvVer = (uv --version 2>$null).Trim()
    if ($LASTEXITCODE -eq 0) {
        Write-DoctorItem "Python (uv)" "OK" "Found $UvVer" $false
    } else {
        throw "uv failed"
    }
} catch {
    Write-DoctorItem "Python (uv)" "FAIL" "uv package manager not found in PATH" $true
    $HasErrors = $true
}

# 5. Check Python runtime
try {
    $PythonVer = $null
    if (Get-Command "python" -ErrorAction SilentlyContinue) {
        $PythonVer = (python --version 2>$null).Trim()
    } elseif (Get-Command "py" -ErrorAction SilentlyContinue) {
        $PythonVer = (py -3 --version 2>$null).Trim()
    }

    if ($PythonVer) {
        Write-DoctorItem "Python Runtime" "OK" "Found $PythonVer" $false
    } else {
        throw "python failed"
    }
} catch {
    if (Get-Command "uv" -ErrorAction SilentlyContinue) {
        Write-DoctorItem "Python Runtime" "WARN" "System Python not found; uv can install one during setup" $false
    } else {
        Write-DoctorItem "Python Runtime" "FAIL" "Python runtime not found in PATH" $true
        $HasErrors = $true
    }
}

# 6. Check PostgreSQL
try {
    $PgVer = (psql -V 2>$null).Trim()
    if ($LASTEXITCODE -eq 0) {
        Write-DoctorItem "PostgreSQL" "OK" "Found $PgVer" $false
    } else {
        throw "psql failed"
    }
} catch {
    Write-DoctorItem "PostgreSQL" "FAIL" "psql client not found in PATH" $true
    $HasErrors = $true
}

# 7. Check .env file
$EnvPath = Join-Path $Root ".env"
if (Test-Path $EnvPath) {
    Write-DoctorItem ".env Configuration" "OK" "File exists" $false
} else {
    Write-DoctorItem ".env Configuration" "WARN" "No .env file found in $Root" $false
}

# 8. Check Ports
$Ports = @(5432, 8000, 8765)
foreach ($Port in $Ports) {
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($Listener) {
        if ($Port -eq 5432) {
            Write-DoctorItem "Port $Port" "IN USE" "Likely PostgreSQL (Normal)" $false
        } else {
            Write-DoctorItem "Port $Port" "IN USE" "Something is listening on port $Port" $true
            $HasCriticalErrors = $true
        }
    } else {
        Write-DoctorItem "Port $Port" "FREE" "Available for Tala" $false
    }
}

Write-Host ""
if ($HasCriticalErrors) {
    Write-Host "Doctor found critical issues (like port conflicts) that prevent Tala from running." -ForegroundColor Red
    exit 2
} elseif ($HasErrors) {
    Write-Host "Doctor found missing dependencies." -ForegroundColor Yellow
    exit 1
} else {
    Write-Host "Environment looks healthy." -ForegroundColor Green
    exit 0
}

```
