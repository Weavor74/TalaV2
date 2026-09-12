#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo " Tala Doctor - Unix Environment Check"
echo "========================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
HAS_ERRORS=0
CRITICAL_ERRORS=0

print_status() {
    local status="$1"
    local name="$2"
    local details="$3"
    local is_error="$4"
    
    if [ "$status" = "OK" ]; then
        printf "[\033[32m%s\033[0m] %s" "$status" "$name"
    elif [ "$is_error" = "1" ]; then
        printf "[\033[31m%s\033[0m] %s" "$status" "$name"
    else
        printf "[\033[33m%s\033[0m] %s" "$status" "$name"
    fi
    
    if [ -n "$details" ]; then
        printf " - \033[90m%s\033[0m\n" "$details"
    else
        echo ""
    fi
}

# 1. OS Info
OS="$(uname -s)"
ARCH="$(uname -m)"
print_status "OK" "OS Version" "$OS ($ARCH)" 0

# 2. Check Admin Rights
if [ "$(id -u)" = "0" ]; then
    print_status "OK" "Admin Rights" "Running as root" 0
else
    if command -v sudo &> /dev/null; then
        print_status "OK" "Admin Rights" "sudo is available" 0
    else
        print_status "WARN" "Admin Rights" "Not running as root and sudo not found" 0
    fi
fi

# 3. Check Node.js
if command -v node &> /dev/null; then
    NODE_VER="$(node -v)"
    print_status "OK" "Node.js" "Found version $NODE_VER" 0
else
    print_status "FAIL" "Node.js" "Node.js not found in PATH" 1
    HAS_ERRORS=1
fi

# 4. Check uv
if command -v uv &> /dev/null; then
    UV_VER="$(uv --version)"
    print_status "OK" "Python (uv)" "Found $UV_VER" 0
else
    print_status "FAIL" "Python (uv)" "uv package manager not found in PATH" 1
    HAS_ERRORS=1
fi

# 5. Check Python runtime
if command -v python3 &> /dev/null; then
    PY_VER="$(python3 --version)"
    print_status "OK" "Python Runtime" "Found $PY_VER" 0
elif command -v python &> /dev/null; then
    PY_VER="$(python --version)"
    print_status "OK" "Python Runtime" "Found $PY_VER" 0
elif command -v uv &> /dev/null; then
    print_status "WARN" "Python Runtime" "System Python not found; uv can install one during setup" 0
else
    print_status "FAIL" "Python Runtime" "Python runtime not found in PATH" 1
    HAS_ERRORS=1
fi

# 6. Check PostgreSQL
if command -v psql &> /dev/null; then
    PG_VER="$(psql -V | head -n1)"
    # The client binary being on PATH does not mean a server/cluster exists --
    # e.g. on Debian/Ubuntu, postgresql's postinst auto-creates the default
    # cluster using $LANG, and silently fails to do so if that locale isn't
    # generated. psql still ends up on PATH from the client package, so this
    # must actually probe for a live server rather than just checking the binary.
    if command -v pg_isready &> /dev/null && pg_isready -q 2>/dev/null; then
        print_status "OK" "PostgreSQL" "Found $PG_VER, server is accepting connections" 0
    else
        print_status "FAIL" "PostgreSQL" "Found $PG_VER, but no server is accepting connections. If a cluster was never created (check 'pg_lsclusters'), run: sudo pg_createcluster <ver> main --start -- --locale=C.UTF-8" 1
        HAS_ERRORS=1
    fi

    # pgvector is required for the embeddings table; bootstrap-db.sh enables the
    # extension per-database but can't install the server package itself.
    if command -v dpkg &> /dev/null; then
        PG_MAJOR="$(echo "$PG_VER" | grep -oE '[0-9]+' | head -1)"
        if dpkg -s "postgresql-${PG_MAJOR}-pgvector" &> /dev/null 2>&1; then
            print_status "OK" "pgvector" "postgresql-${PG_MAJOR}-pgvector installed" 0
        else
            print_status "FAIL" "pgvector" "postgresql-${PG_MAJOR}-pgvector not installed (run: sudo apt install postgresql-${PG_MAJOR}-pgvector)" 1
            HAS_ERRORS=1
        fi
    else
        print_status "WARN" "pgvector" "Cannot auto-verify on this system; ensure the pgvector extension package for PostgreSQL is installed" 0
    fi
else
    print_status "FAIL" "PostgreSQL" "psql client not found in PATH" 1
    HAS_ERRORS=1
fi

# 6b. Check xdotool (Linux desktop_observe/desktop_action backend)
# pygetwindow -- pyautogui's window backend -- has no Linux implementation
# (raises NotImplementedError on import), so window enumeration/focus on
# Linux is driven through xdotool instead.
if [ "$OS" = "Linux" ]; then
    if command -v xdotool &> /dev/null; then
        print_status "OK" "xdotool" "Found $(xdotool --version 2>&1 | head -n1)" 0
    else
        print_status "FAIL" "xdotool" "Not found in PATH; required for desktop_observe/desktop_action (run: sudo apt install xdotool)" 1
        HAS_ERRORS=1
    fi
fi

# 7. Check .env file
if [ -f "$ROOT_DIR/.env" ]; then
    print_status "OK" ".env Configuration" "File exists" 0
else
    print_status "WARN" ".env Configuration" "No .env file found in $ROOT_DIR" 0
fi

# 8. Check Ports (using lsof or netstat)
check_port() {
    local port=$1
    if command -v lsof &> /dev/null; then
        if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null ; then
            if [ "$port" = "5432" ]; then
                print_status "IN USE" "Port $port" "Likely PostgreSQL (Normal)" 0
            else
                print_status "IN USE" "Port $port" "Something is listening on port $port" 1
                CRITICAL_ERRORS=1
            fi
        else
            print_status "FREE" "Port $port" "Available for Tala" 0
        fi
    else
        print_status "WARN" "Port $port" "Cannot check port (lsof missing)" 0
    fi
}

check_port 5432
check_port 8000
check_port 8765

echo ""
if [ "$CRITICAL_ERRORS" = "1" ]; then
    echo -e "\033[31mDoctor found critical issues (like port conflicts) that prevent Tala from running.\033[0m"
    exit 2
elif [ "$HAS_ERRORS" = "1" ]; then
    echo -e "\033[33mDoctor found missing dependencies.\033[0m"
    exit 1
else
    echo -e "\033[32mEnvironment looks healthy.\033[0m"
    exit 0
fi
