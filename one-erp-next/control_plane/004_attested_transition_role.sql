-- ONE ERP Next / control-plane 004: isolate final attested transitions.
-- PostgreSQL 14+; disposable CI integration only until reviewed for release.
-- 001 -> 002 -> 003 -> 004. Does NOT verify a signature in PostgreSQL.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='one_erp_auth_attestor'
    ) THEN
        CREATE ROLE one_erp_auth_attestor
            NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
            NOREPLICATION;
    END IF;
END $$;

GRANT USAGE ON SCHEMA one_auth TO one_erp_auth_attestor;

-- Remove ability of the general admin/provisioning service to claim that
-- physical infrastructure, restore tests or HTTPS already passed.
REVOKE EXECUTE ON FUNCTION one_auth.advance_tenant_bootstrap(
    uuid,uuid,text,bytea,uuid
) FROM PUBLIC, one_erp_auth_provisioner;

-- Only the independently authenticated evidence-verification backend should
-- own a login that inherits this NOLOGIN attestor role. It is NOT the ordinary
-- tenant administration service or a customer/client role.
GRANT EXECUTE ON FUNCTION one_auth.advance_tenant_bootstrap(
    uuid,uuid,text,bytea,uuid
) TO one_erp_auth_attestor;

-- Defense in depth. Do not allow the isolated attestor to create, reset,
-- or fail onboarding jobs or mutate raw registry/audit state.
REVOKE EXECUTE ON FUNCTION one_auth.register_tenant(
    uuid,uuid,uuid
) FROM one_erp_auth_attestor;
REVOKE EXECUTE ON FUNCTION one_auth.begin_tenant_bootstrap(
    uuid,uuid,uuid,text,uuid
) FROM one_erp_auth_attestor;
REVOKE EXECUTE ON FUNCTION one_auth.fail_tenant_bootstrap(
    uuid,uuid,text,uuid
) FROM one_erp_auth_attestor;
REVOKE ALL ON ALL TABLES IN SCHEMA one_auth FROM one_erp_auth_attestor;

COMMIT;
-- These GRANT/REVOKE statements enforce two distinct SQL principals only.
-- The SQL function itself still accepts arbitrary 32-byte digests from its
-- attestor role. Production MUST independently bind that role to a privately
-- deployed evidence-verifying service, enforce MFA and mutual TLS, review
-- migration ownership/role inheritance, rotate credentials and export audits.
