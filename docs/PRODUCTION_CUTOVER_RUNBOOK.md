# ONE ERP Production Cutover Runbook

Target release: `20.0.1.31.0`

## Safety rules

- Do not change `one-erp-live` environment variables or branch before the current live state has either been backed up or explicitly accepted as disposable.
- Never store database passwords, Odoo master passwords, or full connection URIs in Git.
- Keep `ONE_ALLOW_EXTERNAL_INIT=0` during normal production operation.
- Use Supabase Session Pooler on port 5432 for Render production connectivity.

## Supabase production target

Project: `one-erp-prod-db`
Project ref: `ltcetiitotugourqpxva`
Database name: `postgres`
Region: `eu-central-1`

Required Render variables for production:

- `ONE_DB_HOST` = Supabase Session Pooler host
- `ONE_DB_PORT=5432`
- `ONE_DB_NAME=postgres`
- `ONE_DB_USER` = Session Pooler user shown by Supabase
- `ONE_DB_PASSWORD` = private database password
- `ONE_ADMIN_LOGIN=admin`
- `ONE_ADMIN_PASSWORD` = private production administrator password

## Existing-schema cutover

When a valid Odoo schema already exists in Supabase:

1. Keep `ONE_ALLOW_EXTERNAL_INIT=0`.
2. Attach the external database variables to `one-erp-live`.
3. Deploy the production release.
4. Confirm logs show an existing external schema and normal startup.
5. Verify login, landing page, core menus, accounting, sales, purchases, inventory, manufacturing, CRM, POS, localization, and reports.

## Empty-production bootstrap

Only use this path if the target Supabase database is verified empty and a fresh production bootstrap is explicitly approved.

Temporarily set all three guards:

- `ONE_ALLOW_EXTERNAL_INIT=1`
- `ONE_ALLOW_PRODUCTION_INIT=1`
- `ONE_PRODUCTION_INIT_CONFIRM=INITIALIZE_EMPTY_PRODUCTION_DB`

The startup script also requires a private `ONE_ADMIN_PASSWORD` before it will initialize an empty production database.

After the bootstrap completes and `one_ui` is installed:

1. Set `ONE_ALLOW_EXTERNAL_INIT=0`.
2. Set `ONE_ALLOW_PRODUCTION_INIT=0`.
3. Remove or blank `ONE_PRODUCTION_INIT_CONFIRM`.
4. Redeploy once and confirm startup follows the existing-schema path without module updates.

## Rollback

- Keep the prior production branch and release commit intact.
- If external startup fails, do not initialize another database automatically.
- Revert branch/environment settings only after confirming the rollback target is safe.
