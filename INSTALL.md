# Tala Cognitive Engine Room - Installation Guide

Tala uses a **Native Transparent Deployment** strategy. Instead of hiding bundled binaries in black-box folders, Tala installs directly into your host machine using official package managers (`winget`, `brew`, `apt`). 

This keeps your machine clean, standardized, and easily inspectable.

---

## 1. Installation

Choose your platform below. The installer will perform preflight health checks, install any missing dependencies (`Node.js`, `Python uv`, `PostgreSQL`), configure your `.env`, and bootstrap the database securely.

Dependency detection is explicit:

- **Node.js** is installed when `node`/`npm` are missing.
- **Python tooling** is installed as `uv`; if no system Python is available, the installer asks `uv` to install Python 3.11.
- **PostgreSQL** is installed when `psql` is missing, then the database role and database are bootstrapped.
- **Configuration** is owned by `scripts/first_run.py`; installers do not overwrite an existing `.env`.

### Windows
Open PowerShell and run:
```powershell
.\install-windows.ps1
```
*(Uses `winget` to provision dependencies)*

### macOS
Open your Terminal and run:
```bash
./install-macos.sh
```
*(Uses Homebrew to provision dependencies)*

### Linux
Open your Terminal and run:
```bash
./install-linux.sh
```
*(Uses `apt-get` on Debian/Ubuntu or `xbps-install` on Void Linux to provision dependencies. On Void, `xbps-install` must already be available.)*

---

## 2. Launching Tala

Once installed, simply run the start script to boot both the Python cognitive backend and the Electron UI:

**Windows:**
```powershell
.\scripts\start-tala.ps1
```

**macOS / Linux:**
```bash
./scripts/start-tala.sh
```

The launch scripts are also first-run aware. If `.env` or `.venv` is missing, they run `scripts/first_run.py` before booting the backend.

### Manual First-Run Bootstrap
Use this if you already installed Node.js, `uv`, and PostgreSQL yourself. If those tools are missing, use the platform installer instead so Tala can provision them:

**Windows:**
```powershell
uv run python scripts/first_run.py --self-test
```

**macOS / Linux:**
```bash
uv run python scripts/first_run.py --self-test
```

For a faster repair pass without npm audit, run:
```bash
uv run python scripts/first_run.py --self-test --no-audit
```

---

## 3. Packaging Desktop Builds

Tala packages with `electron-builder`. Runtime state is not baked into the app; packaged first-run setup creates `.env`, `.venv`, `vault/`, `data/`, and scratch space under Electron's per-user `userData` directory.

```bash
npm run pack          # unpacked app directory for smoke testing
npm run package       # installer/archive for the current OS
npm run package:win   # Windows NSIS + zip
npm run package:mac   # macOS dmg + zip
npm run package:linux # Linux AppImage + deb + tar.gz
```

Run `npm run self-test` before packaging and after installing a package on a clean machine.

---

## 4. Support & Diagnostics (Tala Doctor)

If you encounter issues during installation or launch, run the **Tala Doctor**. It will scan your OS, admin rights, dependencies, and check for port conflicts on `5432` (Postgres), `8000` (Backend), and `8765` (WebSockets).

**Windows:**
```powershell
.\scripts\doctor.ps1
```

**macOS / Linux:**
```bash
./scripts/doctor.sh
```

---

## 5. Rollback and Uninstall Notes

Because Tala uses a transparent deployment, it doesn't leave behind a monolithic black box. If you ever need to uninstall Tala, here is exactly what it touched:

### What Tala Created (Safe to Delete)
- **`.env`**: Your local environment configuration file.
- **`vault/`**: Where your profiles, memories, and node graphs are stored locally.
- **`.venv/`**: Tala's Python virtual environment.
- **`data/` and `scratch/`**: Local runtime data and temporary working files.
- **Database Schema**: Tala creates a Postgres role named `tala` and a database named `tala`.
  - *To remove:* Run `psql -U postgres -c "DROP DATABASE tala; DROP ROLE tala;"`

### What Tala Installed (Leaves your system standard)
The installers only add these tools if you did not already have them. If you want to remove them, use your system's package manager:
- **Node.js**: Remove via `winget uninstall OpenJS.NodeJS.LTS`, `brew uninstall node`, `apt-get remove nodejs`, or `xbps-remove nodejs npm`.
- **Python (uv)**: Remove via `winget uninstall astral-sh.uv`, `brew uninstall uv`, or `rm ~/.local/bin/uv`.
- **PostgreSQL**: Remove via `winget uninstall PostgreSQL.PostgreSQL`, `brew uninstall postgresql@16`, `apt-get remove postgresql`, or `xbps-remove postgresql postgresql-client`.
