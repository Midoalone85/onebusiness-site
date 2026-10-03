#!/usr/bin/env bash
set -euo pipefail
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

PG_INITDB=$(find /usr/lib/postgresql -type f -name initdb 2>/dev/null | head -n 1)
PG_BIN=$(dirname "$PG_INITDB")
PGDATA=/tmp/one-pgdata

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

COMMON="--db_host=/tmp --db_port=5432 --db_user=odoo --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons --proxy-mode --without-demo=all --workers=0 --max-cron-threads=1 --http-port=${PORT:-10000}"

odoo $COMMON -d one_erp_db -i base --stop-after-init
exec odoo $COMMON -d one_erp_db
