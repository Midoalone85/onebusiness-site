"""Adversarial catalog preflight tests; no network or database mutation."""
from contextlib import contextmanager
import unittest

from one_erp_core.staging_pg_preflight import inspect_restricted_staging_reader

DB="one_erp_ci_stage"
ROLE="one_stage_reader"


class FakeCursor:
    def __init__(self, state):
        self.s=state
        self.answer=None
        self.queries=[]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql,params))
        if self.s["error"]:
            raise RuntimeError("Do not expose db://secret-password")
        if "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in sql:
            self.answer=None
        elif "current_database()" in sql:
            self.answer=(
                self.s["database"], self.s["role"], *self.s["flags"])
        elif "has_table_privilege" in sql:
            name=params[0].removeprefix("public.")
            privilege=params[1] if len(params)>1 else "SELECT"
            self.answer=(self.s["grants"].get((name,privilege),False),)
        elif "relforcerowsecurity" in sql:
            name=params[0].removeprefix("public.")
            self.answer=self.s["rls"].get(name)
        else:
            raise AssertionError("Unexpected source SQL")

    def fetchone(self):
        return self.answer


class FakeConnection:
    def __init__(self):
        self.state={
            "database":DB,"role":ROLE,
            "flags":(False,False,False,False,False,False),
            "grants":{},
            "rls":{name:(True,True) for name in (
                "one_invoice_source_requests","one_invoice_source_approvals",
                "one_journal_entries","one_invoice_docs")},
            "error":False,
        }
        for name in (
            "one_memberships","one_commercial_orders",
            "one_stock_balances","one_return_jobs"):
            self.state["grants"][(name,"SELECT")]=True
        self.cur=FakeCursor(self.state)
        self.commits=0

    @contextmanager
    def transaction(self):
        yield
        self.commits+=1

    def cursor(self):
        return self.cur


class StagingPostgresPreflightTests(unittest.TestCase):
    def setUp(self):
        self.db=FakeConnection()

    def check(self):
        return inspect_restricted_staging_reader(
            self.db, expected_database=DB, expected_role=ROLE)

    def test_restricted_role_with_required_view_grants_and_forced_rls_passes(self):
        self.assertTrue(self.check())
        self.assertEqual(self.db.commits,1)
        self.assertTrue(any("REPEATABLE READ, READ ONLY" in sql
                            for sql,_ in self.db.cur.queries))
        self.assertTrue(all(
            "INSERT " not in sql and "UPDATE " not in sql
            for sql,_ in self.db.cur.queries))

    def test_rejects_superuser_bypassrls_createdb_createrole_ddl(self):
        for index in range(6):
            with self.subTest(index=index):
                self.db=FakeConnection()
                flags=list(self.db.state["flags"])
                flags[index]=True
                self.db.state["flags"]=tuple(flags)
                self.assertFalse(self.check())

    def test_role_and_database_must_be_exact(self):
        for key,bad in (("role","postgres"),("role","other_login"),
                        ("database","wrong_tenant_db")):
            with self.subTest(key=key,bad=bad):
                self.db=FakeConnection()
                self.db.state[key]=bad
                self.assertFalse(self.check())

    def test_missing_any_operational_read_grant_fails(self):
        for name in (
            "one_memberships","one_commercial_orders",
            "one_stock_balances","one_return_jobs"):
            with self.subTest(name=name):
                self.db=FakeConnection()
                self.db.state["grants"][(name,"SELECT")]=False
                self.assertFalse(self.check())

    def test_inherited_or_public_write_privilege_blocks_readiness(self):
        for name in (
            "one_invoice_source_requests","one_invoice_source_approvals",
            "one_journal_entries","one_journal_lines",
            "one_invoice_docs","one_payment_docs"):
            for right in ("INSERT","UPDATE","DELETE","TRUNCATE","TRIGGER"):
                with self.subTest(name=name,right=right):
                    self.db=FakeConnection()
                    self.db.state["grants"][(name,right)]=True
                    self.assertFalse(self.check())

    def test_unforced_or_disabled_rls_denies_reader_preflight(self):
        for name in (
            "one_invoice_source_requests","one_invoice_source_approvals",
            "one_journal_entries","one_invoice_docs"):
            for value in ((True,False),(False,True),(False,False),None):
                with self.subTest(name=name,value=value):
                    self.db=FakeConnection()
                    self.db.state["rls"][name]=value
                    self.assertFalse(self.check())

    def test_connection_error_is_generic_fail_closed(self):
        self.db.state["error"]=True
        self.assertFalse(self.check())

    def test_invalid_expected_identifiers_fail_before_database_contact(self):
        for db,role in (
            ("",ROLE),("mydb;DROP ROLE postgres",ROLE),
            ("UpperCase",ROLE),(DB,"postgres"),(DB,"public"),
            (DB,"a"),(DB,"evil-owner"),
        ):
            with self.subTest(db=db,role=role):
                with self.assertRaises(ValueError):
                    inspect_restricted_staging_reader(
                        self.db,expected_database=db,expected_role=role)
        self.assertEqual(self.db.cur.queries,[])


if __name__ == "__main__":
    unittest.main()
