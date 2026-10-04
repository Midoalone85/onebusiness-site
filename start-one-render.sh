#!/usr/bin/env bash
set -euo pipefail
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

PG_INITDB=$(find /usr/lib/postgresql -type f -name initdb 2>/dev/null | head -n 1)
PG_BIN=$(dirname "$PG_INITDB")
PGDATA=/tmp/one-pgdata
ODOO_DATA=/tmp/one-odoo-data
DB_NAME="${ONE_DB_NAME:-one_erp_db}"
LOCAL_PG=0
ODOO_PID=""

mkdir -p "$ODOO_DATA"

stop_local_pg() {
  if [[ "$LOCAL_PG" -eq 1 ]] && [[ -s "$PGDATA/PG_VERSION" ]]; then
    "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true
  fi
}

handle_term() {
  trap - TERM INT
  if [[ -n "$ODOO_PID" ]]; then
    kill -TERM "$ODOO_PID" >/dev/null 2>&1 || true
    wait "$ODOO_PID" >/dev/null 2>&1 || true
  fi
  stop_local_pg
  exit 143
}

# Persistent database support is opt-in.
# Do not set ONE_DB_HOST/USER/PASSWORD until the target database has been
# verified and any existing production data has been backed up.
if [[ -n "${ONE_DB_HOST:-}" ]]; then
  DB_HOST="$ONE_DB_HOST"
  DB_PORT="${ONE_DB_PORT:-5432}"
  DB_USER="${ONE_DB_USER:-odoo}"
  DB_PASSWORD="${ONE_DB_PASSWORD:-}"

  ODOO_DB_ARGS=(
    "--db_host=$DB_HOST"
    "--db_port=$DB_PORT"
    "--db_user=$DB_USER"
  )
  PSQL_ARGS=(-h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER")

  if [[ -n "$DB_PASSWORD" ]]; then
    ODOO_DB_ARGS+=("--db_password=$DB_PASSWORD")
    export PGPASSWORD="$DB_PASSWORD"
  fi

  echo "ONE ERP: persistent PostgreSQL mode enabled for database '$DB_NAME'."
else
  LOCAL_PG=1

  if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
    rm -rf "$PGDATA"
    mkdir -p "$PGDATA"
    "$PG_BIN/initdb" -D "$PGDATA" -U odoo --auth=trust --locale=C.UTF-8 --no-instructions
  fi

  # Local fallback for staging/dev only. Data in /tmp is ephemeral on Render.
  "$PG_BIN/pg_ctl" -D "$PGDATA" -o "-h '' -p 5432 -k /tmp" -w start
  trap handle_term TERM INT

  if ! "$PG_BIN/psql" -h /tmp -p 5432 -U odoo -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    "$PG_BIN/createdb" -h /tmp -p 5432 -U odoo "$DB_NAME"
  fi

  ODOO_DB_ARGS=(
    "--db_host=/tmp"
    "--db_port=5432"
    "--db_user=odoo"
  )
  PSQL_ARGS=(-h /tmp -p 5432 -U odoo)

  echo "ONE ERP: ephemeral PostgreSQL fallback active. Do not use for permanent production data."
fi

COMMON=(
  "${ODOO_DB_ARGS[@]}"
  "--data-dir=$ODOO_DATA"
  "--addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons"
  "--proxy-mode"
  "--no-database-list"
  "--without-demo=True"
  "--workers=0"
  "--max-cron-threads=1"
  "--http-interface=0.0.0.0"
  "--http-port=${PORT:-10000}"
)

# Install on a fresh database; start existing databases without migrations.
# IMPORTANT FOR RENDER:
# Do not use --stop-after-init here. Odoo opens the HTTP port before the module
# install/update finishes, which lets Render detect the service immediately
# instead of timing out while waiting for the database bootstrap to complete.
HAS_ODOO_SCHEMA=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc \
  "SELECT CASE WHEN to_regclass('public.ir_module_module') IS NULL THEN '0' ELSE '1' END;" \
  | tr -d '[:space:]')

INIT_ARGS=()
if [[ "$HAS_ODOO_SCHEMA" == "1" ]]; then
  echo "ONE ERP: existing schema detected; normal startup without module updates or asset deletion."
  # Run module upgrades separately during maintenance, only after a verified
  # database AND filestore backup. A restart must not perform that upgrade.
elif [[ "$HAS_ODOO_SCHEMA" == "0" && "$LOCAL_PG" -eq 1 ]]; then
  echo "ONE ERP: fresh database detected; installing ONE ERP workspace while HTTP is online."
  INIT_ARGS=(-i one_ui)
elif [[ "$HAS_ODOO_SCHEMA" == "0" ]]; then
  echo "ONE ERP: external database has no application schema; initialize it explicitly after verifying the target and backups." >&2
  exit 1
else
  echo "ONE ERP: database schema check returned an unexpected result; refusing initialization." >&2
  stop_local_pg
  exit 1
fi

if [[ "$LOCAL_PG" -eq 1 ]]; then
  # Keep the shell as PID 1 so it can stop the local PostgreSQL child cleanly
  # when Render replaces or terminates this staging instance.
  odoo "${COMMON[@]}" -d "$DB_NAME" "${INIT_ARGS[@]}" &
  ODOO_PID=$!

  set +e
  wait "$ODOO_PID"
  STATUS=$?
  set -e

  stop_local_pg
  exit "$STATUS"
fi

exec odoo "${COMMON[@]}" -d "$DB_NAME" "${INIT_ARGS[@]}"
