#!/usr/bin/env bash
set -e

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

# Run via sudo -u postgres if we are root, or normally if psql works directly
PSQL_CMD="psql"
if [ "$(id -u)" = "0" ] && id -u postgres &>/dev/null; then
    PSQL_CMD="sudo -u postgres psql"
else
    # Check if we can connect as postgres
    if ! $PSQL_CMD -U postgres -tAc "SELECT 1;" >/dev/null 2>&1; then
        echo -e "\033[33mCannot connect as 'postgres' role. It may require a password.\033[0m"
        read -s -p "Enter password for PostgreSQL 'postgres' superuser (leave blank if none): " PG_PASS
        echo ""
        if [ -n "$PG_PASS" ]; then
            export PGPASSWORD="$PG_PASS"
        fi
        if ! $PSQL_CMD -U postgres -tAc "SELECT 1;" >/dev/null 2>&1; then
            echo -e "\033[31mAuthentication failed or server is unreachable. Aborting bootstrap.\033[0m"
            export PGPASSWORD=""
            exit 1
        fi
        PSQL_CMD="psql -U postgres"
    else
        PSQL_CMD="psql -U postgres"
    fi
fi

echo "Ensuring role '$DB_USER' exists..."
if $PSQL_CMD -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo -e "\033[33mRole '$DB_USER' already exists.\033[0m"
else
    $PSQL_CMD -c "CREATE ROLE \"$DB_USER\" WITH LOGIN PASSWORD '$SAFE_DB_PASS';" >/dev/null
    echo -e "\033[32mRole '$DB_USER' created.\033[0m"
fi

echo "Ensuring database '$DB_NAME' exists..."
if $PSQL_CMD -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo -e "\033[33mDatabase '$DB_NAME' already exists.\033[0m"
else
    $PSQL_CMD -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\";" >/dev/null
    echo -e "\033[32mDatabase '$DB_NAME' created.\033[0m"
fi

# The pytest suite isolates itself onto a sibling "<db>_test" database (see the
# database-isolation guard in conftest.py) so it never mutates the app's own
# data. Provision it here so a fresh checkout can run the tests with no manual
# step -- an absent test database was the largest single cause of red CI.
TEST_DB_NAME="${DB_NAME}_test"
echo "Ensuring test database '$TEST_DB_NAME' exists..."
if $PSQL_CMD -tAc "SELECT 1 FROM pg_database WHERE datname='$TEST_DB_NAME'" | grep -q 1; then
    echo -e "\033[33mTest database '$TEST_DB_NAME' already exists.\033[0m"
else
    $PSQL_CMD -c "CREATE DATABASE \"$TEST_DB_NAME\" OWNER \"$DB_USER\";" >/dev/null
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
for db in "$DB_NAME" "$TEST_DB_NAME"; do
    if $PSQL_CMD -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1; then
        echo -e "\033[32mpgvector ready in '$db'.\033[0m"
    else
        echo -e "\033[31mCould not enable pgvector in '$db'. Install the server package"
        echo -e "  (Debian/Ubuntu: postgresql-\$(pg_config --version | grep -oE '[0-9]+' | head -1)-pgvector)"
        echo -e "  then re-run this script. Peer writes to a connection node will be denied until then.\033[0m"
    fi
done

export PGPASSWORD=""
echo -e "\033[32mDatabase bootstrap complete.\033[0m"
exit 0
