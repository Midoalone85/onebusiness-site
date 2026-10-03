#!/usr/bin/env bash
set -euo pipefail
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
: "${DB_HOST:?DB_HOST is required}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
DB_NAME="${DB_NAME:-one_erp_db}"
DB_PORT="${DB_PORT:-5432}"
HTTP_PORT="${PORT:-10000}"
MASTER="${ODOO_MASTER_PASSWORD:-admin}"
COMMON=(
  "--db_host=$DB_HOST"
  "--db_port=$DB_PORT"
  "--db_user=$DB_USER"
  "--db_password=$DB_PASSWORD"
  "--admin-passwd=$MASTER"
  "--addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons"
  "--proxy-mode"
  "--without-demo=all"
  "--workers=0"
  "--max-cron-threads=1"
  "--http-port=$HTTP_PORT"
)
echo "ONE ERP staging: initializing database if needed..."
odoo "${COMMON[@]}" -d "$DB_NAME" -i base --stop-after-init
echo "ONE ERP staging: starting web service..."
exec odoo "${COMMON[@]}" -d "$DB_NAME"
