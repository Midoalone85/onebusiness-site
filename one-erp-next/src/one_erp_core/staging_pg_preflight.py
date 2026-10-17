"""Read-only PostgreSQL catalog preflight for a staging operations reader.

Checks CURRENT LOGIN privileges on one pinned tenant database. This only
supplies evidence for the restricted_database_roles readiness criterion.
It cannot independently certify the IdP, tenant isolation, migrations,
backup restore, TLS or compliance. Call with a secret-managed reader-only
connection factory and a trusted expected DB/role, never values from HTTP.
"""
from __future__ import annotations

import re

_NAME = re.compile(r"[a-z][a-z0-9_]{2,62}\Z")
_OPERATIONS_READ = (
    "one_memberships", "one_commercial_orders",
    "one_stock_balances", "one_return_jobs",
)
_PROTECTED = (
    "one_invoice_source_requests", "one_invoice_source_approvals",
    "one_journal_entries", "one_journal_lines", "one_invoice_docs",
    "one_payment_docs",
)
_RLS_TABLES = (
    "one_invoice_source_requests", "one_invoice_source_approvals",
    "one_journal_entries", "one_invoice_docs",
)


def inspect_restricted_staging_reader(
    connection, *, expected_database: str, expected_role: str,
) -> bool:
    """Run only fixed, read-only catalog lookups; fail shut on ambiguity.

    Tests actual effective inherited/PUBLIC table privilege via
    has_table_privilege, not only GRANT role lists. Never alter schema,
    roles, session identity, tenant GUCs or user/financial data.
    """
    if (not isinstance(expected_database, str) or
        not isinstance(expected_role, str) or
        not _NAME.fullmatch(expected_database) or
        not _NAME.fullmatch(expected_role) or
        expected_role in ("postgres", "public")):
        raise ValueError("Pinned restricted staging role/database required")
    try:
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                cur.execute(
                    """SELECT current_database(),current_user,r.rolsuper,
                              r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,
                              has_database_privilege(
                                current_user,current_database(),'CREATE'),
                              has_schema_privilege(current_user,'public','CREATE')
                       FROM pg_catalog.pg_roles r WHERE r.rolname=current_user"""
                )
                actor=cur.fetchone()
                if (actor is None or len(actor)!=8 or
                    actor[0]!=expected_database or actor[1]!=expected_role or
                    any(flag is not False for flag in actor[2:])):
                    return False
                for table in _OPERATIONS_READ:
                    cur.execute(
                        """SELECT has_table_privilege(current_user,%s,'SELECT')""",
                        ("public."+table,),
                    )
                    row=cur.fetchone()
                    if row is None or row[0] is not True:
                        return False
                for table in _PROTECTED:
                    for action in ("INSERT","UPDATE","DELETE","TRUNCATE","TRIGGER"):
                        cur.execute(
                            "SELECT has_table_privilege(current_user,%s,%s)",
                            ("public."+table,action),
                        )
                        row=cur.fetchone()
                        if row is None or row[0] is not False:
                            return False
                for table in _RLS_TABLES:
                    cur.execute(
                        """SELECT c.relrowsecurity,c.relforcerowsecurity
                           FROM pg_catalog.pg_class c
                           WHERE c.oid=to_regclass(%s)""",
                        ("public."+table,),
                    )
                    row=cur.fetchone()
                    if row is None or row != (True, True):
                        return False
        return True
    except Exception:
        # Permissions or connection failure MUST NOT become readiness.
        return False
