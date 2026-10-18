"""Isolated PostgreSQL 14 CI proof of physical per-tenant database boundaries.

This is an EXPERIMENT, not a provisioning daemon or a production procedure.
Runs only with ONE_TEST_PG_DSN on a disposable GitHub Actions PostgreSQL service.
Creates and DESTROYS two random CI databases/roles. Never run on Oracle.
"""
from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.tenant_readiness import verify_physical_tenant_database
from one_erp_core.tenant_inventory import verify_exclusive_tenant_inventory

DSN = os.environ.get("ONE_TEST_PG_DSN")
PG_HOST = os.environ.get("ONE_TEST_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("ONE_TEST_PG_PORT", "5432"))
MIGRATIONS = Path(__file__).resolve().parents[1] / "db"


@unittest.skipUnless(
    os.environ.get("CI") == "true" and
    isinstance(DSN, str) and "dbname=one_erp_test" in DSN,
    "Requires explicit one_erp_test disposable GitHub CI database",
)
class PhysicallyIsolatedTenantDatabaseTests(unittest.TestCase):
    """CI simulation with two different databases and login principals.

    Postgres roles live at cluster scope. A production provisioner needs
    admin approval, secret storage, provisioning job fencing, recovery
    orchestration, a separate control plane and private connection routing.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql

        # This suite CREATEs and DROPs databases/roles: NEVER accept a
        # customer, Oracle, or other network PostgreSQL DSN by mistake.
        args = psycopg.conninfo.conninfo_to_dict(DSN)
        loopback = ("localhost", "127.0.0.1", "::1")
        if (args.get("dbname") != "one_erp_test" or
            args.get("host") not in loopback or
            args.get("hostaddr") or args.get("port", "5432") != "5432" or
            PG_HOST not in loopback or PG_PORT != 5432):
            raise RuntimeError("Refusing non-loopback or non-disposable CI database")
        cls.psycopg = psycopg
        cls.sql = sql
        cls.admin = psycopg.connect(DSN, autocommit=True)
        # Only opaque, random, tool-generated CI names. Never from headers or
        # a browser-provided tenant name, SQL fragment or database host.
        suffix = uuid4().hex[:14]
        cls.databases = [
            f"one_ci_isolation_a_{suffix}",
            f"one_ci_isolation_b_{suffix}",
        ]
        cls.roles = [
            f"one_ci_reader_a_{suffix}",
            f"one_ci_reader_b_{suffix}",
        ]
        cls.passwords = [secrets.token_urlsafe(30), secrets.token_urlsafe(30)]
        cls.tenants = [uuid4(), uuid4()]
        cls.actors = [uuid4(), uuid4()]
        cls.created_roles = []
        cls.created_databases = []
        try:
            for db, role, pw, tenant, actor in zip(
                cls.databases, cls.roles, cls.passwords, cls.tenants,
                cls.actors
            ):
                cls.admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(pw))
                )
                cls.created_roles.append(role)
                cls.admin.execute(
                    sql.SQL("CREATE DATABASE {} OWNER postgres TEMPLATE template0")
                    .format(sql.Identifier(db))
                )
                cls.created_databases.append(db)
                cls.admin.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC")
                    .format(sql.Identifier(db))
                )
                cls.admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}")
                    .format(sql.Identifier(db), sql.Identifier(role))
                )

                with cls._connection_to_database(db) as init:
                    # Migrate a separate *entire* four-stage business ledger
                    # into each physical database, never just dummy tables.
                    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
                        init.execute(
                            file.read_text(encoding="utf-8"),
                            prepare=False,
                        )
                    init.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                    init.execute(
                        sql.SQL("GRANT USAGE ON SCHEMA public TO {}")
                        .format(sql.Identifier(role))
                    )
                    init.execute(
                        sql.SQL(
                            "GRANT SELECT ON TABLE "
                            "one_tenants,one_memberships,one_accounts,"
                            "one_journal_entries,one_journal_lines TO {}"
                        ).format(sql.Identifier(role))
                    )
                    # Model a seeded company with its own identity.
                    init.execute(
                        "INSERT INTO one_tenants(id,company_name) VALUES (%s,%s)",
                        (tenant, f"Isolated demo tenant {len(cls.created_databases)}"),
                    )
                    init.execute(
                        "INSERT INTO one_memberships(tenant_id,actor_id,role) "
                        "VALUES (%s,%s,'accountant')",
                        (tenant, actor),
                    )
        except BaseException:
            cls._cleanup()
            raise

    @classmethod
    def _connection_to_database(cls, database: str):
        # Derive privileged CI connection from the supplied ephemeral DSN.
        # Never hardcode even throwaway passwords in reusable source files.
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(DSN)
        params["dbname"] = database
        return cls.psycopg.connect(**params, autocommit=True)

    @classmethod
    def _reader(cls, index: int, *, database: str | None = None):
        return cls.psycopg.connect(
            host=PG_HOST, port=PG_PORT,
            dbname=database or cls.databases[index],
            user=cls.roles[index], password=cls.passwords[index],
            autocommit=True,
            connect_timeout=3,
        )

    @classmethod
    def _cleanup(cls):
        # Drop only exact generated names created by this CI suite.
        if not hasattr(cls, "admin"):
            return
        for db in getattr(cls, "created_databases", []):
            try:
                cls.admin.execute(
                    cls.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                    .format(cls.sql.Identifier(db))
                )
            except cls.psycopg.Error:
                pass
        for role in getattr(cls, "created_roles", []):
            try:
                cls.admin.execute(
                    cls.sql.SQL("DROP ROLE IF EXISTS {}")
                    .format(cls.sql.Identifier(role))
                )
            except cls.psycopg.Error:
                pass
        cls.admin.close()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    def test_two_separate_full_ledger_schemas_and_single_company_each(self):
        self.assertNotEqual(self.databases[0], self.databases[1])
        self.assertNotEqual(self.roles[0], self.roles[1])
        for i in range(2):
            with self._reader(i) as db:
                with db.transaction():
                    db.execute(
                        "SELECT set_config('one.tenant_id',%s,true)",
                        (str(self.tenants[i]),),
                    )
                    db.execute(
                        "SELECT set_config('one.actor_id',%s,true)",
                        (str(self.actors[i]),),
                    )
                    actual = db.execute(
                        "SELECT id FROM one_tenants"
                    ).fetchall()
                    self.assertEqual(actual, [(self.tenants[i],)])
                    actual_role = db.execute(
                        "SELECT current_user"
                    ).fetchone()[0]
                    self.assertEqual(actual_role, self.roles[i])
                    # Schema includes payment and invoice ledger, but the
                    # reporting login is never allowed to inspect those.
                    with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                        db.execute("SELECT * FROM one_invoice_docs")
                # The exception rolls back the failed query transaction.

    def test_competing_tenant_reader_cannot_connect_to_other_database(self):
        for reader, target_db in ((0, self.databases[1]),
                                  (1, self.databases[0])):
            with self.assertRaises(self.psycopg.OperationalError):
                with self._reader(reader, database=target_db):
                    self.fail("Cross-tenant connection unexpectedly opened")

    def test_reader_is_not_admin_and_cannot_write_or_create_objects(self):
        for i in range(2):
            with self._reader(i) as db:
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    db.execute(
                        "INSERT INTO one_tenants(id,company_name) "
                        "VALUES (%s,%s)",
                        (uuid4(), "Forbidden"),
                    )
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    db.execute("CREATE TABLE unauthorized_ci_probe(id integer)")
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    db.execute("SELECT * FROM one_payment_docs")
                self.assertEqual(
                    db.execute("SELECT rolsuper,rolbypassrls "
                               "FROM pg_roles WHERE rolname=current_user")
                    .fetchone(),
                    (False, False),
                )

    def test_malicious_session_tenant_switch_cannot_read_other_company(self):
        for index in range(2):
            other = self.tenants[1-index]
            with self._reader(index) as db:
                with db.transaction():
                    db.execute(
                        "SELECT set_config('one.tenant_id',%s,true)",
                        (str(other),),
                    )
                    db.execute(
                        "SELECT set_config('one.actor_id',%s,true)",
                        (str(self.actors[1-index]),),
                    )
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM one_tenants")
                        .fetchone()[0], 0,
                    )

    def test_all_four_ledger_migrations_exist_in_each_database(self):
        for dbname in self.databases:
            with self._connection_to_database(dbname) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname='public'"
                ).fetchall()}
            for expected in (
                "one_tenants", "one_journal_entries",
                "one_invoice_docs", "one_payment_docs",
                "one_payment_allocations",
            ):
                self.assertIn(expected, tables)


    def test_readiness_evidence_is_computed_from_verified_live_db_facts(self):
        for i in range(2):
            with self._reader(i) as db:
                evidence = verify_physical_tenant_database(
                    db, expected_tenant=self.tenants[i],
                    expected_actor=self.actors[i],
                    expected_database=self.databases[i],
                    expected_reader_role=self.roles[i],
                )
                self.assertEqual(evidence.database_name, self.databases[i])
                self.assertEqual(evidence.tenant_id, self.tenants[i])
                self.assertEqual(len(evidence.digest), 32)
                self.assertIn("one_invoice_docs", evidence.observed_tables)
                self.assertIn("one_payment_allocations", evidence.observed_tables)
        self.assertNotEqual(
            self.tenants[0], self.tenants[1],
        )

    def test_readiness_preflight_rejects_wrong_tenant_db_role_and_actor(self):
        with self._reader(0) as db:
            good = dict(
                expected_tenant=self.tenants[0],
                expected_actor=self.actors[0],
                expected_database=self.databases[0],
                expected_reader_role=self.roles[0],
            )
            for bad in (
                {"expected_tenant": self.tenants[1]},
                {"expected_actor": self.actors[1]},
                {"expected_database": self.databases[1]},
                {"expected_reader_role": self.roles[1]},
                {"expected_database": "something; DROP DATABASE postgres"},
            ):
                fields = good | bad
                with self.assertRaises(AccountingError):
                    verify_physical_tenant_database(db, **fields)

    def test_readiness_refuses_privileged_admin_connection(self):
        # Even the administrator must not be accepted as an application
        # report-reader, regardless of superuser/RLS bypass privileges.
        with self._connection_to_database(self.databases[0]) as admin_db:
            with self.assertRaises(AccountingError):
                verify_physical_tenant_database(
                    admin_db, expected_tenant=self.tenants[0],
                    expected_actor=self.actors[0],
                    expected_database=self.databases[0],
                    expected_reader_role="postgres",
                )


    def test_exclusive_inventory_checks_actual_rows_and_peer_connect_privileges(self):
        for i in range(2):
            with self._connection_to_database(self.databases[i]) as inspector:
                evidence = verify_exclusive_tenant_inventory(
                    inspector,
                    expected_tenant=self.tenants[i],
                    expected_database=self.databases[i],
                    expected_reader_role=self.roles[i],
                    forbidden_peer_roles=(self.roles[1-i],),
                )
                self.assertEqual(evidence.tenant_id, self.tenants[i])
                self.assertEqual(evidence.reader_role, self.roles[i])
                self.assertEqual(len(evidence.digest), 32)

    def test_exclusive_inventory_detects_hidden_second_tenant(self):
        rogue = uuid4()
        with self._connection_to_database(self.databases[0]) as admin_db:
            admin_db.execute(
                "INSERT INTO one_tenants(id,company_name) VALUES (%s,%s)",
                (rogue, "Synthetic stray tenant"),
            )
            try:
                with self.assertRaises(AccountingError):
                    verify_exclusive_tenant_inventory(
                        admin_db,
                        expected_tenant=self.tenants[0],
                        expected_database=self.databases[0],
                        expected_reader_role=self.roles[0],
                        forbidden_peer_roles=(self.roles[1],),
                    )
            finally:
                admin_db.execute("DELETE FROM one_tenants WHERE id=%s", (rogue,))

    def test_exclusive_inventory_denies_public_database_connect(self):
        database = self.databases[0]
        self.admin.execute(
            self.sql.SQL("GRANT CONNECT ON DATABASE {} TO PUBLIC")
            .format(self.sql.Identifier(database))
        )
        try:
            with self._connection_to_database(database) as inspector:
                with self.assertRaises(AccountingError):
                    verify_exclusive_tenant_inventory(
                        inspector,
                        expected_tenant=self.tenants[0],
                        expected_database=database,
                        expected_reader_role=self.roles[0],
                        forbidden_peer_roles=(self.roles[1],),
                    )
        finally:
            self.admin.execute(
                self.sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC")
                .format(self.sql.Identifier(database))
            )

    def test_exclusive_inventory_denies_foreign_reader_connect(self):
        database, foreign = self.databases[0], self.roles[1]
        self.admin.execute(
            self.sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                self.sql.Identifier(database), self.sql.Identifier(foreign)
            )
        )
        try:
            with self._connection_to_database(database) as inspector:
                with self.assertRaises(AccountingError):
                    verify_exclusive_tenant_inventory(
                        inspector,
                        expected_tenant=self.tenants[0],
                        expected_database=database,
                        expected_reader_role=self.roles[0],
                        forbidden_peer_roles=(foreign,),
                    )
        finally:
            self.admin.execute(
                self.sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    self.sql.Identifier(database), self.sql.Identifier(foreign)
                )
            )

    def test_exclusive_inventory_rejects_tenant_reader_and_bad_scope(self):
        base = dict(
            expected_tenant=self.tenants[0],
            expected_database=self.databases[0],
            expected_reader_role=self.roles[0],
            forbidden_peer_roles=(self.roles[1],),
        )
        with self._reader(0) as reader:
            with self.assertRaises(AccountingError):
                verify_exclusive_tenant_inventory(reader, **base)
        with self._connection_to_database(self.databases[0]) as inspector:
            for override in (
                {"expected_tenant": self.tenants[1]},
                {"expected_database": self.databases[1]},
                {"expected_reader_role": self.roles[1]},
                {"forbidden_peer_roles": ()},
                {"forbidden_peer_roles": (self.roles[0],)},
                {"forbidden_peer_roles": ("invalid;identifier",)},
            ):
                with self.subTest(override=override), self.assertRaises(AccountingError):
                    verify_exclusive_tenant_inventory(inspector, **(base | override))

