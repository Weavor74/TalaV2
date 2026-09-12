"""
NAME
       first_run.py - prepare Tala for first run on any supported OS

SYNOPSIS
       uv run python scripts/first_run.py [OPTIONS]

DESCRIPTION
       Idempotent bootstrap script that prepares a Tala checkout for its first
       run (or repairs a broken one). Executes the following steps in order:

       1. Ensure local runtime directories (vault, nodes, workflows, data, scratch)
       2. Generate .env with safe local defaults (never overwrites an existing one)
       3. Install Node.js dependencies via npm ci / npm install
       4. Install Python dependencies via uv sync (core + test extras; voice/transformers
          are attempted but non-fatal because they carry heavy optional requirements)
       5. Initialize (or migrate) the PostgreSQL database schema

       Options:
         --skip-node     Skip step 3 (npm dependency install).
         --skip-python   Skip step 4 (Python virtualenv / dependency install).
         --skip-db       Skip step 5 (database schema initialization).
         --self-test     Run the Tala self-test suite after setup.
         --no-audit      Skip npm audit when --self-test is used.

       Environment overrides (all optional):
         TALA_INSTALL_ROOT    Repository root (default: parent of this script).
         TALA_USER_DATA_ROOT  User-data directory (default: same as install root).
         TALA_VENV_DIR        Path to the Python virtualenv (default: .venv in data root).
         TALA_ENV_PATH        Path to the .env file (default: .env in data root).

SEE ALSO
       scripts/doctor.ps1(1), scripts/doctor.sh(1), INSTALL.md
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(os.environ.get("TALA_INSTALL_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
USER_DATA_DIR = Path(os.environ.get("TALA_USER_DATA_ROOT", ROOT_DIR)).expanduser().resolve()
VENV_DIR = Path(os.environ.get("TALA_VENV_DIR", USER_DATA_DIR / ".venv")).expanduser().resolve()
ENV_PATH = Path(os.environ.get("TALA_ENV_PATH", USER_DATA_DIR / ".env")).expanduser().resolve()


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _venv_pip() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "pip.exe"
    return VENV_DIR / "bin" / "pip"


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(command)}")
    env = {
        **os.environ,
        "TALA_INSTALL_ROOT": str(ROOT_DIR),
        "TALA_USER_DATA_ROOT": str(USER_DATA_DIR),
        "TALA_VENV_DIR": str(VENV_DIR),
        "TALA_ENV_PATH": str(ENV_PATH),
    }
    return subprocess.run(command, cwd=ROOT_DIR, text=True, check=check, env=env)


def ensure_directories() -> None:
    for dirname in ("vault", "nodes", "workflows", "data", "scratch"):
        path = USER_DATA_DIR / dirname
        path.mkdir(parents=True, exist_ok=True)
        print(f"  - ensured {path}")


def ensure_env() -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ENV_PATH.exists():
        print(f"  - {ENV_PATH} exists; leaving it unchanged")
        return

    vault_path = (USER_DATA_DIR / "vault").resolve()
    ENV_PATH.write_text(
        "\n".join(
            [
                "TALA_DB_URL=postgresql://tala:tala@localhost:5432/tala",
                "INFERENCE_MODE=local",
                "VLLM_HOST=127.0.0.1",
                "VLLM_PORT=8000",
                "VLLM_MODEL=mistralai/Mistral-7B-Instruct-v0.3",
                "WS_HOST=127.0.0.1",
                "WS_PORT=8765",
                f"VAULT_PATH={vault_path}",
                "TALA_WALLET_ADDRESS=local-tala",
                "TALA_VPS_ACCEPT_ANY_TALA=0",
                "TOKEN_COST_PER_TOKEN=0.0000001",
                "INITIAL_AI_TOKEN_BALANCE=1000.0",
                "LOG_LEVEL=INFO",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"  - created {ENV_PATH} with local defaults")


def install_node_dependencies() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is not available in PATH")
    command = [npm, "ci"] if (ROOT_DIR / "package-lock.json").exists() else [npm, "install"]
    _run(command)


def install_python_dependencies() -> None:
    uv = shutil.which("uv")
    if uv:
        # Core deps + test (pytest must be present for the self-test suite).
        _run([uv, "sync", "--extra", "test"])
        # Voice deps require numba which is incompatible with Python >= 3.13.
        # Treat as non-fatal so the installer does not abort on newer Pythons.
        print("  - Attempting to install voice features (requires Python < 3.13)...")
        result = _run([uv, "sync", "--extra", "test", "--extra", "voice"], check=False)
        if result.returncode != 0:
            print(
                "  - Voice feature install failed (likely Python >= 3.13 / numba incompatibility).\n"
                "  - Core Tala will work fine. To enable voice, use Python < 3.13 and run:\n"
                "  -   uv pip install -e .[voice]"
            )
        print("  - Attempting to install embedded engine (transformers)...")
        _run([uv, "sync", "--extra", "test", "--extra", "transformers"], check=False)
        return

    print("  - uv not found; falling back to stdlib venv + pip")
    _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    _run([str(_venv_pip()), "install", "--upgrade", "pip"])
    _run([str(_venv_pip()), "install", "-e", ".[test]"])
    print("  - Attempting to install voice features (requires Python < 3.13)...")
    result = _run([str(_venv_pip()), "install", "-e", ".[voice]"], check=False)
    if result.returncode != 0:
        print(
            "  - Voice feature install failed (likely Python >= 3.13 / numba incompatibility).\n"
            "  - Core Tala will work fine. To enable voice, use Python < 3.13 and run:\n"
            "  -   pip install -e .[voice]"
        )
    print("  - Attempting to install embedded engine (transformers)...")
    _run([str(_venv_pip()), "install", "-e", ".[transformers]"], check=False)


def install_playwright_browsers() -> None:
    """Provision Playwright's browsers (non-fatal).

    The playwright *package* is installed with the core dependencies, but its
    browsers are a separate download that `playwright install` performs --
    and nothing ran it, so every browsing feature failed at first use with a
    "browser executable not found" error. Non-fatal: the download is large
    and needs network, and Tala works without browsing.
    """
    python = _venv_python()
    if not python.exists():
        print("  - venv Python missing; skipping Playwright browser install")
        return
    print("  - Installing Playwright browsers (Chromium)...")
    result = _run([str(python), "-m", "playwright", "install", "chromium"], check=False)
    if result.returncode != 0:
        print(
            "  - Playwright browser install failed (offline?). Browsing features\n"
            "  - will be unavailable until you run: python -m playwright install chromium"
        )


def migrate_database() -> bool:
    python = _venv_python()
    if not python.exists():
        raise RuntimeError(f"Virtualenv Python not found: {python}")

    script = (
        "import asyncio\n"
        "from python.harness import db\n"
        "async def main():\n"
        "    await db.init_pool()\n"
        "    await db.close_pool()\n"
        "asyncio.run(main())\n"
    )
    result = _run([str(python), "-c", script], check=False)
    if result.returncode != 0:
        print("  - database migration skipped/failed; verify PostgreSQL and TALA_DB_URL")
        return False
    print("  - database schema is initialized")
    return True


def run_self_test(include_audit: bool) -> bool:
    python = _venv_python()
    command = [str(python), "scripts/tala_self_test.py"]
    if not include_audit:
        command.extend(["--no-audit"])
    result = _run(command, check=False)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Tala for first run on any supported OS.")
    parser.add_argument("--skip-node", action="store_true", help="Skip npm dependency install.")
    parser.add_argument("--skip-python", action="store_true", help="Skip Python virtualenv/dependency install.")
    parser.add_argument("--skip-db", action="store_true", help="Skip database schema initialization.")
    parser.add_argument("--self-test", action="store_true", help="Run Tala self-test after setup.")
    parser.add_argument("--no-audit", action="store_true", help="Skip npm audit when --self-test is used.")
    args = parser.parse_args()

    print("========================================================")
    print(" Tala First-Run Setup")
    print("========================================================")

    print("[1/5] Ensuring local directories...")
    ensure_directories()

    print("[2/5] Ensuring .env...")
    ensure_env()

    if not args.skip_node:
        print("[3/5] Installing Node dependencies...")
        install_node_dependencies()
    else:
        print("[3/5] Skipping Node dependencies")

    if not args.skip_python:
        print("[4/5] Installing Python dependencies...")
        install_python_dependencies()
        install_playwright_browsers()
    else:
        print("[4/5] Skipping Python dependencies")

    db_ok = True
    if not args.skip_db:
        print("[5/5] Initializing database schema...")
        db_ok = migrate_database()
    else:
        print("[5/5] Skipping database initialization")

    if args.self_test:
        print("[post] Running Tala self-test...")
        if not run_self_test(include_audit=not args.no_audit):
            return 1

    print("First-run setup complete.")
    return 0 if db_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
