-- DBA-reviewed provisioning template for report reader role (PG14+).
-- Apply manually to a DEDICATED tenant database after schema migrations 001-004.
-- A trusted backend owns the LOGIN role; no tenant/user receives SQL access.
-- NO PASSWORDS OR CREDENTIALS IN THIS FILE.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname='one_erp_finance_report_reader'
    ) THEN
        CREATE ROLE one_erp_finance_report_reader
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO one_erp_finance_report_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM one_erp_finance_report_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM one_erp_finance_report_reader;

-- Only the tables the dashboard currently needs:
GRANT SELECT ON TABLE
    one_tenants,
    one_memberships,
    one_accounts,
    one_journal_entries,
    one_journal_lines
TO one_erp_finance_report_reader;

-- Explicitly deny posting, issuing and settlement functions.
REVOKE ALL ON FUNCTION one_post_journal(uuid,uuid,uuid)
    FROM one_erp_finance_report_reader;
REVOKE ALL ON FUNCTION one_close_period(uuid,uuid,uuid)
    FROM one_erp_finance_report_reader;
REVOKE ALL ON FUNCTION one_issue_invoice(uuid,uuid,uuid)
    FROM one_erp_finance_report_reader;
REVOKE ALL ON FUNCTION one_apply_payment(uuid,uuid,uuid)
    FROM one_erp_finance_report_reader;
COMMIT;

-- IMPORTANT: PostgreSQL session variables (one.tenant_id and one.actor_id)
-- remain caller-settable. RLS cannot by itself protect a shared DB from an
-- actor that gets arbitrary SQL/credentials. Do not expose the reporting
-- LOGIN role's credentials or permit untrusted custom queries.
--
-- Provision a secret-managed LOGIN role for the trusted reporting backend;
-- GRANT one_erp_finance_report_reader TO that role only in the tenant DB.
-- Role must not inherit any write roles, schema ownership, or BYPASSRLS.
-- Then prohibit schema changes by the reporting login and test grants,
-- row-level policies, connection isolation, backups and revocation.
