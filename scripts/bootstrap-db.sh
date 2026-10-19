#!/usr/bin/env bash
set -euo pipefail

DB_USER="${1:-tala}"
DB_PASS="${2:-tala}"
DB_NAME="${3:-tala}"

echo "Bootstrapping PostgreSQL Database..."

# Basic input validation to prevent naive SQL injection
if [[ ! "$DB_USER" =~ ^[a-zA-Z0-9_]+$ ]] || [[ ! "$DB_NAME" =~ ^[a-zA-Z0-9_]+$ ]]; then
    echo -e "\033[31mError: Database user and name must be alphanumeric.\033[0m"
    exit 1
fi

# Escape single quotes in password
SAFE_DB_PASS="${DB_PASS//\'/''}"

if ! command -v psql &> /dev/null; then
    echo -e "\033[31mpsql is not in PATH. Please ensure PostgreSQL is installed.\033[0m"
    exit 1
fi

clear_pgpassword() {
    unset PGPASSWORD || true
}
trap clear_pgpassword EXIT

# Notices on stderr must not change the outcome; judge success by exit status.
# ON_ERROR_STOP=1 makes SQL errors (missing extension, auth, ...) a non-zero
# exit instead of psql continuing and returning 0. --no-password stops libpq
# prompting once per statement: we prompt at most once and reuse PGPASSWORD.
export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c client_min_messages=warning"

PSQL_BIN=(psql)
if [ "$(id -u)" = "0" ] && id -u postgres &>/dev/null; then
    PSQL_BIN=(sudo -u postgres psql)
fi

run_psql() {
    "${PSQL_BIN[@]}" -v ON_ERROR_STOP=1 --no-password "$@"
}

psql_ok() {
    run_psql "$@" >/dev/null 2>&1
}

psql_scalar() {
    run_psql -d postgres -tAc "$1" 2>/dev/null | tr -d '[:space:]' || true
}

# Run via sudo -u postgres if we are root, or -U postgres otherwise.
if [ "$(id -u)" = "0" ] && id -u postgres &>/dev/null; then
    :
else
    PSQL_BIN=(psql -U postgres)
    if ! psql_ok -d postgres -tAc "SELECT 1;"; then
        if [ -n "${TALA_BOOTSTRAP_NONINTERACTIVE:-}" ] && [ -z "${PGPASSWORD:-}" ]; then
            echo -e "\033[31mCannot connect as 'postgres' (psql --no-password failed). Set PGPASSWORD and re-run.\033[0m"
            exit 1
        fi
        echo -e "\033[33mCannot connect as 'postgres' role. It may require a password.\033[0m"
        read -s -p "Enter password for PostgreSQL 'postgres' superuser (leave blank if none): " PG_PASS
        echo ""
        if [ -n "$PG_PASS" ]; then
            export PGPASSWORD="$PG_PASS"
        fi
        if ! psql_ok -d postgres -tAc "SELECT 1;"; then
            echo -e "\033[31mAuthentication failed or server is unreachable. Aborting bootstrap.\033[0m"
            exit 1
        fi
    fi
fi

echo "Ensuring role '$DB_USER' exists..."
if [ "$(psql_scalar "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")" = "1" ]; then
    echo -e "\033[33mRole '$DB_USER' already exists.\033[0m"
else
    run_psql -d postgres -c "CREATE ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$SAFE_DB_PASS';" >/dev/null
    echo -e "\033[32mRole '$DB_USER' created.\033[0m"
fi

echo "Ensuring database '$DB_NAME' exists..."
if [ "$(psql_scalar "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'")" = "1" ]; then
    echo -e "\033[33mDatabase '$DB_NAME' already exists.\033[0m"
else
    run_psql -d postgres -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\";" >/dev/null
    echo -e "\033[32mDatabase '$DB_NAME' created.\033[0m"
fi

# The pytest suite isolates itself onto a sibling "<db>_test" database (see the
# database-isolation guard in conftest.py) so it never mutates the app's own
# data. Provision it here so a fresh checkout can run the tests with no manual
# step -- an absent test database was the largest single cause of red CI.
TEST_DB_NAME="${DB_NAME}_test"
echo "Ensuring test database '$TEST_DB_NAME' exists..."
if [ "$(psql_scalar "SELECT 1 FROM pg_database WHERE datname='$TEST_DB_NAME'")" = "1" ]; then
    echo -e "\033[33mTest database '$TEST_DB_NAME' already exists.\033[0m"
else
    run_psql -d postgres -c "CREATE DATABASE \"$TEST_DB_NAME\" OWNER \"$DB_USER\";" >/dev/null
    echo -e "\033[32mTest database '$TEST_DB_NAME' created.\033[0m"
fi

# pgvector, in both databases.
#
# Creating an extension needs superuser, which the app's own role is not, so
# the harness cannot do this for itself -- it can only report that it could
# not. Provisioning is this script's job, and it is not optional: without the
# extension there is no `vector` type, so the embeddings table cannot be
# created, so the VPS write gate finds no approved context and DENIES every
# write to a connection node. A fresh install could not connect to the VPS,
# and said only "Memory context validation failed".
VECTOR_OK=1
PG_MAJOR="$(run_psql -d postgres -tAc "SHOW server_version;" 2>/dev/null | grep -oE '^[0-9]+' | head -1 || true)"
for db in "$DB_NAME" "$TEST_DB_NAME"; do
    if run_psql -d "$db" -c "SET client_min_messages TO warning; CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1; then
        echo -e "\033[32mpgvector ready in '$db'.\033[0m"
    else
        VECTOR_OK=0
        echo -e "\033[31mCould not enable pgvector in '$db' (psql exit $?).\033[0m"
    fi
done

if [ "$VECTOR_OK" -ne 1 ]; then
    echo -e "\033[31mThe pgvector extension is not installed in this PostgreSQL ${PG_MAJOR:-?} server.\033[0m"
    echo -e "\033[33mTala needs the package that matches this server's major version.\033[0m"
    if command -v apt-get >/dev/null 2>&1; then
        echo -e "\033[33m  Debian/Ubuntu: sudo apt-get install postgresql-${PG_MAJOR:-$(pg_config --version 2>/dev/null | grep -oE '[0-9]+' | head -1)}-pgvector\033[0m"
    elif command -v brew >/dev/null 2>&1; then
        echo -e "\033[33m  macOS Homebrew: brew install pgvector\033[0m"
        echo -e "\033[33m  (or build from https://github.com/pgvector/pgvector for postgresql@${PG_MAJOR:-16})\033[0m"
    else
        echo -e "\033[33m  Install the pgvector package for PostgreSQL ${PG_MAJOR:-?} from your distro, then re-run this script.\033[0m"
    fi
    echo -e "\033[31mUntil then, peer writes to a connection node will be DENIED (embeddings table cannot be created).\033[0m"
    echo -e "\033[33mDatabase bootstrap finished with warnings (pgvector missing).\033[0m"
    exit 1
fi

echo -e "\033[32mDatabase bootstrap complete.\033[0m"
exit 0
