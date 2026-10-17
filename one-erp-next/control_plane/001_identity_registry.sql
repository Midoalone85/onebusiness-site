-- ONE ERP identity control-plane SCHEMA PROTOTYPE (PostgreSQL 14+).
-- Test in isolated CI database. Production should be provisioned in a
-- SEPARATE secured identity control-plane DB, not in customer ledger databases.
-- Only a trusted provisioning role may insert/update; tokens are never stored.
BEGIN;
CREATE SCHEMA one_auth;

CREATE TABLE one_auth.subject_links (
    issuer text NOT NULL CHECK (issuer ~ '^https://'),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    actor_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (issuer,subject)
);
CREATE INDEX one_subject_actor_lookup
    ON one_auth.subject_links(actor_id);

CREATE TABLE one_auth.tenant_entitlements (
    actor_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (actor_id,tenant_id)
);

-- SHA-256(UTF8(issuer + NUL + jti)): jti is NOT persisted in plaintext.
CREATE TABLE one_auth.revoked_jti (
    token_digest bytea PRIMARY KEY
        CHECK (octet_length(token_digest)=32),
    expires_at timestamptz NOT NULL
);
CREATE INDEX one_revoked_jti_expiry ON one_auth.revoked_jti(expires_at);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='one_erp_auth_lookup') THEN
        CREATE ROLE one_erp_auth_lookup
            NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;

GRANT USAGE ON SCHEMA one_auth TO one_erp_auth_lookup;
REVOKE ALL ON ALL TABLES IN SCHEMA one_auth FROM one_erp_auth_lookup;
GRANT SELECT ON one_auth.subject_links, one_auth.tenant_entitlements,
                one_auth.revoked_jti TO one_erp_auth_lookup;
COMMIT;

-- The control-plane lookup account receives ONLY one_erp_auth_lookup.
-- Do not expose these mappings to customers or a tenant's database login.
-- Access provisioning requires separate signed admin approval/audit, which
-- has NOT been implemented yet. In production provision a dedicated DB,
-- backups, active disablement, emergency revocation, and key rotation.
