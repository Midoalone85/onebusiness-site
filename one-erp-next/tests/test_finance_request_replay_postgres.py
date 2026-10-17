"""Real concurrency and rollback tests, safe only on disposable PostgreSQL CI."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from pathlib import Path
from threading import Event
from time import monotonic, sleep
import unittest
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.finance_request_replay import _run_once
from one_erp_core.tenancy import Membership, Role

DSN = os.environ.get("ONE_TEST_PG_DSN", "")


@unittest.skipUnless(
    os.environ.get("CI") == "true" and "dbname=one_erp_test" in DSN,
    "Run only against disposable PostgreSQL CI service",
)
class PostgresFinanceReplayRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.psycopg, cls.sql = psycopg, sql
        cls.args = psycopg.conninfo.conninfo_to_dict(DSN)
        if cls.args.get("dbname") != "one_erp_test":
            raise RuntimeError("Refusing to touch a non-test database")
        cls.admin = psycopg.connect(DSN, autocommit=True)
        cls.db_name = "one_erp_ci_replay_" + uuid4().hex[:12]
        cls.admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.db_name))
        )
        try:
            with cls.connect() as conn:
                root = Path(__file__).resolve().parents[1]
                for migration in sorted((root / "db").glob("[0-9][0-9][0-9]_*.sql")):
                    conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
                conn.execute(
                    """CREATE TABLE ci_replay_side_effects(
                       tenant_id uuid NOT NULL, request_id uuid NOT NULL,
                       PRIMARY KEY(tenant_id,request_id))"""
                )
                if conn.execute(
                    "SELECT to_regclass('public.one_finance_requests')"
                ).fetchone()[0] is None:
                    raise AssertionError("Missing migration 057")
        except BaseException:
            cls.admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(cls.db_name)))
            cls.admin.close()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "admin") or cls.admin.closed:
            return
        try:
            cls.admin.execute(
                cls.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    cls.sql.Identifier(cls.db_name)))
        finally:
            cls.admin.close()

    @classmethod
    def connect(cls, application_name=None):
        args = dict(cls.args, dbname=cls.db_name)
        if application_name:
            args["application_name"] = application_name
        return cls.psycopg.connect(**args, autocommit=True)

    def setUp(self):
        self.tenant, self.actor, self.document = uuid4(), uuid4(), uuid4()
        self.memberships = (Membership(self.actor, self.tenant, Role.ACCOUNTANT),)
        with self.connect() as conn:
            conn.execute("INSERT INTO one_tenants(id,company_name) VALUES(%s,%s)",
                         (self.tenant, "Isolated replay fixture"))
            conn.execute(
                """INSERT INTO one_memberships(tenant_id,actor_id,role)
                   VALUES(%s,%s,'accountant')""",
                (self.tenant, self.actor))

    def request(self, conn, amount="115.00", write=None):
        def save():
            conn.execute("INSERT INTO ci_replay_side_effects VALUES(%s,%s)",
                         (self.tenant, self.document))
            return self.document
        return _run_once(
            conn, operation="internal_invoice", document_id=self.document,
            tenant_id=self.tenant, actor_id=self.actor,
            memberships=self.memberships,
            payload={"amount": Decimal(amount), "currency": "SAR"},
            write=write if write is not None else save,
        )

    def effect_count(self):
        with self.connect() as conn:
            return conn.execute(
                """SELECT count(*) FROM ci_replay_side_effects
                   WHERE tenant_id=%s AND request_id=%s""",
                (self.tenant, self.document)).fetchone()[0]

    def test_two_connections_same_request_post_once(self):
        first_in_write, release_first = Event(), Event()
        app = "one_replay_" + uuid4().hex[:12]

        def first():
            with self.connect() as conn:
                def save():
                    first_in_write.set()
                    if not release_first.wait(15):
                        raise RuntimeError("First transaction release timeout")
                    conn.execute("INSERT INTO ci_replay_side_effects VALUES(%s,%s)",
                                 (self.tenant, self.document))
                    return self.document
                return self.request(conn, write=save)

        def second():
            with self.connect(app) as conn:
                return self.request(conn)

        with ThreadPoolExecutor(max_workers=2) as pool:
            original = pool.submit(first)
            self.assertTrue(first_in_write.wait(12), "First reservation failed")
            retry = pool.submit(second)
            try:
                deadline = monotonic() + 9
                with self.connect() as observer:
                    while monotonic() < deadline:
                        waiting = observer.execute(
                            """SELECT count(*) FROM pg_stat_activity
                               WHERE application_name=%s AND wait_event_type='Lock'""",
                            (app,)).fetchone()[0]
                        if waiting:
                            break
                        sleep(0.03)
                    else:
                        self.fail("Retry did not block on first transaction")
            finally:
                release_first.set()
            a, b = original.result(timeout=18), retry.result(timeout=18)
        self.assertFalse(a.replayed)
        self.assertTrue(b.replayed)
        self.assertEqual(a.document_id, b.document_id)
        self.assertEqual(self.effect_count(), 1)

    def test_failure_undoes_reservation_and_posting(self):
        with self.connect() as conn:
            def failed_post():
                conn.execute("INSERT INTO ci_replay_side_effects VALUES(%s,%s)",
                             (self.tenant, self.document))
                raise AccountingError("Simulated posting failure")
            with self.assertRaisesRegex(AccountingError, "Simulated"):
                self.request(conn, write=failed_post)
        self.assertEqual(self.effect_count(), 0)
        with self.connect() as conn:
            count = conn.execute(
                """SELECT count(*) FROM one_finance_requests
                   WHERE tenant_id=%s AND request_id=%s""",
                (self.tenant, self.document)).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertFalse(self.request(conn).replayed)
            self.assertTrue(self.request(conn).replayed)
        self.assertEqual(self.effect_count(), 1)

    def test_changed_amount_same_reference_rejected(self):
        with self.connect() as conn:
            self.assertFalse(self.request(conn).replayed)
        with self.connect() as conn:
            with self.assertRaisesRegex(AccountingError, "Conflicting"):
                self.request(conn, amount="116.00")
        self.assertEqual(self.effect_count(), 1)



    def test_incomplete_reservation_is_rejected_on_commit(self):
        with self.connect() as conn:
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                conn.execute(
                    """INSERT INTO one_finance_requests
                       (tenant_id,operation,request_id,actor_id,request_sha256)
                       VALUES(%s,'internal_invoice',%s,%s,%s)""",
                    (self.tenant, self.document, self.actor, "0" * 64),
                )
            self.assertEqual(
                conn.execute(
                    """SELECT count(*) FROM one_finance_requests
                       WHERE tenant_id=%s AND request_id=%s""",
                    (self.tenant, self.document),
                ).fetchone()[0], 0,
            )

    def test_completed_finance_receipt_is_immutable(self):
        with self.connect() as conn:
            self.assertFalse(self.request(conn).replayed)
            with self.assertRaises(self.psycopg.errors.RaiseException):
                conn.execute(
                    """DELETE FROM one_finance_requests
                       WHERE tenant_id=%s AND request_id=%s""",
                    (self.tenant, self.document),
                )
            with self.assertRaises(self.psycopg.errors.RaiseException):
                conn.execute(
                    """UPDATE one_finance_requests SET result_id=NULL
                       WHERE tenant_id=%s AND request_id=%s""",
                    (self.tenant, self.document),
                )
            self.assertTrue(self.request(conn).replayed)
        self.assertEqual(self.effect_count(), 1)

    def test_replay_receipts_require_rls_and_revoke_public_grants(self):
        with self.connect() as conn:
            flags = conn.execute(
                """SELECT relrowsecurity,relforcerowsecurity
                   FROM pg_class WHERE oid='public.one_finance_requests'::regclass"""
            ).fetchone()
            self.assertEqual(flags, (True, True))
            public_grants = conn.execute(
                """SELECT count(*) FROM pg_class c,
                       LATERAL aclexplode(c.relacl) acl
                   WHERE c.oid='public.one_finance_requests'::regclass
                     AND acl.grantee=0"""
            ).fetchone()[0]
            self.assertEqual(public_grants, 0)


if __name__ == "__main__":
    unittest.main()
