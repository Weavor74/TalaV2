"""Installer DB bootstrap: psql notices must not abort; password prompted once."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "bootstrap-db.ps1"
SH = ROOT / "scripts" / "bootstrap-db.sh"
INSTALL_WIN = ROOT / "install-windows.ps1"


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def test_ps1_judges_psql_by_exit_code_not_stderr():
    src = _ps1()
    assert "NativeCommandError" in src
    assert '$ErrorActionPreference = "Continue"' in src
    assert "$LASTEXITCODE" in src
    assert "ON_ERROR_STOP=1" in src
    assert "client_min_messages" in src
    assert "--no-password" in src
    assert "Invoke-Psql" in src
    assert "CREATE EXTENSION IF NOT EXISTS vector" in src
    # Bare native psql (the 5.1 trap) is only inside Invoke-Psql.
    assert "& psql @all 2>&1" in src
    assert "psql -U postgres" not in src
    assert "2>$null" not in src


def test_ps1_prompts_postgres_password_once_and_clears_it():
    src = _ps1()
    assert src.count("Read-Host") >= 1
    assert src.count('Enter password for PostgreSQL') == 1
    assert "$env:PGPASSWORD" in src
    assert "Clear-PgPassword" in src
    assert "finally" in src
    assert "Remove-Item Env:PGPASSWORD" in src
    assert "--no-password" in src


def test_ps1_missing_pgvector_names_windows_install_for_detected_major():
    src = _ps1()
    assert "andreiramani/pgvector_pgsql_windows" in src
    assert "Write-PgvectorMissingHelp" in src
    assert "SHOW server_version" in src
    assert "vector.dll" in src
    assert "share\\extension" in src
    assert "Test-IsElevated" in src
    assert "Install-PgvectorFromGitHub" in src
    # Ask first; refuse a zip whose PG major does not match the server.
    assert "Download the matching pgvector build" in src
    assert "does not match server" in src
    assert "Read-Host" in src


def test_pgvector_windows_release_tag_matches_server_major():
    """Same rule the .ps1 uses: tag 0.8.6_17 matches PG 17, not 16 or 18."""
    pattern = re.compile(r"^[\d.]+_(\d+)(?:\.|$)")

    def major_from_tag(tag: str) -> int | None:
        match = pattern.match(tag)
        return int(match.group(1)) if match else None

    assert major_from_tag("0.8.6_17") == 17
    assert major_from_tag("0.8.6_17.6") == 17
    assert major_from_tag("0.8.6_16") == 16
    assert major_from_tag("0.8.6_18") == 18
    assert major_from_tag("0.8.6_17") != 16
    src = _ps1()
    assert "0.8.6_$Major" in src or "0.8.6_$Major" in src.replace("{", "")
    assert r"^[\d.]+_" in src


def test_sh_uses_on_error_stop_and_exit_code():
    src = _sh()
    assert "ON_ERROR_STOP=1" in src
    assert "--no-password" in src
    assert "client_min_messages" in src
    assert "run_psql" in src
    assert "PGPASSWORD" in src
    assert "trap clear_pgpassword EXIT" in src
    assert "CREATE EXTENSION IF NOT EXISTS vector" in src
    assert "postgresql-${PG_MAJOR" in src
    assert "exit 1" in src
    # Missing pgvector is a failed bootstrap, not a green finish.
    assert "Database bootstrap finished with warnings (pgvector missing)" in src
    complete = src.rindex("Database bootstrap complete.")
    missing = src.index("Database bootstrap finished with warnings (pgvector missing)")
    assert missing < complete


def test_sh_syntax():
    subprocess.run(["bash", "-n", str(SH)], check=True)


def test_install_windows_does_not_abort_the_rest_on_bootstrap_stderr():
    src = INSTALL_WIN.read_text(encoding="utf-8")
    assert "NativeCommandError" in src
    assert "try {" in src
    assert "bootstrap-db.ps1" in src
    assert "catch" in src


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser")
def test_ps1_parses_on_windows():
    cmd = (
        "$err = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{PS1}', [ref]$null, [ref]$err); "
        "if ($err) { $err | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
