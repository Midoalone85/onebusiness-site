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

# Prefer a single Render-style DATABASE_URL when provided. This keeps database
# credentials out of the repository and makes the service compatible with
# Render Postgres using its internal connection string.
if [[ -n "${DATABASE_URL:-}" && -z "${ONE_DB_HOST:-}" ]]; then
  eval "$(python3 - <<'PY'
import os, shlex
from urllib.parse import urlparse, unquote
u = urlparse(os.environ["DATABASE_URL"])
vals = {
    "ONE_DB_HOST": u.hostname or "",
    "ONE_DB_PORT": str(u.port or 5432),
    "ONE_DB_USER": unquote(u.username or ""),
    "ONE_DB_PASSWORD": unquote(u.password or ""),
    "ONE_DB_NAME": (u.path or "/one_erp_db").lstrip("/") or "one_erp_db",
}
for k, v in vals.items():
    print(f"export {k}={shlex.quote(v)}")
PY
)"
  DB_NAME="${ONE_DB_NAME:-one_erp_db}"
  echo "ONE ERP: DATABASE_URL detected; using external PostgreSQL."
fi

ODOO_PID=""
BOOTSTRAP_HTTP_PID=""

mkdir -p "$ODOO_DATA"

# Bind Render's public port immediately on staging, before PostgreSQL init or
# Odoo module bootstrap. This prevents Render from repeatedly restarting a
# fresh instance when it discovers the port late during first boot.
if [[ "${RENDER_SERVICE_NAME:-}" == "one-erp-staging" && ( -z "${ONE_DB_HOST:-}" || "${ONE_ALLOW_EXTERNAL_INIT:-0}" == "1" ) ]]; then
  BOOTSTRAP_PAGE_DIR="/tmp/one-erp-bootstrap"
  mkdir -p "$BOOTSTRAP_PAGE_DIR"
  cat >"$BOOTSTRAP_PAGE_DIR/index.html" <<'HTML'
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="8">
  <title>ONE ERP | Preparing workspace</title>
  <style>
    *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:Arial,Tahoma,sans-serif;background:radial-gradient(circle at 80% 10%,#173b72 0,transparent 35%),linear-gradient(145deg,#071426,#0c2443 58%,#092f4a);color:#fff}.card{width:min(92vw,720px);padding:48px;border:1px solid rgba(255,255,255,.15);border-radius:28px;background:rgba(9,25,48,.72);box-shadow:0 30px 90px rgba(0,0,0,.35);backdrop-filter:blur(18px)}.brand{font-size:30px;font-weight:900;letter-spacing:-1px}.brand span{color:#55d7ff}.pill{display:inline-flex;margin:24px 0 12px;padding:8px 14px;border-radius:999px;background:rgba(85,215,255,.12);border:1px solid rgba(85,215,255,.25);color:#a8edff;font-size:13px;font-weight:700}.title{font-size:clamp(28px,5vw,48px);line-height:1.1;margin:12px 0}.sub{font-size:17px;line-height:1.8;color:#c9d7e8}.bar{height:8px;margin-top:28px;border-radius:999px;background:rgba(255,255,255,.09);overflow:hidden}.bar:after{content:"";display:block;width:42%;height:100%;border-radius:inherit;background:linear-gradient(90deg,#2d75ff,#5a5cff,#49d9ef);animation:move 1.4s ease-in-out infinite alternate}@keyframes move{to{transform:translateX(-135%)}}.en{direction:ltr;text-align:left;margin-top:18px;color:#91a8c1;font-size:14px}.foot{margin-top:28px;font-size:12px;color:#7188a1}@media(max-width:600px){.card{padding:30px 24px;border-radius:22px}.sub{font-size:15px}}
  </style>
</head>
<body>
  <main class="card">
    <div class="brand">ONE <span>ERP</span></div>
    <div class="pill">تهيئة مساحة العمل</div>
    <h1 class="title">نجهّز نظامك الآن</h1>
    <p class="sub">يتم تشغيل وحدات ONE ERP وتجهيز بيئة العمل بأمان. ستنتقل هذه الصفحة تلقائيًا إلى النظام فور اكتمال التشغيل.</p>
    <div class="bar"></div>
    <p class="en">Preparing your ONE ERP workspace. This page refreshes automatically when the application is ready.</p>
    <div class="foot">ONE Business • Secure staging startup</div>
  </main>
</body>
</html>
HTML
  cat >"/tmp/one-bootstrap-server.py" <<'PY'
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("BOOTSTRAP_PAGE_DIR", "/tmp/one-erp-bootstrap")
PORT = int(os.environ.get("PORT", "10000"))

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        return

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/one/health":
            body = json.dumps({
                "service": "ONE ERP",
                "status": "booting",
                "phase": "workspace_bootstrap",
                "ready": False,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
PY
  export BOOTSTRAP_PAGE_DIR
  python3 /tmp/one-bootstrap-server.py >/tmp/one-bootstrap-http.log 2>&1 &
  BOOTSTRAP_HTTP_PID=$!
  echo "ONE ERP: branded staging bootstrap page listening on port ${PORT:-10000}."

  # On a brand-new Render network configuration the platform may restart the
  # instance once after discovering the public port. Do not begin ephemeral
  # PostgreSQL/Odoo initialization until that discovery window has passed, or
  # all bootstrap work is thrown away with the first instance.
  if [[ "${ONE_RENDER_PORT_WARMUP:-1}" == "1" ]]; then
    echo "ONE ERP: waiting for Render port discovery before database bootstrap..."
    sleep "${ONE_RENDER_PORT_WARMUP_SECONDS:-65}"
  fi
fi

stop_local_pg() {
  if [[ "$LOCAL_PG" -eq 1 ]] && [[ -s "$PGDATA/PG_VERSION" ]]; then
    "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true
  fi
}

handle_term() {
  trap - TERM INT
  if [[ -n "$BOOTSTRAP_HTTP_PID" ]]; then
    kill -TERM "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
    wait "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
  fi
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

# Demo credentials are intentionally simple only on the named staging service,
# including when staging uses the dedicated persistent Render Postgres database.
# Any other ephemeral service keeps a generated strong password.
if [[ "${RENDER_SERVICE_NAME:-}" == "one-erp-staging" ]]; then
  export ONE_ADMIN_LOGIN="${ONE_ADMIN_LOGIN:-admin}"
  export ONE_ADMIN_PASSWORD="${ONE_ADMIN_PASSWORD:-admin}"
  export ONE_BOOTSTRAP_ADMIN_PASSWORD="$ONE_ADMIN_PASSWORD"
  echo "ONE ERP OWNER LOGIN: $ONE_ADMIN_LOGIN"
  echo "ONE ERP OWNER CREDENTIALS INITIALIZED."
elif [[ "$LOCAL_PG" -eq 1 ]]; then
  export ONE_ADMIN_LOGIN="${ONE_ADMIN_LOGIN:-admin}"
  export ONE_ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
print("ONE-" + secrets.token_urlsafe(18))
PY
)"
  export ONE_BOOTSTRAP_ADMIN_PASSWORD="$ONE_ADMIN_PASSWORD"
  echo "ONE ERP OWNER LOGIN: $ONE_ADMIN_LOGIN"
  echo "ONE ERP OWNER CREDENTIALS INITIALIZED."
fi

provision_admin() {
  export ONE_ADMIN_LOGIN="${ONE_ADMIN_LOGIN:-admin}"

  if [[ -n "${ONE_ADMIN_PASSWORD:-}" ]]; then
    export ONE_ADMIN_PASSWORD
  elif [[ -n "${ONE_BOOTSTRAP_ADMIN_PASSWORD:-}" ]]; then
    export ONE_ADMIN_PASSWORD="$ONE_BOOTSTRAP_ADMIN_PASSWORD"
  elif [[ "$LOCAL_PG" -eq 1 ]]; then
    echo "ONE ERP: refusing to provision an insecure default administrator password." >&2
    return 1
  else
    return 0
  fi

  echo "ONE ERP: waiting to provision the administrator account..."
  for _ in $(seq 1 600); do
    TABLES_READY=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc \
      "SELECT CASE
         WHEN to_regclass('public.ir_model_data') IS NOT NULL
          AND to_regclass('public.res_users') IS NOT NULL
         THEN '1' ELSE '0' END;" 2>/dev/null | tr -d '[:space:]' || true)

    if [[ "$TABLES_READY" != "1" ]]; then
      sleep 2
      continue
    fi

    READY=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc \
      "SELECT CASE WHEN
         EXISTS (
           SELECT 1
           FROM ir_model_data d
           JOIN res_users u ON u.id = d.res_id
           WHERE d.module='base' AND d.name='user_admin' AND d.model='res.users'
         )
         AND EXISTS (
           SELECT 1
           FROM ir_module_module
           WHERE name='one_ui' AND state='installed'
         )
       THEN '1' ELSE '0' END;" 2>/dev/null | tr -d '[:space:]' || true)

    if [[ "$READY" == "1" ]]; then
      # Wait until Odoo releases the registry-loading advisory lock. The module
      # can already be marked installed while registry finalization is still in
      # progress, and starting a second Odoo process during that window causes
      # a lock timeout.
      REGISTRY_READY=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc \
        "SELECT CASE
           WHEN pg_try_advisory_lock_shared(hashtext('registry_loading'))
           THEN CASE
             WHEN pg_advisory_unlock_shared(hashtext('registry_loading'))
             THEN '1' ELSE '0'
           END
           ELSE '0'
         END;" 2>/dev/null | tr -d '[:space:]' || true)

      if [[ "$REGISTRY_READY" != "1" ]]; then
        sleep 2
        continue
      fi

      cat >/tmp/one_set_admin.py <<'PY'
import os
user = env.ref("base.user_admin", raise_if_not_found=False)
if not user:
    raise RuntimeError("base.user_admin not found")
user.sudo().write({
    "login": os.environ.get("ONE_ADMIN_LOGIN", "admin"),
    "password": os.environ["ONE_ADMIN_PASSWORD"],
})
env.cr.commit()
print("ONE ERP administrator credentials provisioned.")
PY
      odoo shell "${ODOO_DB_ARGS[@]}" \
        "--data-dir=$ODOO_DATA" \
        "--addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons" \
        -d "$DB_NAME" < /tmp/one_set_admin.py
      rm -f /tmp/one_set_admin.py
      return 0
    fi
    sleep 2
  done

  echo "ONE ERP: administrator provisioning timed out." >&2
  return 1
}

COMMON=(
  "${ODOO_DB_ARGS[@]}"
  "--data-dir=$ODOO_DATA"
  "--addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons"
  "--proxy-mode"
  "--no-database-list"
  "--without-demo=True"
  "--workers=0"
  "--max-cron-threads=1"
)

HTTP_ARGS=(
  "--http-interface=0.0.0.0"
  "--http-port=${PORT:-10000}"
)

bootstrap_batch() {
  local modules="$1"
  echo "ONE ERP bootstrap: installing batch [$modules]..."
  odoo "${COMMON[@]}"     -d "$DB_NAME"     -i "$modules"     --stop-after-init     --no-http
  echo "ONE ERP bootstrap: batch [$modules] complete."
}

one_ui_code_version() {
  python3 - <<'PY'
import ast
from pathlib import Path

manifest = Path("/mnt/extra-addons/one_ui/__manifest__.py")
try:
    values = ast.literal_eval(manifest.read_text(encoding="utf-8"))
    print(values.get("version", ""))
except Exception:
    print("")
PY
}

configure_staging_attachment_storage() {
  # Render's filesystem is ephemeral. Keep new staging attachments and generated
  # web assets in PostgreSQL so a service restart cannot leave the database
  # pointing at vanished /tmp filestore files.
  if [[ "${RENDER_SERVICE_NAME:-}" != "one-erp-staging" || "$LOCAL_PG" -eq 1 ]]; then
    return 0
  fi

  local attachment_location
  attachment_location=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc \
    "SELECT COALESCE((SELECT value FROM ir_config_parameter WHERE key='ir_attachment.location' LIMIT 1), 'file');" \
    2>/dev/null | tr -d '[:space:]' || true)

  if [[ "$attachment_location" == "db" ]]; then
    echo "ONE ERP: staging attachment storage already persistent in PostgreSQL."
    return 0
  fi

  echo "ONE ERP: migrating staging attachment policy to PostgreSQL and resetting generated web assets."
  odoo shell "${ODOO_DB_ARGS[@]}" \
    --db-filter="^${DB_NAME//./\\.}$" \
    --no-http \
    --data-dir="$ODOO_DATA" \
    --addons-path="$ADDONS_PATH" \
    <<'PY'
icp = env["ir.config_parameter"].sudo()
icp.set_str("ir_attachment.location", "db")

Attachment = env["ir.attachment"].sudo().with_context(active_test=False)
generated_assets = Attachment.search([
    "|",
    ("url", "=like", "/web/assets/%"),
    ("name", "=like", "web.assets_%"),
])
count = len(generated_assets)
if generated_assets:
    generated_assets.unlink()

env.cr.commit()
print(f"ONE ERP: PostgreSQL attachment storage enabled; reset {count} generated web asset attachment(s).")
PY
}

upgrade_one_ui_if_needed() {
  # Persistent staging should absorb ONE UI releases without rebuilding the
  # whole ERP. Production is intentionally excluded from this automatic path.
  if [[ "${RENDER_SERVICE_NAME:-}" != "one-erp-staging" || "$LOCAL_PG" -eq 1 || "${ONE_AUTO_UPDATE_ONE_UI:-1}" != "1" ]]; then
    return 0
  fi

  local code_version db_version
  code_version="$(one_ui_code_version | tr -d '[:space:]')"
  db_version=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc     "SELECT COALESCE((SELECT latest_version FROM ir_module_module WHERE name='one_ui' LIMIT 1), '');"     2>/dev/null | tr -d '[:space:]' || true)

  if [[ -z "$code_version" ]]; then
    echo "ONE ERP: unable to read ONE UI code version; skipping automatic staging upgrade." >&2
    return 0
  fi

  if [[ "$db_version" == "$code_version" ]]; then
    echo "ONE ERP: ONE UI already current ($code_version); no module upgrade required."
    return 0
  fi

  echo "ONE ERP: ONE UI release changed ($db_version -> $code_version); upgrading staging module only."
  odoo "${COMMON[@]}" -d "$DB_NAME" -u one_ui --stop-after-init --no-http
  echo "ONE ERP: ONE UI staging upgrade complete ($code_version)."
}

# Install a fresh local staging database in small batches. Running all ONE ERP
# dependencies in one Odoo process can exceed the memory available on Render's
# free web service. A tiny temporary HTTP server keeps Render's port detector
# satisfied while the database is prepared offline in memory-bounded batches.
HAS_ODOO_SCHEMA=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc   "SELECT CASE WHEN to_regclass('public.ir_module_module') IS NULL THEN '0' ELSE '1' END;"   | tr -d '[:space:]')

run_staging_bootstrap() {
  echo "ONE ERP: starting memory-safe batched bootstrap."
  # Keep each install process deliberately small so memory is released between
  # groups. Odoo resolves each group's transitive dependencies automatically.
  bootstrap_batch "web"
  bootstrap_batch "contacts,account"
  bootstrap_batch "stock,sale_management,purchase_stock"
  bootstrap_batch "crm,hr,mrp"
  bootstrap_batch "point_of_sale,l10n_sa_edi"
  bootstrap_batch "one_ui"

  if [[ -n "$BOOTSTRAP_HTTP_PID" ]]; then
    kill "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
    wait "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
    BOOTSTRAP_HTTP_PID=""
  fi
  echo "ONE ERP: batched bootstrap complete."
}

if [[ "$HAS_ODOO_SCHEMA" == "1" ]]; then
  ONE_UI_STATE=$("$PG_BIN/psql" "${PSQL_ARGS[@]}" -d "$DB_NAME" -tAc       "SELECT COALESCE((SELECT state FROM ir_module_module WHERE name='one_ui' LIMIT 1), 'missing');"       2>/dev/null | tr -d '[:space:]' || true)

  if [[ "$LOCAL_PG" -eq 1 ]]; then
    if [[ "$ONE_UI_STATE" != "installed" ]]; then
      echo "ONE ERP: partial staging schema detected (one_ui=$ONE_UI_STATE); resuming bootstrap."
      run_staging_bootstrap
    else
      echo "ONE ERP: complete staging schema detected; normal startup."
    fi
  elif [[ "${RENDER_SERVICE_NAME:-}" == "one-erp-staging" && "${ONE_ALLOW_EXTERNAL_INIT:-0}" == "1" && "$ONE_UI_STATE" != "installed" ]]; then
    echo "ONE ERP: partial external staging schema detected (one_ui=$ONE_UI_STATE); safely resuming bootstrap."
    run_staging_bootstrap
  else
    echo "ONE ERP: existing external schema detected (one_ui=$ONE_UI_STATE)."
    if [[ "$ONE_UI_STATE" == "installed" ]]; then
      upgrade_one_ui_if_needed
      configure_staging_attachment_storage
    else
      echo "ONE ERP: normal startup without module updates or asset deletion."
    fi
  fi
elif [[ "$HAS_ODOO_SCHEMA" == "0" && "$LOCAL_PG" -eq 1 ]]; then
  echo "ONE ERP: fresh staging database detected."
  run_staging_bootstrap
elif [[ "$HAS_ODOO_SCHEMA" == "0" ]]; then
  if [[ "${RENDER_SERVICE_NAME:-}" == "one-erp-staging" && "${ONE_ALLOW_EXTERNAL_INIT:-0}" == "1" ]]; then
    echo "ONE ERP: verified empty external staging database; explicit initialization enabled."
    run_staging_bootstrap
  else
    echo "ONE ERP: external database has no application schema; refusing initialization without the staging-only ONE_ALLOW_EXTERNAL_INIT=1 safety flag." >&2
    exit 1
  fi
else
  echo "ONE ERP: database schema check returned an unexpected result; refusing initialization." >&2
  stop_local_pg
  exit 1
fi

if [[ -n "$BOOTSTRAP_HTTP_PID" ]]; then
  kill "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
  wait "$BOOTSTRAP_HTTP_PID" >/dev/null 2>&1 || true
  BOOTSTRAP_HTTP_PID=""
fi

RUN_ARGS=("${COMMON[@]}" "${HTTP_ARGS[@]}")

if [[ "$LOCAL_PG" -eq 1 ]]; then
  # Keep the shell as PID 1 so it can stop the local PostgreSQL child cleanly
  # when Render replaces or terminates this staging instance.
  odoo "${RUN_ARGS[@]}" -d "$DB_NAME" &
  ODOO_PID=$!
  provision_admin &

  set +e
  wait "$ODOO_PID"
  STATUS=$?
  set -e

  stop_local_pg
  exit "$STATUS"
fi

provision_admin &
exec odoo "${RUN_ARGS[@]}" -d "$DB_NAME"
