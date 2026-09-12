#!/usr/bin/env bash
set -e

echo "========================================================"
echo " Tala Cognitive Engine Room - Native Linux Installer"
echo "========================================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT_DIR="$SCRIPT_DIR"

SUDO=""
if [ "$(id -u)" != "0" ]; then
    if command -v sudo &> /dev/null; then
        SUDO="sudo"
    else
        echo -e "\033[31mThis installer needs root privileges or sudo to install packages.\033[0m"
        exit 1
    fi
fi

PKG_MANAGER=""
if command -v apt-get &> /dev/null && [ -f /etc/debian_version ]; then
    PKG_MANAGER="apt"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v xbps-install &> /dev/null; then
    PKG_MANAGER="xbps"
elif [ -f /etc/os-release ] && grep -qi '^ID=void' /etc/os-release; then
    echo -e "\033[31mVoid Linux detected, but xbps-install is not available in PATH.\033[0m"
    echo -e "\033[33mInstall/repair XBPS first, then rerun this installer.\033[0m"
    exit 1
else
    echo -e "\033[31mUnsupported Linux package manager.\033[0m"
    echo -e "\033[33mSupported package managers: apt-get (Debian/Ubuntu), dnf (AlmaLinux/RHEL/Fedora), xbps-install (Void Linux).\033[0m"
    exit 1
fi

# 1. Preflight
set +e
bash "$ROOT_DIR/scripts/doctor.sh"
DOCTOR_EXIT=$?
set -e
if [ $DOCTOR_EXIT -eq 2 ]; then
    echo -e "\033[31mDoctor found critical issues (e.g., port conflicts). Please resolve them before running the installer.\033[0m"
    exit 1
elif [ $DOCTOR_EXIT -eq 1 ]; then
    echo -e "\033[33mDoctor found missing dependencies. Attempting to install them...\033[0m"
fi

# 2. Dependencies via package manager
if [ "$PKG_MANAGER" = "apt" ]; then
    echo "[2/6] Checking Dependencies via APT..."
elif [ "$PKG_MANAGER" = "dnf" ]; then
    echo "[2/6] Checking Dependencies via DNF..."
else
    echo "[2/6] Checking Dependencies via XBPS..."
fi

install_if_missing() {
    local cmd="$1"
    local apt_pkg="$2"
    local xbps_pkg="$3"
    local dnf_pkg="$4"
    local friendly_name="$5"
    
    if command -v "$cmd" &> /dev/null; then
        echo -e "  - \033[32m$friendly_name is already installed.\033[0m"
    else
        if [ "$PKG_MANAGER" = "apt" ]; then
            echo -e "  - \033[33mInstalling $friendly_name via apt...\033[0m"
            $SUDO apt-get update -yqq
            $SUDO apt-get install -yqq $apt_pkg
        elif [ "$PKG_MANAGER" = "dnf" ]; then
            echo -e "  - \033[33mInstalling $friendly_name via dnf...\033[0m"
            $SUDO dnf install -y $dnf_pkg
        elif [ "$PKG_MANAGER" = "xbps" ]; then
            echo -e "  - \033[33mInstalling $friendly_name via xbps-install...\033[0m"
            $SUDO xbps-install -Sy $xbps_pkg
        fi
        echo -e "  - \033[32m$friendly_name installed!\033[0m"
    fi
}

install_if_missing "node" "nodejs npm" "nodejs npm" "nodejs npm" "Node.js"
install_if_missing "psql" "postgresql postgresql-contrib" "postgresql postgresql-client" "postgresql postgresql-server" "PostgreSQL"
# xdotool drives desktop_observe/desktop_action's window enumeration and focus
# on Linux, since pygetwindow (pyautogui's window backend) has no Linux
# implementation at all -- it raises NotImplementedError on import there.
install_if_missing "xdotool" "xdotool" "xdotool" "xdotool" "xdotool"
# pyttsx3 (offline TTS) is only a wrapper: on Linux it drives espeak-ng, and
# without the engine installed offline voice always fails here while working
# out of the box on Windows/macOS (which ship native speech engines).
install_if_missing "espeak-ng" "espeak-ng libespeak-ng1" "espeak-ng" "espeak-ng" "espeak-ng (offline TTS engine)"
# unzip is what the in-app auto-updater uses to apply an update after the app
# quits; it's absent on many minimal installs.
install_if_missing "unzip" "unzip" "unzip" "unzip" "unzip"

# The psql client being on PATH does not guarantee a cluster exists. On
# Debian/Ubuntu, postgresql's postinst auto-creates the default cluster using
# $LANG, and silently fails to do so if that locale isn't generated (common on
# minimal images/containers). Every check downstream that only asks "is psql
# installed?" then reports success even though there is no server to talk to,
# and bootstrap-db.sh fails with a confusing "authentication failed" instead.
if command -v pg_createcluster &> /dev/null && [ -z "$(pg_lsclusters -h 2>/dev/null)" ]; then
    echo -e "  - \033[33mNo PostgreSQL cluster found (likely a locale failure during install); creating one...\033[0m"
    PG_MAJOR="$(psql -V | grep -oE '[0-9]+' | head -1)"
    SAFE_LOCALE="C.UTF-8"
    locale -a 2>/dev/null | grep -qi '^en_US\.utf8$' && SAFE_LOCALE="en_US.UTF-8"
    $SUDO pg_createcluster "$PG_MAJOR" main --start -- --locale="$SAFE_LOCALE"
    echo -e "  - \033[32mCluster created with locale $SAFE_LOCALE.\033[0m"
fi

# pgvector: bootstrap-db.sh enables the extension per-database but can't
# install the server package itself. Without it there's no `vector` type, so
# the embeddings table can't be created and the VPS write gate denies every
# write to a connection node.
if [ "$PKG_MANAGER" = "apt" ] && command -v psql &> /dev/null; then
    PG_MAJOR="$(psql -V | grep -oE '[0-9]+' | head -1)"
    PGVECTOR_PKG="postgresql-${PG_MAJOR}-pgvector"
    if dpkg -s "$PGVECTOR_PKG" &> /dev/null 2>&1; then
        echo -e "  - \033[32mpgvector is already installed.\033[0m"
    else
        echo -e "  - \033[33mInstalling pgvector ($PGVECTOR_PKG)...\033[0m"
        $SUDO apt-get install -yqq "$PGVECTOR_PKG"
        echo -e "  - \033[32mpgvector installed!\033[0m"
    fi
else
    echo -e "  - \033[33mpgvector: install the matching server package for your distro if the extension step fails during bootstrap.\033[0m"
fi

if ! command -v uv &> /dev/null; then
    echo -e "  - \033[33mInstalling Python (uv) package manager...\033[0m"
    echo -e "  - \033[33mWarning: This will download and execute the official uv installer script from astral.sh.\033[0m"
    echo -e "  - \033[33mTo review the script first, visit: https://astral.sh/uv/install.sh\033[0m"
    read -p "Proceed with uv installation? (Y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]] && [[ -n $REPLY ]]; then
        echo -e "\033[31mAborting installation.\033[0m"
        exit 1
    fi
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo -e "  - \033[32mPython (uv) is already installed.\033[0m"
fi

if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo -e "  - \033[33mPython runtime not found. Installing Python 3.11 via uv...\033[0m"
    uv python install 3.11
    echo -e "  - \033[32mPython runtime is available through uv.\033[0m"
else
    echo -e "  - \033[32mPython runtime is already available.\033[0m"
fi

# Ensure Postgres service is running
echo "  - Ensuring PostgreSQL service is running..."
if command -v systemctl &> /dev/null; then
    $SUDO systemctl enable postgresql --now >/dev/null 2>&1 || true
elif command -v sv &> /dev/null && [ -d /etc/sv/postgresql ]; then
    $SUDO ln -s /etc/sv/postgresql /var/service/postgresql >/dev/null 2>&1 || true
    $SUDO sv up postgresql >/dev/null 2>&1 || true
else
    echo -e "  - \033[33mCould not auto-start PostgreSQL. Start it manually if database bootstrap fails.\033[0m"
fi

# 3. Bootstrap Database
echo "[3/6] Bootstrapping Database..."
sleep 2 # wait for systemctl
# Must run as root: bootstrap-db.sh's peer-auth branch (sudo -u postgres psql)
# only kicks in when it sees uid 0. As a plain user it falls through to
# `psql -U postgres` directly, which Debian's default pg_hba.conf peer-auth
# mapping rejects for any OS user that isn't literally "postgres" -- so it
# always ends up stuck on an interactive password prompt with no valid password.
$SUDO bash "$ROOT_DIR/scripts/bootstrap-db.sh" "tala" "tala" "tala"

# 4. Generate .env safely
echo "[4/6] Checking Configuration..."
ENV_PATH="$ROOT_DIR/.env"
if [ -f "$ENV_PATH" ]; then
    echo -e "  - \033[33m.env file already exists. Skipping generation to prevent silent overwrite.\033[0m"
else
    echo -e "  - \033[33m.env will be generated by first-run setup.\033[0m"
fi

# 5. First-run setup
echo "[5/6] Running First-Run Setup..."
cd "$ROOT_DIR"
uv run python scripts/first_run.py --self-test

echo "[6/6] Final Checks"
echo -e "========================================================"
echo -e " \033[32mInstallation Complete!\033[0m"
echo -e " \033[32mRun Tala by executing: ./scripts/start-tala.sh\033[0m"
echo -e "========================================================"
