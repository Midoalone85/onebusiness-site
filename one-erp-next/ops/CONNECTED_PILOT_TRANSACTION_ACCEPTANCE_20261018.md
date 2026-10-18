# ONE ERP NEXT | Connected pilot transaction acceptance

**18 October 2026 | DISPOSABLE CI ONLY | not Oracle staging, production or a customer pilot**

## What the new test actually executes

The PostgreSQL test `ApprovedSourcePostgresTests.test_real_invoice_to_private_readonly_wsgi_pilot_chain` establishes one isolated disposable company and performs:

1. Create a real purchase order for 10 units at 50 SAR each and a sale for 5 units at 100 SAR.
2. Confirm purchase and sale; receive 10 units, posting Inventory Dr 500 and GRNI Cr 500.
3. Ship 2 units, posting COGS Dr 100 and Inventory Cr 100; leave 8 units on hand.
4. Prepare an internal sales invoice from the valued *actual dispatched* sale, with a separately reviewed policy and a separate invoice approver.
5. Issue one internal invoice for 200 SAR through the audited finance source/replay boundary; confirm exactly one immutable invoice and journal.
6. Create two disposable, separate non-superuser PostgreSQL logins: operations SELECT and accounts-receivable SELECT, with limited ACLs. Connect via the existing tenant-pinned registries, not via superuser or any client-selected DSN.
7. Route HTTPS-shaped, authenticated (CI test identity, **not** a real IdP) requests through the genuine staging WSGI router and query live sales, purchase, inventory, returns and receivables data from the disposable DB.
8. Verify the receivable is 200 SAR, company/date/currency match, stock is 8 and fully available, and GL balances remain 400 Inventory, -500 GRNI, 100 COGS, 200 AR and -200 Sales.
9. Verify anonymous GET returns 401, cross-tenant GET returns 403, operation write request returns 405, and internal finance POST remains disabled (404).
10. Replay the identical approved document on a second DB connection and prove only one invoice and its journal exist, then clean up all temporary reader logins.

**Important:** The example uses out-of-scope internal tax for a financial integrity test, not a Saudi VAT/ZATCA-compliant issuance. No external bank confirmation, subscription or government reporting occurs.

## Automated controls

- The strict finance gate requires the new test by its exact name, on Python 3.11/3.13 and PostgreSQL 16/17.
- The test is intentionally blocked from touching real tenants by the existing CI-only, loopback-only `one_erp_test` fixture restrictions.
- The synthetic `StagingReadiness` all-true callback and fake test-only identity **must never be copied** into a deployed Oracle service.
- Passing this single flow is not proof of tax, stock-serial, concurrent-browser, external IdP, actual DBA ACL, disaster recovery or penetration-test compliance.

## Remaining live-pilot acceptance

- Freeze and independently review the exact source and migrations, including the current draft PR #53 and its sibling branch.
- Pin the target Oracle Jeddah tenancy, budget, host, DNS/TLS certificate, secrets and audited real OIDC/MFA identity. Confirm real instance and deployed SHA from an authorized operator.
- Provision disposable real staging company databases separately, with explicit DBA-reviewed operation and finance reader roles, and run the 3-layer tenant DB privilege preflight.
- Demonstrate a real encrypted backup and successful isolated restore.
- Run authenticated read-only HTTPS staging smoke including anonymous and wrong-tenant denial tests.
- Conduct a supervised user-accessed order/invoice test with a **non-real customer** and finance owner, then reconcile document, stock, journal, receivables and activity.
- Keep internal invoice mutations and payments disabled until independent finance, ZATCA and security sign-off.

**Release status:** source-only experimental candidate. NO automatic merge, OCI change, DNS, production payment, migration or invoice.