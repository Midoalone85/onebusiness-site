"""Disposable real-PostgreSQL adverse role-grant tests (not Oracle/customer data)."""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.runtime_reader_privilege_audit import (
    RestrictedReaderPolicy, inspect_restricted_reader,
)

DSN = os.environ.get("ONE_TEST_PG_DSN", "")
CI_ONLY = os.environ.get("CI") == "true" and "dbname=one_erp_test" in DSN


@unittest.skipUnless(CI_ONLY, "Dedicated disposable PostgreSQL CI required")
class RuntimeReadOnlyRoleAuditPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.pg, cls.sql = psycopg, sql
        cls.args = psycopg.conninfo.conninfo_to_dict(DSN)
        if cls.args.get("dbname") != "one_erp_test":
            raise RuntimeError("Never run runtime role audit fixture outside one_erp_test")
        cls.admin = psycopg.connect(DSN, autocommit=True)
        suffix = uuid4().hex[:12]
        cls.db_name = "one_ci_acl_" + suffix
        cls.reader = "one_ci_acl_reader_" + suffix
        cls.group = "one_ci_acl_group_" + suffix
        cls.extra = "one_ci_acl_extra_" + suffix
        cls.created = []
        try:
            for role,login in ((cls.reader,True),(cls.group,False),(cls.extra,False)):
                cls.admin.execute(
                    sql.SQL("CREATE ROLE {} {} NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                                sql.Identifier(role),
                                sql.SQL("LOGIN") if login else sql.SQL("NOLOGIN")),
                )
                cls.created.append(role)
            cls.admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER postgres TEMPLATE template0").format(
                    sql.Identifier(cls.db_name)),
            )
            cls.admin.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(cls.db_name)),
            )
            cls.admin.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(cls.db_name),sql.Identifier(cls.reader)),
            )
            with cls.connect() as conn:
                conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                conn.execute(
                    """CREATE TABLE public.one_demo_rows
                       (id integer PRIMARY KEY, tenant_id uuid NOT NULL)""")
                conn.execute("ALTER TABLE public.one_demo_rows ENABLE ROW LEVEL SECURITY")
                conn.execute("ALTER TABLE public.one_demo_rows FORCE ROW LEVEL SECURITY")
                conn.execute("""CREATE POLICY audited_scope ON public.one_demo_rows
                                USING (tenant_id::text=current_setting('one.tenant_id',true))""")
                conn.execute(
                    """CREATE TABLE public.one_private_rows
                       (id integer PRIMARY KEY, tenant_id uuid NOT NULL)""")
                conn.execute("ALTER TABLE public.one_private_rows ENABLE ROW LEVEL SECURITY")
                conn.execute("ALTER TABLE public.one_private_rows FORCE ROW LEVEL SECURITY")
                conn.execute("CREATE TABLE public.one_global_catalog (id integer PRIMARY KEY)")
                conn.execute(
                    """CREATE FUNCTION public.one_read_allowed() RETURNS integer
                       LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$""")
                conn.execute(
                    """CREATE FUNCTION public.one_issue_unapproved() RETURNS integer
                       LANGUAGE sql AS $$ SELECT 999 $$""")
                conn.execute("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
                conn.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                        sql.Identifier(cls.reader)))
                conn.execute(
                    sql.SQL("GRANT SELECT ON public.one_demo_rows TO {}").format(
                        sql.Identifier(cls.reader)))
                conn.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION public.one_read_allowed() TO {}")
                    .format(sql.Identifier(cls.reader)))
        except BaseException:
            cls.cleanup()
            raise

    @classmethod
    def connect(cls):
        return cls.pg.connect(**dict(cls.args,dbname=cls.db_name),autocommit=True)

    @classmethod
    def cleanup(cls):
        if not hasattr(cls,"admin") or cls.admin.closed:
            return
        try:
            cls.admin.execute(
                cls.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    cls.sql.Identifier(cls.db_name)))
            for role in reversed(getattr(cls,"created",[])):
                cls.admin.execute(
                    cls.sql.SQL("DROP ROLE IF EXISTS {}").format(
                        cls.sql.Identifier(role)))
        finally:
            cls.admin.close()

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()

    def setUp(self):
        self.policy = RestrictedReaderPolicy(
            login_role=self.reader,
            required_tables=frozenset({"one_demo_rows"}),
            allowed_tables=frozenset({"one_demo_rows"}),
            allowed_function_signatures=frozenset({"public.one_read_allowed()"}),
        )

    def inspect(self, policy=None):
        with self.connect() as conn:
            return inspect_restricted_reader(
                conn,expected_database=self.db_name,
                policy=policy if policy is not None else self.policy)

    def statement(self,query):
        with self.connect() as conn:
            conn.execute(query)

    def test_restricted_effective_grants_pass_without_deploying_anything(self):
        report=self.inspect()
        self.assertEqual(report.login_role,self.reader)
        self.assertEqual(report.approved_table_reads,("one_demo_rows",))
        self.assertEqual(report.approved_function_executes,("public.one_read_allowed()",))
        self.assertFalse(report.reachable_group_roles)

    def test_public_execute_on_unapproved_function_is_release_blocking(self):
        self.statement("GRANT EXECUTE ON FUNCTION public.one_issue_unapproved() TO PUBLIC")
        try:
            with self.assertRaisesRegex(AccountingError,"unapproved function"):
                self.inspect()
        finally:
            self.statement("REVOKE EXECUTE ON FUNCTION public.one_issue_unapproved() FROM PUBLIC")

    def test_public_select_on_confidential_relation_is_release_blocking(self):
        self.statement("GRANT SELECT ON public.one_private_rows TO PUBLIC")
        try:
            with self.assertRaisesRegex(AccountingError,"Unapproved SELECT"):
                self.inspect()
        finally:
            self.statement("REVOKE SELECT ON public.one_private_rows FROM PUBLIC")

    def test_direct_or_inherited_mutation_is_release_blocking(self):
        self.statement(
            self.sql.SQL("GRANT UPDATE ON public.one_demo_rows TO {}")
            .format(self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError,"modify a relation"):
                self.inspect()
        finally:
            self.statement(
                self.sql.SQL("REVOKE UPDATE ON public.one_demo_rows FROM {}")
                .format(self.sql.Identifier(self.reader)))

    def test_missing_forced_rls_on_reader_table_is_release_blocking(self):
        self.statement("ALTER TABLE public.one_demo_rows NO FORCE ROW LEVEL SECURITY")
        try:
            with self.assertRaisesRegex(AccountingError,"lacks forced RLS"):
                self.inspect()
        finally:
            self.statement("ALTER TABLE public.one_demo_rows FORCE ROW LEVEL SECURITY")

    def test_new_global_table_access_requires_explicit_review(self):
        self.statement(
            self.sql.SQL("GRANT SELECT ON public.one_global_catalog TO {}")
            .format(self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError,"Unapproved SELECT"):
                self.inspect()
            widened=RestrictedReaderPolicy(
                login_role=self.reader,
                required_tables=self.policy.required_tables,
                allowed_tables=self.policy.allowed_tables | {"one_global_catalog"},
                globally_readable_tables=frozenset({"one_global_catalog"}),
                allowed_function_signatures=self.policy.allowed_function_signatures,
            )
            self.assertIn("one_global_catalog",self.inspect(widened).approved_table_reads)
        finally:
            self.statement(
                self.sql.SQL("REVOKE SELECT ON public.one_global_catalog FROM {}")
                .format(self.sql.Identifier(self.reader)))

    def test_unapproved_role_membership_blocks_even_without_inheritance(self):
        self.statement(
            self.sql.SQL("GRANT {} TO {}").format(
                self.sql.Identifier(self.group),self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError,"SET ROLE"):
                self.inspect()
            with self.assertRaisesRegex(AccountingError,"unapproved function"):
                # An explicitly allowed group still requires full grant auditing.
                self.statement(
                    self.sql.SQL("GRANT EXECUTE ON FUNCTION "
                                 "public.one_issue_unapproved() TO {}").format(
                                     self.sql.Identifier(self.group)))
                approved_group=RestrictedReaderPolicy(
                    login_role=self.reader,
                    required_tables=self.policy.required_tables,
                    allowed_tables=self.policy.allowed_tables,
                    allowed_function_signatures=self.policy.allowed_function_signatures,
                    permitted_group_roles=frozenset({self.group}),
                )
                self.inspect(approved_group)
        finally:
            self.statement(
                self.sql.SQL("REVOKE EXECUTE ON FUNCTION "
                             "public.one_issue_unapproved() FROM {}").format(
                                 self.sql.Identifier(self.group)))
            self.statement(
                self.sql.SQL("REVOKE {} FROM {}").format(
                    self.sql.Identifier(self.group),self.sql.Identifier(self.reader)))

    def test_column_only_select_on_private_table_is_not_invisible(self):
        self.statement(
            self.sql.SQL("GRANT SELECT(id) ON public.one_private_rows TO {}")
            .format(self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError, "Unapproved SELECT"):
                self.inspect()
        finally:
            self.statement(
                self.sql.SQL("REVOKE SELECT(id) ON public.one_private_rows FROM {}")
                .format(self.sql.Identifier(self.reader)))

    def test_column_only_select_does_not_satisfy_approved_full_table(self):
        self.statement(
            self.sql.SQL("REVOKE SELECT ON public.one_demo_rows FROM {}")
            .format(self.sql.Identifier(self.reader)))
        self.statement(
            self.sql.SQL("GRANT SELECT(id) ON public.one_demo_rows TO {}")
            .format(self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError, "Partial-column SELECT"):
                self.inspect()
        finally:
            self.statement(
                self.sql.SQL("REVOKE SELECT(id) ON public.one_demo_rows FROM {}")
                .format(self.sql.Identifier(self.reader)))
            self.statement(
                self.sql.SQL("GRANT SELECT ON public.one_demo_rows TO {}")
                .format(self.sql.Identifier(self.reader)))

    def test_column_only_mutation_acl_is_rejected(self):
        for action,column in (("INSERT","id"),("UPDATE","tenant_id"),
                              ("REFERENCES","id")):
            # SQL words come exclusively from the static test cases above.
            permission=action+"("+column+")"
            with self.subTest(action=action):
                self.statement(
                    self.sql.SQL("GRANT {} ON public.one_private_rows TO {}")
                    .format(self.sql.SQL(permission),self.sql.Identifier(self.reader)))
                try:
                    with self.assertRaisesRegex(AccountingError,"modify a column"):
                        self.inspect()
                finally:
                    self.statement(
                        self.sql.SQL("REVOKE {} ON public.one_private_rows FROM {}")
                        .format(self.sql.SQL(permission),self.sql.Identifier(self.reader)))

    def test_nonpublic_schema_table_grants_are_not_invisible_to_audit(self):
        self.statement("CREATE SCHEMA one_ci_private")
        try:
            self.statement("CREATE TABLE one_ci_private.one_secret (id integer)")
            self.statement(
                self.sql.SQL("GRANT USAGE ON SCHEMA one_ci_private TO {}").format(
                    self.sql.Identifier(self.reader)))
            self.statement(
                self.sql.SQL("GRANT SELECT ON one_ci_private.one_secret TO {}").format(
                    self.sql.Identifier(self.reader)))
            with self.assertRaisesRegex(AccountingError,"nonpublic relation"):
                self.inspect()
        finally:
            self.statement("DROP SCHEMA one_ci_private CASCADE")

    def test_nonpublic_schema_create_grant_is_release_blocking(self):
        self.statement("CREATE SCHEMA one_ci_extension")
        try:
            self.statement(
                self.sql.SQL("GRANT CREATE ON SCHEMA one_ci_extension TO {}").format(
                    self.sql.Identifier(self.reader)))
            with self.assertRaisesRegex(AccountingError,"custom schema"):
                self.inspect()
        finally:
            self.statement("DROP SCHEMA one_ci_extension CASCADE")

    def test_reader_cannot_use_unreviewed_owner_privileged_view(self):
        self.statement(
            "CREATE VIEW public.one_ci_reader_view AS "
            "SELECT id,tenant_id FROM public.one_demo_rows")
        try:
            self.statement(
                self.sql.SQL("GRANT SELECT ON public.one_ci_reader_view TO {}").format(
                    self.sql.Identifier(self.reader)))
            with self.assertRaisesRegex(AccountingError,"Unapproved SELECT"):
                self.inspect()
            expanded=RestrictedReaderPolicy(
                login_role=self.reader,required_tables=self.policy.required_tables,
                allowed_tables=self.policy.allowed_tables | {"one_ci_reader_view"},
                allowed_function_signatures=self.policy.allowed_function_signatures)
            with self.assertRaisesRegex(AccountingError,"Unreviewed view"):
                self.inspect(expanded)
        finally:
            self.statement("DROP VIEW public.one_ci_reader_view")

    def test_nonpublic_executable_function_grants_are_rejected(self):
        self.statement("CREATE SCHEMA one_ci_external")
        try:
            self.statement(
                "CREATE FUNCTION one_ci_external.one_privileged() "
                "RETURNS integer LANGUAGE sql AS 'SELECT 42'")
            self.statement(
                self.sql.SQL("GRANT USAGE ON SCHEMA one_ci_external TO {}").format(
                    self.sql.Identifier(self.reader)))
            self.statement(
                self.sql.SQL("GRANT EXECUTE ON FUNCTION "
                             "one_ci_external.one_privileged() TO {}").format(
                                 self.sql.Identifier(self.reader)))
            with self.assertRaisesRegex(AccountingError,"unapproved function"):
                self.inspect()
        finally:
            self.statement("DROP SCHEMA one_ci_external CASCADE")

    def test_directly_granted_function_not_in_review_allowlist_blocks(self):
        self.statement(
            self.sql.SQL("GRANT EXECUTE ON FUNCTION "
                         "public.one_issue_unapproved() TO {}").format(
                             self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError,"unapproved function"):
                self.inspect()
        finally:
            self.statement(
                self.sql.SQL("REVOKE EXECUTE ON FUNCTION "
                             "public.one_issue_unapproved() FROM {}").format(
                                 self.sql.Identifier(self.reader)))

    def test_sequence_usage_is_not_safe_for_report_only_login(self):
        self.statement("CREATE SEQUENCE public.one_ci_secret_seq")
        try:
            self.statement(
                self.sql.SQL("GRANT USAGE ON SEQUENCE public.one_ci_secret_seq TO {}")
                .format(self.sql.Identifier(self.reader)))
            with self.assertRaisesRegex(AccountingError,"use a sequence"):
                self.inspect()
        finally:
            self.statement("DROP SEQUENCE public.one_ci_secret_seq")

    def test_temp_create_permission_on_database_is_release_blocking(self):
        self.statement(
            self.sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}")
            .format(self.sql.Identifier(self.db_name),self.sql.Identifier(self.reader)))
        try:
            with self.assertRaisesRegex(AccountingError,"database/schema privileges"):
                self.inspect()
        finally:
            self.statement(
                self.sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}")
                .format(self.sql.Identifier(self.db_name),self.sql.Identifier(self.reader)))

    def test_invalid_policy_is_denied_without_db_access(self):
        class NoDb:
            def transaction(self):
                raise AssertionError("DB touched unexpectedly")
        for bad in [
            RestrictedReaderPolicy(login_role="--x",required_tables=frozenset({"one_demo_rows"}),
                                   allowed_tables=frozenset({"one_demo_rows"})),
            RestrictedReaderPolicy(login_role=self.reader,required_tables=frozenset(),
                                   allowed_tables=frozenset({"one_demo_rows"})),
            RestrictedReaderPolicy(login_role=self.reader,
                                   required_tables=frozenset({"one_demo_rows"}),
                                   allowed_tables=frozenset({"one_demo_rows"}),
                                   allowed_function_signatures=frozenset({"evil_function()"})),
        ]:
            with self.assertRaises(AccountingError):
                inspect_restricted_reader(
                    NoDb(),expected_database=self.db_name,policy=bad)


if __name__ == "__main__":
    unittest.main()
