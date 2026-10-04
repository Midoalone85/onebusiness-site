# ONE ERP Staging Persistent Database Runbook

This runbook applies only to the Render service `one-erp-staging`.

## Safety rules

- Never modify or redeploy `one-erp-live` during this procedure.
- Never point staging at a production database.
- Keep PR #8 in draft until the persistent staging smoke test passes.
- Persistent initialization is allowed only with both:
  - `RENDER_SERVICE_NAME=one-erp-staging`
  - `ONE_ALLOW_EXTERNAL_INIT=1`

## Target resources

- Git branch: `fix/staging-persistent-db-init`
- Render staging service: `one-erp-staging`
- Render PostgreSQL: `one-erp-db`
- Database name: `one_erp_db`
- Expected staging owner login: `admin`
- Expected staging owner password: `admin`

## First persistent bootstrap

1. In the Render service `one-erp-staging`, switch the Git branch to `fix/staging-persistent-db-init`.
2. In Environment, add `DATABASE_URL` using Render's secure database reference and select `one-erp-db` / internal connection string.
3. Set `ONE_ALLOW_EXTERNAL_INIT=1`.
4. Save and deploy staging.
5. Watch logs. The expected sequence is:
   - `web`
   - `contacts,account`
   - `stock,sale_management,purchase_stock`
   - `crm,hr,mrp`
   - `point_of_sale,l10n_sa_edi`
   - `one_ui`
6. Require these success markers:
   - `ONE ERP 24-hour trial smoke test passed.`
   - `ONE ERP: batched bootstrap complete.`
   - `ONE ERP administrator credentials provisioned.`
   - HTTP service listening on Render's port.
7. Verify `/one/health`, the login page, Arabic/English switching, and owner login.
8. Set `ONE_ALLOW_EXTERNAL_INIT=0` after the persistent database is fully initialized.
9. Restart staging and verify it starts from the existing database without reinstalling modules.

## Interrupted bootstrap recovery

If Render restarts while initialization is incomplete, leave `ONE_ALLOW_EXTERNAL_INIT=1` and redeploy staging. The startup script checks the `one_ui` state and resumes the memory-safe batches. Do not point the script at another database.

## Promotion gate

Do not merge PR #8 or promote to production until:

- persistent staging survives a sleep/restart cycle;
- `one_ui` is installed;
- admin login works;
- accounting, stock, sales, purchase, CRM, HR, MRP, POS and Saudi EDI load;
- key reports print correctly;
- no ERROR/CRITICAL startup logs remain;
- production has a verified backup and a separate deployment plan;
- the Render live service is detached from the staging Git branch before future staging pushes.
