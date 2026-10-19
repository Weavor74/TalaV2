param(
    [string]$DbUser = "tala",
    [string]$DbPass = "tala",
    [string]$DbName = "tala",
    [switch]$NonInteractive
)

# PowerShell 5.1 turns native stderr (including psql NOTICE) into
# NativeCommandError. With $ErrorActionPreference = Stop that aborts the
# installer even when psql exited 0 ("extension already exists, skipping").
# Judge success by $LASTEXITCODE only.
$ErrorActionPreference = "Stop"
$env:PGOPTIONS = "-c client_min_messages=warning"

Write-Host "Bootstrapping PostgreSQL Database..."

if ($DbUser -match "[^a-zA-Z0-9_]" -or $DbName -match "[^a-zA-Z0-9_]") {
    Write-Host "Error: Database user and name must be alphanumeric." -ForegroundColor Red
    exit 1
}

$SafeDbPass = $DbPass -replace "'", "''"

function Clear-PgPassword {
    if (Test-Path Env:PGPASSWORD) {
        $env:PGPASSWORD = $null
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Get-PsqlText {
    param($Output)
    if ($null -eq $Output) { return "" }
    $parts = foreach ($item in @($Output)) { "$item" }
    return ($parts -join "`n").Trim()
}

function Invoke-Psql {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $all = @("-v", "ON_ERROR_STOP=1", "--no-password") + $Arguments
        $output = & psql @all 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($null -eq $code) { $code = 1 }
    return @{ ExitCode = [int]$code; Output = $output }
}

function Get-PsqlScalar {
    param($Result)
    $lines = @(
        (Get-PsqlText $Result.Output) -split "(`r`n|`n)" |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and $_ -notmatch "^(NOTICE|WARNING|ERROR|psql):" }
    )
    if ($lines.Count -eq 0) { return "" }
    return $lines[-1]
}

function Test-IsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PostgresHome {
    $cmd = Get-Command psql -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $bin = Split-Path -Parent $cmd.Source
    $home = Split-Path -Parent $bin
    if (Test-Path (Join-Path $home "lib")) { return $home }
    return $null
}

function Get-ServerMajor {
    $result = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SHOW server_version;")
    if ($result.ExitCode -ne 0) { return $null }
    $text = Get-PsqlScalar $result
    if ($text -match "^(\d+)") { return $Matches[1] }
    return $null
}

function Write-PgvectorMissingHelp {
    param([string]$Major)
    $shown = if ($Major) { $Major } else { "<major>" }
    Write-Host "The pgvector extension is not installed in this PostgreSQL $shown server." -ForegroundColor Red
    Write-Host "The Windows PostgreSQL packages do not bundle it. Tala needs the build that" -ForegroundColor Yellow
    Write-Host "matches this server's major version (PostgreSQL $shown)." -ForegroundColor Yellow
    Write-Host "To install:" -ForegroundColor Yellow
    Write-Host "  1. Download the zip for PostgreSQL $shown from" -ForegroundColor Yellow
    Write-Host "     https://github.com/andreiramani/pgvector_pgsql_windows/releases" -ForegroundColor Cyan
    Write-Host "     (asset name looks like vector.v0.8.x-pg$shown.zip)." -ForegroundColor Yellow
    Write-Host "  2. Copy vector.dll into the PostgreSQL lib directory and" -ForegroundColor Yellow
    Write-Host "     vector.control / vector--*.sql into share\extension." -ForegroundColor Yellow
    Write-Host "  3. Re-run this script: scripts\bootstrap-db.ps1" -ForegroundColor Yellow
    Write-Host "Until then, peer writes to a connection node will be DENIED (embeddings table cannot be created)." -ForegroundColor Red
}

function Install-PgvectorFromGitHub {
    param([string]$Major, [string]$PgHome)
    if (-not $Major -or -not $PgHome) { return $false }
    $libDir = Join-Path $PgHome "lib"
    $extDir = Join-Path $PgHome "share\extension"
    if (-not (Test-Path $libDir) -or -not (Test-Path $extDir)) {
        Write-Host "Could not find PostgreSQL lib/share folders under $PgHome." -ForegroundColor Red
        return $false
    }
    $tagMajor = $null
    $url = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $headers = @{ "User-Agent" = "TalaV2-bootstrap-db" }
        $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/andreiramani/pgvector_pgsql_windows/releases" -Headers $headers
        foreach ($rel in @($releases)) {
            $tag = [string]$rel.tag_name
            if ($tag -match "^[\d.]+_$([regex]::Escape($Major))$" -or $tag -match "^[\d.]+_$([regex]::Escape($Major))\.") {
                $tagMajor = $Major
                $asset = @($rel.assets) | Where-Object { $_.name -match "pg$Major\.zip$" } | Select-Object -First 1
                if ($asset) { $url = [string]$asset.browser_download_url }
                break
            }
        }
    } catch {
        Write-Host "Could not list GitHub releases: $_" -ForegroundColor Yellow
    }
    if (-not $url) {
        $url = "https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.6_$Major/vector.v0.8.6-pg$Major.zip"
        $tagMajor = $Major
    }
    if ($tagMajor -ne $Major) {
        Write-Host "Refusing to install: release PostgreSQL major '$tagMajor' does not match server $Major." -ForegroundColor Red
        return $false
    }
    Write-Host "Downloading pgvector for PostgreSQL $Major from:" -ForegroundColor Yellow
    Write-Host "  $url" -ForegroundColor Cyan
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("tala-pgvector-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "vector.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $dll = Get-ChildItem -Path $tmp -Recurse -Filter "vector.dll" | Select-Object -First 1
        $control = Get-ChildItem -Path $tmp -Recurse -Filter "vector.control" | Select-Object -First 1
        $sqls = @(Get-ChildItem -Path $tmp -Recurse -Filter "vector--*.sql")
        if (-not $dll -or -not $control) {
            Write-Host "Zip did not contain vector.dll and vector.control." -ForegroundColor Red
            return $false
        }
        Copy-Item -Force $dll.FullName (Join-Path $libDir "vector.dll")
        Copy-Item -Force $control.FullName (Join-Path $extDir "vector.control")
        foreach ($sql in $sqls) {
            Copy-Item -Force $sql.FullName (Join-Path $extDir $sql.Name)
        }
        Write-Host "Installed pgvector files into $PgHome (PostgreSQL $Major)." -ForegroundColor Green
        return $true
    } catch {
        Write-Host "pgvector download/install failed: $_" -ForegroundColor Red
        return $false
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

try {
    try {
        $null = Get-Command psql -ErrorAction Stop
    } catch {
        Write-Host "psql is not in PATH. Please ensure PostgreSQL is installed." -ForegroundColor Red
        exit 1
    }
    $verCheck = Invoke-Psql -Arguments @("-V")
    if ($verCheck.ExitCode -ne 0) {
        Write-Host "psql is not in PATH. Please ensure PostgreSQL is installed." -ForegroundColor Red
        exit 1
    }

    $probe = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SELECT 1;")
    if ($probe.ExitCode -ne 0) {
        if ($NonInteractive -and -not $env:PGPASSWORD) {
            Write-Host "Cannot connect as 'postgres' (psql --no-password failed). Set PGPASSWORD and re-run." -ForegroundColor Red
            exit 1
        }
        Write-Host "Cannot connect as 'postgres' role. It may require a password." -ForegroundColor Yellow
        $PgPass = Read-Host "Enter password for PostgreSQL 'postgres' superuser (leave blank if none)" -AsSecureString
        if ($PgPass -and $PgPass.Length -gt 0) {
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($PgPass)
            try {
                $env:PGPASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            } finally {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
        }
        $probe = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SELECT 1;")
        if ($probe.ExitCode -ne 0) {
            Write-Host "Authentication failed or server is unreachable. Aborting bootstrap." -ForegroundColor Red
            Write-Host (Get-PsqlText $probe.Output) -ForegroundColor DarkGray
            exit 1
        }
    }

    Write-Host "Ensuring role '$DbUser' exists..."
    $role = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SELECT 1 FROM pg_roles WHERE rolname='$DbUser'")
    if ($role.ExitCode -ne 0) {
        Write-Host "Could not query pg_roles. Aborting bootstrap." -ForegroundColor Red
        Write-Host (Get-PsqlText $role.Output) -ForegroundColor DarkGray
        exit 1
    }
    if ((Get-PsqlScalar $role) -eq "1") {
        Write-Host "Role '$DbUser' already exists." -ForegroundColor Yellow
    } else {
        $created = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-c", "CREATE ROLE ""$DbUser"" WITH LOGIN PASSWORD '$SafeDbPass';")
        if ($created.ExitCode -ne 0) {
            Write-Host "Failed to create role '$DbUser'." -ForegroundColor Red
            Write-Host (Get-PsqlText $created.Output) -ForegroundColor DarkGray
            exit 1
        }
        Write-Host "Role '$DbUser' created." -ForegroundColor Green
    }

    Write-Host "Ensuring database '$DbName' exists..."
    $dbCheck = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SELECT 1 FROM pg_database WHERE datname='$DbName'")
    if ($dbCheck.ExitCode -ne 0) {
        Write-Host "Could not query pg_database. Aborting bootstrap." -ForegroundColor Red
        exit 1
    }
    if ((Get-PsqlScalar $dbCheck) -eq "1") {
        Write-Host "Database '$DbName' already exists." -ForegroundColor Yellow
    } else {
        $created = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-c", "CREATE DATABASE ""$DbName"" OWNER ""$DbUser"";")
        if ($created.ExitCode -ne 0) {
            Write-Host "Failed to create database '$DbName'." -ForegroundColor Red
            Write-Host (Get-PsqlText $created.Output) -ForegroundColor DarkGray
            exit 1
        }
        Write-Host "Database '$DbName' created." -ForegroundColor Green
    }

    # The pytest suite isolates itself onto a sibling "<db>_test" database (see the
    # database-isolation guard in conftest.py) so it never mutates the app's own
    # data. Provision it here so a fresh checkout can run the tests with no manual
    # step -- an absent test database was the largest single cause of red CI.
    $TestDbName = "${DbName}_test"
    Write-Host "Ensuring test database '$TestDbName' exists..."
    $testCheck = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-tAc", "SELECT 1 FROM pg_database WHERE datname='$TestDbName'")
    if ($testCheck.ExitCode -ne 0) {
        Write-Host "Could not query pg_database for '$TestDbName'. Aborting bootstrap." -ForegroundColor Red
        exit 1
    }
    if ((Get-PsqlScalar $testCheck) -eq "1") {
        Write-Host "Test database '$TestDbName' already exists." -ForegroundColor Yellow
    } else {
        $created = Invoke-Psql -Arguments @("-U", "postgres", "-d", "postgres", "-c", "CREATE DATABASE ""$TestDbName"" OWNER ""$DbUser"";")
        if ($created.ExitCode -ne 0) {
            Write-Host "Failed to create test database '$TestDbName'." -ForegroundColor Red
            Write-Host (Get-PsqlText $created.Output) -ForegroundColor DarkGray
            exit 1
        }
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
    # CREATE EXTENSION can fail even as superuser. CREATE EXTENSION IF NOT EXISTS
    # prints NOTICE to stderr when the extension is already present; that must
    # not abort this script.
    $extSql = "SET client_min_messages TO warning; CREATE EXTENSION IF NOT EXISTS vector;"
    $VectorOk = $true
    foreach ($Db in @($DbName, $TestDbName)) {
        $ext = Invoke-Psql -Arguments @("-U", "postgres", "-d", $Db, "-c", $extSql)
        if ($ext.ExitCode -eq 0) {
            Write-Host "pgvector ready in '$Db'." -ForegroundColor Green
        } else {
            $VectorOk = $false
            Write-Host "Could not enable pgvector in '$Db' (psql exit $($ext.ExitCode))." -ForegroundColor Red
            Write-Host (Get-PsqlText $ext.Output) -ForegroundColor DarkGray
        }
    }
    if (-not $VectorOk) {
        $major = Get-ServerMajor
        $pgHome = Get-PostgresHome
        Write-PgvectorMissingHelp -Major $major
        $canOffer = (Test-IsElevated) -and $major -and $pgHome -and -not $NonInteractive
        if ($canOffer) {
            $answer = Read-Host "Download the matching pgvector build for PostgreSQL $major from github.com/andreiramani/pgvector_pgsql_windows into '$pgHome'? [y/N]"
            if ($answer -match "^[Yy]") {
                if (Install-PgvectorFromGitHub -Major $major -PgHome $pgHome) {
                    $VectorOk = $true
                    foreach ($Db in @($DbName, $TestDbName)) {
                        $ext = Invoke-Psql -Arguments @("-U", "postgres", "-d", $Db, "-c", $extSql)
                        if ($ext.ExitCode -eq 0) {
                            Write-Host "pgvector ready in '$Db'." -ForegroundColor Green
                        } else {
                            $VectorOk = $false
                            Write-Host "Still could not enable pgvector in '$Db' (psql exit $($ext.ExitCode))." -ForegroundColor Red
                            Write-Host (Get-PsqlText $ext.Output) -ForegroundColor DarkGray
                        }
                    }
                }
            }
        } elseif (-not (Test-IsElevated)) {
            Write-Host "Re-run this script from an elevated PowerShell to be offered an automatic install into the PostgreSQL folder." -ForegroundColor Yellow
        }
    }

    if (-not $VectorOk) {
        Write-Host "Database bootstrap finished with warnings (pgvector missing)." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "Database bootstrap complete." -ForegroundColor Green
    exit 0
} finally {
    Clear-PgPassword
}
