# ONE ERP Next: audited control-plane provisioning (draft / CI only)

**No change has been applied to the live Oracle deployment.** This is a
database-side prototype, not a web admin API or a ready-to-use sign-in screen.

## Architecture and authority

- Use an isolated control-plane PostgreSQL database, entirely separate from
  every customer ledger database. Install `001_identity_registry.sql` before
  `002_audited_provisioning.sql` **only after reviewed backups**.
- Only a trusted, authenticated, operator-authorized backend may hold a login
  role that inherits `one_erp_auth_provisioner`. No browser, public endpoint,
  tenant database account or unverified caller is ever given this role.
- The provisioning role can execute a handful of named SECURITY DEFINER
  procedures. It cannot directly read or update membership tables or audit
  records. PostgreSQL session settings and function parameters alone are NOT
  proof of the administrator's identity.
- The backend must derive `operator_id` from a freshly authenticated
  administrator session with MFA, evaluated permissions and a documented
  approval/change ticket. Never accept an operator UUID from user input.
- Every privileged transaction supplies a new, server-generated `request_id`.
  Repeated request IDs fail closed with no additional state changes.
- A logical tenant registry gates new membership grants and immediate tenant
  disablement, but **does not create or verify a physical PostgreSQL customer
  database**. Database provisioning, TLS, migrations and credentials require
  an independent, audited infrastructure workflow.
- `link_subject` never rebinds an existing issuer + subject to another actor,
  even when two requests race. Names, emails, passwords and authentication
  tokens are not audit log fields.
- `revoke_jti_digest` stores only the issuer-bound SHA-256 digest previously
  constructed by the trusted backend. The source JTI and access token must not
  appear in SQL logs, observability traces or audit records. Once disabled,
  identities and tenant grants are excluded from report authorization.
- A temporarily disabled subject or tenant can be reactivated by an operator.
  **Fresh-token issuance / minimum session epoch on reactivation is not
  implemented**. Do not allow production reactivation until old sessions are
  fully revoked or have expired and a fresh authentication is required.

## Prototype steps (not for production use)

1. Migrate an empty, disposable CI identity database with 001 then 002.
2. Provision a dedicated backend login using organization-approved secret
   management; give it ONLY the provisioner role and schema USAGE.
3. From a trusted admin backend, call the named functions with parameters and
   a unique operator-approved request identifier:
   `register_tenant`, `link_subject`, `set_entitlement`,
   `set_tenant_active`, `disable_subject`, or `revoke_jti_digest`.
4. Verify each completed operation has exactly one matching
   `one_auth.provisioning_audit` entry and any failure rolled back.
5. Verify the independent lookup role can read only current active memberships,
   and the administrative role cannot query or mutate the underlying tables.
6. Test cross-tenant access denial and immediate revocation in the private
   finance API using two physically separate, **disposable** databases.

## Release remains BLOCKED until

- Trusted real IdP, enforced admin MFA, approval workflow, session epoch and
  revocation delivery, key refresh, user recovery and subject lifecycle.
- Audited external provisioning of **one actual database per customer**,
  restricted per-tenant credentials and tested isolation.
- Centralized immutable audit export with operator IP/device/correlation,
  alerts, retention, reconciliation and independent security review.
- Verified encrypted backups and **actual** isolated restore drills; the
  current signed backup manifest only performs offline integrity preflight.
- HTTPS endpoints, UI accessibility, negative browser tests, penetration/load
  tests, ZATCA/tax compliance and financial posting sign-off.
- A documented release approval. CI green is not proof of deployed security.

No deployment scripts in this feature must target Oracle production.
