param(
    [string]$DbUser = "tala",
    [string]$DbPass = "tala",
    [string]$DbName = "tala"
)

$ErrorActionPreference = "Stop"

Write-Host "Bootstrapping PostgreSQL Database..."

# Basic input validation to prevent naive SQL injection
if ($DbUser -match "[^a-zA-Z0-9_]" -or $DbName -match "[^a-zA-Z0-9_]") {
    Write-Host "Error: Database user and name must be alphanumeric." -ForegroundColor Red
    exit 1
}

# Escape single quotes in password
$SafeDbPass = $DbPass -replace "'", "''"

# Check if psql is available
try {
    $null = psql -V 2>$null
    if ($LASTEXITCODE -ne 0) { throw }
} catch {
    Write-Host "psql is not in PATH. Please ensure PostgreSQL is installed." -ForegroundColor Red
    exit 1
}

# Test Postgres Connection
$ConnTest = psql -U postgres -tAc "SELECT 1;" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Cannot connect as 'postgres' role. It may require a password." -ForegroundColor Yellow
    $PgPass = Read-Host "Enter password for PostgreSQL 'postgres' superuser (leave blank if none)" -AsSecureString
    if ($PgPass) {
        $env:PGPASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto([System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($PgPass))
    }
    # Test again
    psql -U postgres -tAc "SELECT 1;" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Authentication failed or server is unreachable. Aborting bootstrap." -ForegroundColor Red
        exit 1
    }
}

# Create Role
Write-Host "Ensuring role '$DbUser' exists..."
$RoleExists = (psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DbUser'" 2>$null)
if ($RoleExists -eq "1") {
    Write-Host "Role '$DbUser' already exists." -ForegroundColor Yellow
} else {
    psql -U postgres -c "CREATE ROLE ""$DbUser"" WITH LOGIN PASSWORD '$SafeDbPass';" | Out-Null
    Write-Host "Role '$DbUser' created." -ForegroundColor Green
}

# Create Database
Write-Host "Ensuring database '$DbName' exists..."
$DbExists = (psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'" 2>$null)
if ($DbExists -eq "1") {
    Write-Host "Database '$DbName' already exists." -ForegroundColor Yellow
} else {
    psql -U postgres -c "CREATE DATABASE ""$DbName"" OWNER ""$DbUser"";" | Out-Null
    Write-Host "Database '$DbName' created." -ForegroundColor Green
}

# The pytest suite isolates itself onto a sibling "<db>_test" database (see the
# database-isolation guard in conftest.py) so it never mutates the app's own
# data. Provision it here so a fresh checkout can run the tests with no manual
# step -- an absent test database was the largest single cause of red CI.
$TestDbName = "${DbName}_test"
Write-Host "Ensuring test database '$TestDbName' exists..."
$TestDbExists = (psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$TestDbName'" 2>$null)
if ($TestDbExists -eq "1") {
    Write-Host "Test database '$TestDbName' already exists." -ForegroundColor Yellow
} else {
    psql -U postgres -c "CREATE DATABASE ""$TestDbName"" OWNER ""$DbUser"";" | Out-Null
    Write-Host "Test database '$TestDbName' created." -ForegroundColor Green
}

# Clear password from env to be safe
$env:PGPASSWORD = ""

Write-Host "Database bootstrap complete." -ForegroundColor Green
exit 0
