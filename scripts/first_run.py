"""
first_run.py - Auto-generated documentation base.
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
        _run([uv, "sync", "--extra", "voice"])
        print("  - Attempting to install embedded engine (transformers)...")
        _run([uv, "sync", "--extra", "voice", "--extra", "transformers"], check=False)
        return

    print("  - uv not found; falling back to stdlib venv + pip")
    _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    _run([str(_venv_pip()), "install", "--upgrade", "pip"])
    _run([str(_venv_pip()), "install", "-e", ".[voice]"])
    print("  - Attempting to install embedded engine (transformers)...")
    _run([str(_venv_pip()), "install", "-e", ".[transformers]"], check=False)


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
