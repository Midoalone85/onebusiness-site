#!/usr/bin/env bash
set -euo pipefail
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

PG_INITDB=$(find /usr/lib/postgresql -type f -name initdb 2>/dev/null | head -n 1)
PG_BIN=$(dirname "$PG_INITDB")
PGDATA=/tmp/one-pgdata
ODOO_DATA=/tmp/one-odoo-data
DB_NAME="${ONE_DB_NAME:-one_erp_db}"

mkdir -p "$ODOO_DATA"

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
  if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
    rm -rf "$PGDATA"
    mkdir -p "$PGDATA"
    "$PG_BIN/initdb" -D "$PGDATA" -U odoo --auth=trust --locale=C.UTF-8 --no-instructions
  fi

  # Local fallback for staging/dev only. Data in /tmp is ephemeral on Render.
  "$PG_BIN/pg_ctl" -D "$PGDATA" -o "-h '' -p 5432 -k /tmp" -w start

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

# Build the stable ONE ERP core from upstream business modules plus our branded workspace.
# Legacy custom bundle modules stay available on disk but are deliberately not installed
# until each one has passed compatibility checks with the current engine.
odoo "${COMMON[@]}" -d "$DB_NAME" -i contacts,sale_management,purchase_stock,stock,account,mrp,crm,hr,one_ui --stop-after-init

# Ensure ONE ERP branding/assets/security settings are refreshed on every immutable deployment.
odoo "${COMMON[@]}" -d "$DB_NAME" -u one_ui --stop-after-init

# Generated web bundles can become stale between module updates. Rebuild them in the same lifecycle.
"$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c   "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%';"

exec odoo "${COMMON[@]}" -d "$DB_NAME"
