# ONE ERP NEXT | Next acceptance milestones (17 October 2026)

## Verified baseline
GitHub Actions at commit b29957f7b53207bf8d9cc0ab704e4b0836cc60c2:
- Finance 39024562139: pass
- Integration 39024562252: pass
- Brand guard 39024562207: pass
- Jekyll 39024562180: pass
Integration branch 387 commits ahead of main, 0 behind at review.

## Acceptance order
1. Review 177 differing files, including executable permission boundaries, 51 numbered finance SQL migrations, and non-default deployment scripts. Require full migration/privilege review, not CI alone.
2. Run clean-room provisioning and migrations in disposable isolated PostgreSQL, verify rollbacks, RLS and adversarial cross-tenant access with runtime non-superuser roles. Existing CI fixture is destructive and must never be used against customer DB.
3. Validate ONE ERP end-user flows on deployed staging URLs for Arabic/English login, Sales, Purchase, Inventory, Returns, Finance, Reports and administrative config. Screenshot/UI tests alone are not end-to-end acceptance.
4. Validate Oracle security and infrastructure: private secrets, HTTPS cert, backup/restore, IdP, monitoring, health checks, ZATCA compliance integration. Do not claim these from code checks.
5. Review and merge integration branch into main only after explicit acceptance; publish production only after separate deployment approval.

## Delivery status
CI integration pass: YES.
Complete commercially usable SaaS: NOT VERIFIED.
Live operational staging and Oracle deployment: NOT VERIFIED.
This record does not authorize merge or deployment.
