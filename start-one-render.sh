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

# Keep PostgreSQL on a Unix socket only so Render sees Odoo as the only TCP web port.
"$PG_BIN/pg_ctl" -D "$PGDATA" -o "-h '' -p 5432 -k /tmp" -w start

if ! "$PG_BIN/psql" -h /tmp -p 5432 -U odoo -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='one_erp_db'" | grep -q 1; then
  "$PG_BIN/createdb" -h /tmp -p 5432 -U odoo one_erp_db
fi

COMMON="--db_host=/tmp --db_port=5432 --db_user=odoo --data-dir=$ODOO_DATA --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons --proxy-mode --without-demo=True --workers=0 --max-cron-threads=1 --http-interface=0.0.0.0 --http-port=${PORT:-10000}"

# Ensure the core schema is present/updated.
odoo $COMMON -d one_erp_db -i base --stop-after-init

# Web bundles are cached as attachment records. On Render's ephemeral filesystem
# an older record can point to a file that no longer exists, which produces a
# blank backend after login. Purge generated bundles so Odoo rebuilds them into
# the same /tmp lifecycle as PostgreSQL.
"$PG_BIN/psql" -h /tmp -p 5432 -U odoo -d one_erp_db -v ON_ERROR_STOP=1 -c   "DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%';"

exec odoo $COMMON -d one_erp_db
