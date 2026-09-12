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

# pgvector, in both databases.
#
# Creating an extension needs superuser, which the app's own role is not, so
# the harness cannot do this for itself -- it can only report that it could
# not. Provisioning is this script's job, and it is not optional: without the
# extension there is no `vector` type, so the embeddings table cannot be
# created, so the VPS write gate finds no approved context and DENIES every
# write to a connection node. A fresh install could not connect to the VPS,
# and said only "Memory context validation failed".
#
# Note: the winget/EDB PostgreSQL packages do NOT ship pgvector binaries, so
# CREATE EXTENSION can fail even as superuser. In that case point the user at
# the install instructions instead of silently continuing.
$VectorOk = $true
foreach ($Db in @($DbName, $TestDbName)) {
    psql -U postgres -d $Db -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "pgvector ready in '$Db'." -ForegroundColor Green
    } else {
        $VectorOk = $false
        Write-Host "Could not enable pgvector in '$Db'." -ForegroundColor Red
    }
}
if (-not $VectorOk) {
    Write-Host "The pgvector extension is not installed in this PostgreSQL server." -ForegroundColor Red
    Write-Host "The Windows PostgreSQL packages do not bundle it. To install:" -ForegroundColor Yellow
    Write-Host "  1. Download a pgvector release for your PostgreSQL major version:" -ForegroundColor Yellow
    Write-Host "     https://github.com/pgvector/pgvector/releases (or build per its README)" -ForegroundColor Yellow
    Write-Host "  2. Copy vector.dll into the PostgreSQL 'lib' directory and the" -ForegroundColor Yellow
    Write-Host "     vector.control / vector--*.sql files into 'share\extension'." -ForegroundColor Yellow
    Write-Host "  3. Re-run this script: scripts\bootstrap-db.ps1" -ForegroundColor Yellow
    Write-Host "Until then, peer writes to a connection node will be DENIED (embeddings table cannot be created)." -ForegroundColor Red
}

# Clear password from env to be safe
$env:PGPASSWORD = ""

if (-not $VectorOk) {
    Write-Host "Database bootstrap finished with warnings (pgvector missing)." -ForegroundColor Yellow
    exit 1
}
Write-Host "Database bootstrap complete." -ForegroundColor Green
exit 0
