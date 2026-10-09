#!/usr/bin/env bash
# ONE ERP / Oracle: recover the real administrator through Odoo ORM.
# Run ONLY on an authorized ONE ERP server, under the Odoo service account.
# Usage: ODOO_CMD=/path/to/odoo-bin ODOO_CONF=/etc/odoo/odoo.conf \
#        ONE_ERP_RECOVERY_EMAIL=you@example.com bash recover_one_erp_admin.sh DATABASE
set -Eeuo pipefail
umask 077

if [[ $# -ne 1 || -z "$1" ]]; then
  printf 'Usage: ODOO_CMD=/path/to/odoo-bin [ODOO_CONF=/path/to/odoo.conf] [ONE_ERP_RECOVERY_EMAIL=email] bash %s DATABASE\n' "$0" >&2
  exit 2
fi
if [[ "${GITHUB_ACTIONS:-}" == "true" || "${CI:-}" == "true" ]]; then
  echo 'Refusing to print account credentials in automated CI logs.' >&2
  exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'python3 not available.' >&2
  exit 4
fi

DB_NAME="$1"
ODOO_CMD="${ODOO_CMD:-odoo}"
if ! command -v "$ODOO_CMD" >/dev/null 2>&1 && [[ ! -x "$ODOO_CMD" ]]; then
  echo 'Odoo shell executable not found. Set ODOO_CMD to the installed executable.' >&2
  exit 5
fi

# Password is never passed as a CLI argument or committed into GitHub.
ONE_ERP_RECOVERY_PASSWORD="$(python3 -c 'import secrets; print("OneERP!" + secrets.token_urlsafe(24))')"
export ONE_ERP_RECOVERY_PASSWORD ONE_ERP_RECOVERY_EMAIL="${ONE_ERP_RECOVERY_EMAIL:-}"
ODOO_ARGS=(shell --no-http -d "$DB_NAME")
if [[ -n "${ODOO_CONF:-}" ]]; then
  ODOO_ARGS+=(-c "$ODOO_CONF")
fi

# No public recovery HTTP endpoint is installed; this script requires server access.
"$ODOO_CMD" "${ODOO_ARGS[@]}" <<'PY'
import os

password = os.environ["ONE_ERP_RECOVERY_PASSWORD"]
email = os.environ.get("ONE_ERP_RECOVERY_EMAIL", "").strip()
if len(password) < 20:
    raise RuntimeError("Password generation failed")

Users = env["res.users"].sudo().with_context(active_test=False)
admin = env.ref("base.user_admin", raise_if_not_found=False)
if not admin or admin._name != "res.users":
    raise RuntimeError("Odoo main administrator (base.user_admin) was not found. No changes made.")
admin = Users.browse(admin.id).exists()
if not admin:
    raise RuntimeError("Administrator record was missing. No changes made.")

if not email or "@" not in email:
    raise RuntimeError("ONE_ERP_RECOVERY_EMAIL is required to enable secure account recovery.")

desired_login = os.environ.get("ONE_ERP_RECOVERY_LOGIN", email).strip()
if not desired_login:
    raise RuntimeError("Administrator login must not be blank.")
if Users.search_count([("login", "=", desired_login), ("id", "!=", admin.id)]):
    raise RuntimeError("Requested administrator login is already in use. No changes made.")

values = {
    "password": password,
    "active": True,
    "login": desired_login,
    "email": email,
}
admin.write(values)
env["ir.config_parameter"].sudo().set_param(
    "one_erp.admin_credentials_bootstrapped", "1"
)
env.cr.commit()  # Persist ORM changes and prevent future re-bootstrap.
print("RECOVERY_STATUS=COMMITTED")
print("RECOVERY_USER_ID=%s" % admin.id)
print("RECOVERY_LOGIN=%s" % admin.login)
print("RECOVERY_EMAIL=%s" % (admin.email or "(unset)"))
PY

# Show the fresh password only after Odoo has committed successfully.
printf 'RECOVERY_PASSWORD=%s\n' "$ONE_ERP_RECOVERY_PASSWORD"
unset ONE_ERP_RECOVERY_PASSWORD
