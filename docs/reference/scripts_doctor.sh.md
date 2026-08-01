# File: `doctor.sh`

## Overview

A diagnostic script that checks for missing dependencies, port conflicts, and environment health.

## File Contents

```bash
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
    print_status "OK" "PostgreSQL" "Found $PG_VER" 0
else
    print_status "FAIL" "PostgreSQL" "psql client not found in PATH" 1
    HAS_ERRORS=1
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

```
