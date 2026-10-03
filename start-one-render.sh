#!/usr/bin/env bash
set -euo pipefail
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

PG_INITDB=$(find /usr/lib/postgresql -type f -name initdb 2>/dev/null | head -n 1)
PG_BIN=$(dirname "$PG_INITDB")
PGDATA=/tmp/one-pgdata
ODOO_DATA=/tmp/one-odoo-data

mkdir -p "$ODOO_DATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  rm -rf "$PGDATA"
  mkdir -p "$PGDATA"
  "$PG_BIN/initdb" -D "$PGDATA" -U odoo --auth=trust --locale=C.UTF-8 --no-instructions
fi

# Keep PostgreSQL on a Unix socket only so Render sees ONE ERP as the only TCP web port.
"$PG_BIN/pg_ctl" -D "$PGDATA" -o "-h '' -p 5432 -k /tmp" -w start

if ! "$PG_BIN/psql" -h /tmp -p 5432 -U odoo -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='one_erp_db'" | grep -q 1; then
  "$PG_BIN/createdb" -h /tmp -p 5432 -U odoo one_erp_db
fi

COMMON="--db_host=/tmp --db_port=5432 --db_user=odoo --data-dir=$ODOO_DATA --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons --proxy-mode --no-database-list --without-demo=True --workers=0 --max-cron-threads=1 --http-interface=0.0.0.0 --http-port=${PORT:-10000}"

# Build the stable ONE ERP core from upstream business modules plus our branded workspace.
# Legacy custom bundle modules stay available on disk but are deliberately not installed
# until each one has passed compatibility checks with the current engine.
odoo $COMMON -d one_erp_db -i contacts,sale_management,purchase_stock,stock,account,mrp,crm,hr,one_ui --stop-after-init

# Ensure ONE ERP branding/assets/security settings are refreshed on every immutable deployment.
odoo $COMMON -d one_erp_db -u one_ui --stop-after-init

# Generated web bundles are ephemeral on Render. Rebuild them in the same lifecycle.
"$PG_BIN/psql" -h /tmp -p 5432 -U odoo -d one_erp_db -v ON_ERROR_STOP=1 -c \
  "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%';"

exec odoo $COMMON -d one_erp_db
