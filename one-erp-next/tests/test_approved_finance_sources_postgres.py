"""Real PostgreSQL acceptance for independent approved invoice source + replay.

Creates and destroys a disposable database in GitHub CI ONLY. No customer
records, bank payments, ZATCA action or deployed HTTP server.
"""
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.approved_finance_sources import ApprovedPostgresFinanceSourceLoader
from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_replay_commands_wsgi import execute_verified_document_request
from one_erp_core.tenancy import Role

DSN=os.environ.get("ONE_TEST_PG_DSN","")


@unittest.skipUnless(
    os.environ.get("CI")=="true" and "dbname=one_erp_test" in DSN,
    "Dedicated disposable PostgreSQL CI database required",
)
class ApprovedSourcePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.pg=psycopg
        cls.sql=sql
        cls.args=psycopg.conninfo.conninfo_to_dict(DSN)
        if cls.args.get("dbname")!="one_erp_test":
            raise RuntimeError("Refusing non-CI database")
        cls.root=psycopg.connect(DSN,autocommit=True)
        cls.db_name="one_erp_ci_source_"+uuid4().hex[:12]
        cls.root.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.db_name)))
        try:
            with cls.connect() as conn:
                folder=Path(__file__).resolve().parents[1]
                for migration in sorted((folder/"db").glob("[0-9][0-9][0-9]_*.sql")):
                    conn.execute(migration.read_text(encoding="utf-8"),prepare=False)
                assert conn.execute(
                    "SELECT to_regclass('public.one_invoice_source_requests')"
                ).fetchone()[0] is not None
        except BaseException:
            cls.root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(cls.db_name)))
            cls.root.close()
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls,"root") or cls.root.closed:
            return
        try:
            cls.root.execute(
                cls.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    cls.sql.Identifier(cls.db_name)))
        finally:
            cls.root.close()

    @classmethod
    def connect(cls):
        return cls.pg.connect(**dict(cls.args,dbname=cls.db_name),autocommit=True)

    def setUp(self):
        self.tenant,self.actor,self.reviewer=uuid4(),uuid4(),uuid4()
        self.doc_id,self.period_id=uuid4(),uuid4()
        self.loader=ApprovedPostgresFinanceSourceLoader()
        self.payload={
            "kind":"customer_invoice","number":"CI-"+uuid4().hex[:12],
            "partner_id":"customer-ci","issued_on":"2026-10-17",
            "due_on":"2026-11-17","currency":"SAR","source_invoice_id":None,
            "accounts":{
                "receivable_id":"ar","payable_id":"ap",
                "output_vat_id":"outputvat","input_vat_id":"inputvat",
            },
            "lines":[{
                "description":"Example","account_id":"sales",
                "quantity":"1","unit_price":"100.00","discount_amount":"0.00",
                "tax_category":"out_of_scope","tax_rate":"0",
                "tax_code":"NONE","input_tax_recoverable":False,
            }],
        }
        with self.connect() as conn:
            conn.execute("INSERT INTO one_tenants(id,company_name) VALUES(%s,%s)",
                         (self.tenant,"CI only"))
            for actor in (self.actor,self.reviewer):
                conn.execute(
                    """INSERT INTO one_memberships(tenant_id,actor_id,role)
                       VALUES(%s,%s,'accountant')""",(self.tenant,actor))
            conn.execute(
                """INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive)
                   VALUES(%s,%s,'2026-01-01','2027-01-01')""",
                (self.tenant,self.period_id))
            for key,kind in (
                ("ar","asset"),("ap","liability"),("sales","revenue"),
                ("outputvat","liability"),("inputvat","asset"),
                ("inventory","asset"),("grni","liability"),("cogs","expense"),
            ):
                conn.execute(
                    """INSERT INTO one_accounts(tenant_id,id,code,name,kind)
                       VALUES(%s,%s,%s,%s,%s)""",
                    (self.tenant,key,key.upper(),key,kind))

    def bind(self,conn,actor):
        conn.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant),))
        conn.execute("SELECT set_config('one.actor_id',%s,true)",(str(actor),))

    def prepare(self,conn,payload=None,requested_by=None):
        with conn.transaction():
            self.bind(conn,requested_by or self.actor)
            conn.execute(
                """INSERT INTO one_invoice_source_requests
                   (tenant_id,document_id,period_id,requested_by,payload)
                   VALUES(%s,%s,%s,%s,%s::jsonb)""",
                (self.tenant,self.doc_id,self.period_id,
                 requested_by or self.actor,json.dumps(
                     self.payload if payload is None else payload)),
            )

    def approve(self,conn,actor=None):
        with conn.transaction():
            actor=actor or self.reviewer
            self.bind(conn,actor)
            conn.execute(
                """INSERT INTO one_invoice_source_approvals
                   (tenant_id,document_id,approved_by)
                   VALUES(%s,%s,%s)""",
                (self.tenant,self.doc_id,actor),
            )

    def execute(self,conn):
        command={"action":"issue_internal_invoice",
                 "tenant":self.tenant,"document_id":self.doc_id}
        return execute_verified_document_request(
            conn,
            identity=AuthenticatedFinanceIdentity(
                self.actor,frozenset({self.tenant})),
            command=command,source_loader=self.loader,
        )

    def count(self,table,id_col):
        with self.connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "+id_col+"=%s",
                (self.tenant,self.doc_id)).fetchone()[0]

    def test_real_approved_invoice_posts_once_with_durable_replay(self):
        with self.connect() as conn:
            self.prepare(conn)
            with self.assertRaises(PermissionError):
                self.execute(conn)  # no independent approval yet
            self.approve(conn)
            first=self.execute(conn)
        self.assertFalse(first.replayed)
        with self.connect() as conn:
            duplicate=self.execute(conn)  # new connection, same approved source
        self.assertTrue(duplicate.replayed)
        self.assertEqual(first.document_id,duplicate.document_id)
        for table,col in (
            ("one_invoice_docs","id"),("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),("one_finance_requests","request_id"),
        ):
            with self.subTest(table=table):
                self.assertEqual(self.count(table,col),1)
        with self.connect() as conn:
            status=conn.execute(
                """SELECT status,gross_total FROM one_invoice_docs
                   WHERE tenant_id=%s AND id=%s""",
                (self.tenant,self.doc_id)).fetchone()
            self.assertEqual(status,("issued",Decimal("100.000")))

    def test_same_actor_cannot_approve_and_source_is_immutable(self):
        with self.connect() as conn:
            self.prepare(conn)
            with self.assertRaises(self.pg.Error):
                self.approve(conn,self.actor)
            self.approve(conn)
            with conn.transaction():
                self.bind(conn,self.reviewer)
                for statement in (
                    "UPDATE one_invoice_source_requests SET period_id=%s",
                    "DELETE FROM one_invoice_source_requests WHERE period_id=%s",
                ):
                    with self.subTest(statement=statement):
                        with self.assertRaises(self.pg.Error):
                            with conn.transaction():
                                conn.execute(
                                    statement+" AND tenant_id=%s AND document_id=%s"
                                    if statement.startswith("DELETE") else
                                    statement+" WHERE tenant_id=%s AND document_id=%s",
                                    (self.period_id,self.tenant,self.doc_id)
                                    if statement.startswith("UPDATE") else
                                    (self.period_id,self.tenant,self.doc_id),
                                )
                with self.assertRaises(self.pg.Error):
                    with conn.transaction():
                        conn.execute(
                            """DELETE FROM one_invoice_source_approvals
                               WHERE tenant_id=%s AND document_id=%s""",
                            (self.tenant,self.doc_id),
                        )
        self.assertEqual(self.count("one_invoice_source_approvals","document_id"),1)

    def test_malformed_amount_does_not_write_any_financial_document(self):
        source=dict(self.payload)
        source["lines"]=[dict(self.payload["lines"][0],unit_price=100.0)]
        with self.connect() as conn:
            self.prepare(conn,payload=source)
            self.approve(conn)
            with self.assertRaises(PermissionError):
                self.execute(conn)
        self.assertEqual(self.count("one_journal_entries","id"),0)
        self.assertEqual(self.count("one_finance_requests","request_id"),0)

    def test_revoked_membership_fails_closed(self):
        with self.connect() as conn:
            self.prepare(conn)
            self.approve(conn)
            conn.execute(
                """UPDATE one_memberships SET active=false
                   WHERE tenant_id=%s AND actor_id=%s""",
                (self.tenant,self.actor))
            with self.assertRaises(PermissionError):
                self.execute(conn)
        self.assertEqual(self.count("one_invoice_docs","id"),0)

    def test_unapproved_or_cross_company_source_cannot_be_loaded(self):
        with self.connect() as conn:
            self.prepare(conn)
            with conn.transaction():
                with self.assertRaises(PermissionError):
                    self.loader.load_invoice(
                        conn,tenant_id=self.tenant,
                        document_id=self.doc_id,actor_id=self.actor)
            self.approve(conn)
            with conn.transaction():
                with self.assertRaises(PermissionError):
                    self.loader.load_invoice(
                        conn,tenant_id=uuid4(),
                        document_id=self.doc_id,actor_id=self.actor)

    def test_rls_enabled_for_approved_sources(self):
        with self.connect() as conn:
            rows=conn.execute(
                """SELECT relname,relrowsecurity,relforcerowsecurity
                   FROM pg_class WHERE relname IN
                   ('one_invoice_source_requests','one_invoice_source_approvals')
                   ORDER BY relname""").fetchall()
            self.assertEqual(len(rows),2)
            self.assertTrue(all(a and b for _,a,b in rows))



    def test_signed_https_request_posts_approved_invoice_and_replays(self):
        """Signed token -> HTTPS route -> source approval -> real PostgreSQL journal."""
        from datetime import datetime, timezone
        from io import BytesIO
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from one_erp_core.finance_replay_commands_wsgi import OneFinanceInternalDocumentsWSGI
        from one_erp_core.finance_wsgi import TenantFinanceConnections
        from one_erp_core.oidc_identity import RS256VerifierConfig, VerifiedRS256FinanceAccess

        with self.connect() as conn:
            self.prepare(conn)
            self.approve(conn)
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        config = RS256VerifierConfig(
            issuer="https://id.example.test", audience="one-erp-finance-api",
            signing_keys_by_id={"approved-source-ci":pem},
        )
        actor,tenant=self.actor,self.tenant

        class Directory:
            def resolve(self,issuer,subject):
                if subject!="trusted-accountant":
                    raise PermissionError("Not a finance user")
                return AuthenticatedFinanceIdentity(actor,frozenset({tenant}))

        class Revocations:
            def is_revoked(self,issuer,jwt_id):
                return False

        app = OneFinanceInternalDocumentsWSGI(
            verifier=VerifiedRS256FinanceAccess(config,Directory(),Revocations()),
            connections=TenantFinanceConnections({tenant:self.connect}),
            source_loader=self.loader,
            allowed_hosts=frozenset({"finance.example.test"}),
        )
        now=int(datetime.now(timezone.utc).timestamp())
        token=jwt.encode({
            "iss":config.issuer,"aud":config.audience,"sub":"trusted-accountant",
            "iat":now-5,"nbf":now-5,"exp":now+180,
            "jti":"approved-ci-token","token_use":"access",
        },key,algorithm="RS256",headers={"kid":"approved-source-ci"})

        def post():
            body=json.dumps({
                "action":"issue_internal_invoice","tenant":str(self.tenant),
                "document_id":str(self.doc_id),
            }).encode()
            status={}
            reply=b"".join(app({
                "REQUEST_METHOD":"POST","wsgi.url_scheme":"https",
                "HTTP_HOST":"finance.example.test",
                "PATH_INFO":"/api/v1/finance/internal-documents",
                "CONTENT_TYPE":"application/json",
                "CONTENT_LENGTH":str(len(body)),
                "QUERY_STRING":"","HTTP_COOKIE":"",
                "HTTP_AUTHORIZATION":"Bearer "+token,
                "wsgi.input":BytesIO(body),
            },lambda code,headers:status.update(
                code=code,headers=dict(headers))))
            return status,json.loads(reply)

        first,first_doc=post()
        second,second_doc=post()
        self.assertEqual((first["code"],second["code"]),("200 OK","200 OK"))
        self.assertEqual(first_doc["document_id"],str(self.doc_id))
        self.assertFalse(first_doc["replayed"])
        self.assertTrue(second_doc["replayed"])
        self.assertIn("no-store",second["headers"]["Cache-Control"])
        self.assertEqual(self.count("one_invoice_docs","id"),1)
        self.assertEqual(self.count("one_journal_entries","id"),1)



    def test_restricted_runtime_role_obeys_rls_and_cannot_write(self):
        """Actual LOGIN role with only SELECT must be unable to mutate source or GL."""
        role="one_ci_source_read_"+uuid4().hex[:12]
        password="ci_temporary_read_only"
        ident=self.sql.Identifier(role)
        self.root.execute(
            self.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS")
            .format(ident,self.sql.Literal(password)))
        try:
            with self.connect() as admin:
                admin.execute(self.sql.SQL(
                    "GRANT USAGE ON SCHEMA public TO {}").format(ident))
                admin.execute(self.sql.SQL(
                    "GRANT SELECT ON public.one_invoice_source_requests,"
                    " public.one_invoice_source_approvals TO {}").format(ident))
            with self.connect() as admin:
                self.prepare(admin)
                self.approve(admin)
            args=dict(self.args,dbname=self.db_name,user=role,password=password)
            with self.pg.connect(**args,autocommit=True) as reader:
                with reader.transaction():
                    reader.execute("SELECT set_config('one.tenant_id',%s,true)",
                                   (str(self.tenant),))
                    reader.execute("SELECT set_config('one.actor_id',%s,true)",
                                   (str(self.actor),))
                    count=reader.execute(
                        """SELECT count(*) FROM one_invoice_source_requests
                           WHERE tenant_id=%s AND document_id=%s""",
                        (self.tenant,self.doc_id)).fetchone()[0]
                    self.assertEqual(count,1)
                    # A restricted LOGIN has no source or journal write grants.
                    with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                        with reader.transaction():
                            reader.execute(
                                """UPDATE one_invoice_source_requests
                                   SET payload=payload WHERE tenant_id=%s""",
                                (self.tenant,))
                    with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                        with reader.transaction():
                            reader.execute(
                                """DELETE FROM one_invoice_source_approvals
                                   WHERE tenant_id=%s""",(self.tenant,))
                    with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                        with reader.transaction():
                            reader.execute("SELECT count(*) FROM one_journal_entries")
                    # RLS never allows browsing another tenant's approved inputs.
                    reader.execute("SELECT set_config('one.tenant_id',%s,true)",
                                   (str(uuid4()),))
                    self.assertEqual(reader.execute(
                        """SELECT count(*) FROM one_invoice_source_requests
                           WHERE tenant_id=%s""",(self.tenant,)).fetchone()[0],0)
        finally:
            with self.connect() as admin:
                admin.execute(self.sql.SQL("DROP OWNED BY {}").format(ident))
            self.root.execute(self.sql.SQL("DROP ROLE {}").format(ident))

    def test_many_simultaneous_approved_invoice_submissions_post_only_once(self):
        """Real multi-connection race, never a fake database mock."""
        from concurrent.futures import ThreadPoolExecutor
        with self.connect() as conn:
            self.prepare(conn)
            self.approve(conn)
        def submit(_):
            with self.connect() as conn:
                return self.execute(conn)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(submit,range(8)))
        self.assertEqual(sum(not r.replayed for r in results),1)
        self.assertEqual(sum(r.replayed for r in results),7)
        for table,col in (
            ("one_invoice_docs","id"),("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),("one_finance_requests","request_id"),
        ):
            self.assertEqual(self.count(table,col),1)

    def test_viewer_cannot_prepare_or_approve_source(self):
        viewer=uuid4()
        with self.connect() as conn:
            conn.execute("""INSERT INTO one_memberships(tenant_id,actor_id,role)
                            VALUES(%s,%s,'viewer')""",(self.tenant,viewer))
            with self.assertRaises(self.pg.Error):
                self.prepare(conn,requested_by=viewer)
            self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)
            self.prepare(conn)
            with self.assertRaises(self.pg.Error):
                self.approve(conn,viewer)
            self.assertEqual(self.count("one_invoice_source_approvals","document_id"),0)

    def test_invalid_approved_source_variations_never_post(self):
        """Reject tampered invoice snapshot inputs without residual ledger rows."""
        from copy import deepcopy
        mutations=(
            lambda p: p["lines"][0].update(unit_price="NaN"),
            lambda p: p["lines"][0].update(unit_price="1e10"),
            lambda p: p["lines"][0].update(quantity="-1"),
            lambda p: p["lines"][0].update(quantity="0"),
            lambda p: p["lines"][0].update(tax_rate="0.15"),
            lambda p: p["lines"][0].update(tax_category="standard"),
            lambda p: p.update(currency="sar"),
            lambda p: p.update(issued_on="2026-14-01"),
            lambda p: p.update(due_on="2026-01-01"),
            lambda p: p.update(kind="unknown"),
            lambda p: p.update(lines=[]),
            lambda p: p.update(source_invoice_id=str(self.doc_id)),
        )
        for index,mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.doc_id=uuid4()
                payload=deepcopy(self.payload)
                mutate(payload)
                with self.connect() as conn:
                    self.prepare(conn,payload=payload)
                    self.approve(conn)
                    with self.assertRaises((PermissionError,AccountingError,
                                            ValueError,self.pg.Error)):
                        self.execute(conn)
                self.assertEqual(self.count("one_invoice_docs","id"),0)
                self.assertEqual(self.count("one_journal_entries","id"),0)
                self.assertEqual(self.count("one_finance_requests","request_id"),0)

    def test_real_payment_source_is_disabled_not_simulated(self):
        with self.connect() as conn:
            self.prepare(conn)
            self.approve(conn)
            with self.assertRaisesRegex(PermissionError,"not enabled"):
                execute_verified_document_request(
                    conn,
                    identity=AuthenticatedFinanceIdentity(
                        self.actor,frozenset({self.tenant})),
                    command={"action":"apply_internal_payment",
                             "tenant":self.tenant,"document_id":self.doc_id},
                    source_loader=self.loader,
                )
        self.assertEqual(self.count("one_payment_docs","id"),0)
        self.assertEqual(self.count("one_finance_requests","request_id"),0)



    def test_real_restricted_staging_reader_catalog_revokes_write_and_checks_rls(self):
        """Read the actual PG14 catalog as a separate limited LOGIN role."""
        from one_erp_core.staging_pg_preflight import inspect_restricted_staging_reader
        login="one_ci_stage_reader_"+uuid4().hex[:12]
        password="ci_disposable_staging_reader"
        ident=self.sql.Identifier(login)
        self.root.execute(
            self.sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS"
            ).format(ident,self.sql.Literal(password))
        )
        try:
            with self.connect() as admin:
                admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                admin.execute(
                    self.sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
                admin.execute(
                    self.sql.SQL(
                        "GRANT SELECT ON public.one_memberships,"
                        " public.one_commercial_orders,"
                        " public.one_stock_balances,"
                        " public.one_return_jobs TO {}"
                    ).format(ident))
            args=dict(self.args,dbname=self.db_name,
                      user=login,password=password)
            with self.pg.connect(**args,autocommit=True) as reader:
                self.assertTrue(inspect_restricted_staging_reader(
                    reader,expected_database=self.db_name,expected_role=login))
                self.assertFalse(inspect_restricted_staging_reader(
                    reader,expected_database="wrong_database",expected_role=login))
                self.assertFalse(inspect_restricted_staging_reader(
                    reader,expected_database=self.db_name,expected_role="wrong_role"))
            with self.connect() as admin:
                admin.execute(
                    self.sql.SQL(
                        "GRANT UPDATE ON public.one_journal_entries TO {}"
                    ).format(ident)
                )
            with self.pg.connect(**args,autocommit=True) as reader:
                self.assertFalse(inspect_restricted_staging_reader(
                    reader,expected_database=self.db_name,expected_role=login))
            with self.connect() as admin:
                admin.execute(
                    self.sql.SQL(
                        "REVOKE UPDATE ON public.one_journal_entries FROM {}"
                    ).format(ident)
                )
            with self.pg.connect(**args,autocommit=True) as reader:
                self.assertTrue(inspect_restricted_staging_reader(
                    reader,expected_database=self.db_name,expected_role=login))
        finally:
            with self.connect() as admin:
                admin.execute(self.sql.SQL("DROP OWNED BY {}").format(ident))
            self.root.execute(self.sql.SQL("DROP ROLE {}").format(ident))



    def test_trusted_maker_checker_source_is_postable_with_durable_replay(self):
        from one_erp_core.invoice_source_workflow import (
            BuiltInvoiceSource, create_trusted_invoice_source,
            independently_approve_invoice_source,
        )
        class Builder:
            calls=0
            def build(builder_self,conn,*,tenant_id,document_id,actor_id):
                builder_self.calls+=1
                if tenant_id!=self.tenant or document_id!=self.doc_id or actor_id!=self.actor:
                    raise PermissionError("Source builder identity mismatch")
                return BuiltInvoiceSource(self.period_id,self.payload)
        builder=Builder()
        maker=AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        checker=AuthenticatedFinanceIdentity(self.reviewer,frozenset({self.tenant}))
        with self.connect() as conn:
            self.assertEqual(create_trusted_invoice_source(
                conn,identity=maker,tenant_id=self.tenant,
                document_id=self.doc_id,builder=builder),self.doc_id)
            self.assertEqual(builder.calls,1)
            with self.assertRaises(PermissionError):
                independently_approve_invoice_source(
                    conn,identity=maker,tenant_id=self.tenant,
                    document_id=self.doc_id)
            self.assertEqual(independently_approve_invoice_source(
                conn,identity=checker,tenant_id=self.tenant,
                document_id=self.doc_id),self.doc_id)
            with self.assertRaises(self.pg.Error):
                independently_approve_invoice_source(
                    conn,identity=checker,tenant_id=self.tenant,
                    document_id=self.doc_id)
            receipt=self.execute(conn)
            self.assertFalse(receipt.replayed)
        with self.connect() as conn:
            again=self.execute(conn)
        self.assertTrue(again.replayed)
        self.assertEqual(self.count("one_invoice_docs","id"),1)
        self.assertEqual(self.count("one_journal_entries","id"),1)
        self.assertEqual(self.count("one_finance_requests","request_id"),1)

    def test_trusted_invoice_maker_checker_fails_closed_on_permission_and_unavailable_period(self):
        from copy import deepcopy
        from one_erp_core.invoice_source_workflow import (
            BuiltInvoiceSource, create_trusted_invoice_source,
            independently_approve_invoice_source,
        )
        class Builder:
            calls=0
            def build(builder_self,*args,**kwargs):
                builder_self.calls+=1
                return BuiltInvoiceSource(self.period_id,deepcopy(self.payload))
        builder=Builder()
        valid=AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        viewer=uuid4()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO one_memberships(tenant_id,actor_id,role)
                   VALUES(%s,%s,'viewer')""",(self.tenant,viewer))
            foreign=AuthenticatedFinanceIdentity(self.actor,frozenset({uuid4()}))
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=foreign,tenant_id=self.tenant,
                    document_id=self.doc_id,builder=builder)
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        viewer,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id,builder=builder)
            with self.assertRaises(PermissionError):
                independently_approve_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        viewer,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id)
            self.assertEqual(builder.calls,0)
            # A period may only be closed through the separate audited
            # period-close permission workflow; never bypass that protection
            # merely to manufacture a test fixture.
            self.period_id=uuid4()  # unavailable accounting period
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=valid,tenant_id=self.tenant,
                    document_id=self.doc_id,builder=builder)
        self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)
        self.assertEqual(self.count("one_invoice_source_approvals","document_id"),0)

    def test_trusted_source_builder_rejects_tampered_amount_and_does_not_leave_draft(self):
        from copy import deepcopy
        from one_erp_core.invoice_source_workflow import (
            BuiltInvoiceSource,create_trusted_invoice_source,
        )
        maker=AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        for index,value in enumerate((12.5,"NaN","1e10","inf")):
            with self.subTest(index=index):
                self.doc_id=uuid4()
                fake=deepcopy(self.payload)
                fake["lines"][0]["unit_price"]=value
                class Builder:
                    def build(builder_self,*args,**kwargs):
                        return BuiltInvoiceSource(self.period_id,fake)
                with self.connect() as conn:
                    with self.assertRaises(PermissionError):
                        create_trusted_invoice_source(
                            conn,identity=maker,tenant_id=self.tenant,
                            document_id=self.doc_id,builder=Builder())
                self.assertEqual(
                    self.count("one_invoice_source_requests","document_id"),0)

    def test_approved_source_actor_revocation_prevents_new_approval(self):
        from one_erp_core.invoice_source_workflow import (
            independently_approve_invoice_source,
        )
        with self.connect() as conn:
            self.prepare(conn)
            conn.execute(
                """UPDATE one_memberships SET active=false
                   WHERE tenant_id=%s AND actor_id=%s""",
                (self.tenant,self.reviewer))
            with self.assertRaises(PermissionError):
                independently_approve_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        self.reviewer,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id)
        self.assertEqual(
            self.count("one_invoice_source_approvals","document_id"),0)



    def test_approved_invoice_signin_review_http_then_post_once_real_postgres(self):
        """HTTP approval accepts reference only, and SQL enforces different actor."""
        from io import BytesIO
        from one_erp_core.finance_wsgi import TenantFinanceConnections
        from one_erp_core.invoice_approval_wsgi import OneInvoiceApprovalWSGI
        from one_erp_core.invoice_source_workflow import (
            BuiltInvoiceSource, create_trusted_invoice_source,
        )

        class Builder:
            def build(builder_self,conn,*,tenant_id,document_id,actor_id):
                if tenant_id!=self.tenant or actor_id!=self.actor:
                    raise PermissionError("Wrong maker identity")
                return BuiltInvoiceSource(self.period_id,self.payload)

        with self.connect() as conn:
            create_trusted_invoice_source(
                conn,identity=AuthenticatedFinanceIdentity(
                    self.actor,frozenset({self.tenant})),
                tenant_id=self.tenant,document_id=self.doc_id,
                builder=Builder())

        class Verifier:
            who=self.actor
            def verify_access_token(verifier_self,token):
                if token!="header.claims.signature":
                    raise PermissionError("Expired/unknown token")
                return AuthenticatedFinanceIdentity(
                    verifier_self.who,frozenset({self.tenant}))
        verifier=Verifier()
        app=OneInvoiceApprovalWSGI(
            verifier=verifier,
            approval_connections=TenantFinanceConnections({
                self.tenant:self.connect}),
            allowed_hosts=frozenset({"staging.erp.example.net"}),
        )

        def call():
            body=json.dumps({
                "tenant":str(self.tenant),"document_id":str(self.doc_id),
            }).encode()
            status={}
            response=b"".join(app({
                "REQUEST_METHOD":"POST",
                "PATH_INFO":"/api/v1/finance/internal-invoice-approvals",
                "wsgi.url_scheme":"https",
                "HTTP_HOST":"staging.erp.example.net",
                "QUERY_STRING":"","HTTP_COOKIE":"",
                "CONTENT_TYPE":"application/json",
                "CONTENT_LENGTH":str(len(body)),
                "HTTP_AUTHORIZATION":"Bearer header.claims.signature",
                "wsgi.input":BytesIO(body),
            },lambda code,headers:status.update(code=code,headers=dict(headers))))
            return status,json.loads(response)

        maker_code,_=call()
        self.assertEqual(maker_code["code"],"403 Forbidden")
        self.assertEqual(self.count("one_invoice_source_approvals","document_id"),0)
        verifier.who=self.reviewer
        checker_code,checker_body=call()
        self.assertEqual(checker_code["code"],"200 OK")
        self.assertEqual(checker_body["status"],"approved_internal_source")
        self.assertEqual(self.count("one_invoice_source_approvals","document_id"),1)
        duplicate_code,_=call()
        self.assertEqual(duplicate_code["code"],"409 Conflict")
        self.assertEqual(self.count("one_invoice_source_approvals","document_id"),1)
        with self.connect() as conn:
            first=self.execute(conn)
        with self.connect() as conn:
            replay=self.execute(conn)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.count("one_invoice_docs","id"),1)
        self.assertEqual(self.count("one_journal_entries","id"),1)



    def prepare_real_dispatched_sale(
        self, *, dispatch_quantity="2.000", value_dispatch=True,
    ):
        """Disposable *actual purchase receipt and costed sale* fixture.

        No manually forged on-hand balance: a confirmed purchase increases
        physical stock and posts GRNI/inventory. A confirmed sale dispatch
        posts the separate COGS/inventory journal using WAC cost.
        """
        self.sales_order,self.purchase_order=uuid4(),uuid4()
        self.sales_product,self.sales_warehouse=uuid4(),uuid4()
        self.sales_admin=uuid4()
        self.sales_event=uuid4()
        self.purchase_event=uuid4()
        self.purchase_journal,self.cost_journal=uuid4(),uuid4()
        with self.connect() as conn:
            with conn.transaction():
                self.bind(conn,self.sales_admin)
                conn.execute(
                    """INSERT INTO one_memberships(tenant_id,actor_id,role)
                       VALUES(%s,%s,'company_admin')""",
                    (self.tenant,self.sales_admin),
                )
                conn.execute(
                    """INSERT INTO one_products(tenant_id,id,sku,name)
                       VALUES(%s,%s,'REAL-CI-01','Example shipped product')""",
                    (self.tenant,self.sales_product),
                )
                conn.execute(
                    """INSERT INTO one_warehouses(tenant_id,id,code,name)
                       VALUES(%s,%s,'STAGING','CI warehouse')""",
                    (self.tenant,self.sales_warehouse),
                )
                for kind,order,price,partner in (
                    ("purchase",self.purchase_order,"50.00","supplier-ci"),
                    ("sale",self.sales_order,"100.00","customer-ci"),
                ):
                    conn.execute(
                        """INSERT INTO one_commercial_orders(
                             tenant_id,id,kind,number,partner_id,currency)
                           VALUES(%s,%s,%s,%s,%s,'SAR')""",
                        (self.tenant,order,kind,
                         "REAL-"+kind+"-"+uuid4().hex[:12],partner),
                    )
                    conn.execute(
                        """INSERT INTO one_commercial_order_lines(
                             tenant_id,order_id,line_no,product_id,warehouse_id,
                             ordered_qty,unit_price)
                           VALUES(%s,%s,1,%s,%s,%s,%s)""",
                        (self.tenant,order,self.sales_product,
                         self.sales_warehouse,Decimal("10") if kind=="purchase"
                         else Decimal("5"),Decimal(price)),
                    )
                    conn.execute(
                        "SELECT one_confirm_commercial_order(%s,%s,%s)",
                        (self.tenant,order,self.sales_admin),
                    )
                # Real purchase receipt + WAC stock/GRNI postings.
                conn.execute(
                    """SELECT one_receive_and_value_goods(
                       %s,%s,1,10,%s,%s,%s,'inventory','grni',%s)""",
                    (self.tenant,self.purchase_order,self.purchase_event,
                     self.purchase_journal,self.period_id,self.sales_admin),
                )
                if Decimal(dispatch_quantity)>0:
                    if value_dispatch:
                        conn.execute(
                            """SELECT one_ship_and_value_goods(
                               %s,%s,1,%s,%s,%s,%s,'inventory','cogs',%s)""",
                            (self.tenant,self.sales_order,Decimal(dispatch_quantity),
                             self.sales_event,self.cost_journal,self.period_id,
                             self.sales_admin),
                        )
                    else:
                        conn.execute(
                            """SELECT one_fulfill_commercial_line(
                               %s,%s,1,%s,%s,%s)""",
                            (self.tenant,self.sales_order,
                             Decimal(dispatch_quantity),self.sales_event,
                             self.sales_admin),
                        )

    def prepare_reviewed_sales_intent(self, *, invoice_id=None, reviewer=None):
        invoice_id=invoice_id or self.doc_id
        reviewer=reviewer or self.reviewer
        with self.connect() as conn:
            with conn.transaction():
                self.bind(conn,self.actor)
                conn.execute(
                    """INSERT INTO one_sales_invoice_intents(
                       tenant_id,invoice_id,order_id,period_id,number,
                       issued_on,due_on,receivable_account_id,payable_account_id,
                       revenue_account_id,output_vat_account_id,input_vat_account_id,
                       tax_category,tax_rate,tax_code,tax_policy_reference,
                       tax_policy_reviewed_by,requested_by)
                       VALUES (%s,%s,%s,%s,%s,'2026-10-17','2026-11-17',
                       'ar','ap','sales','outputvat','inputvat',
                       'out_of_scope',0,'NONE','INTERNAL-CI-NOT-ZATCA',%s,%s)""",
                    (self.tenant,invoice_id,self.sales_order,self.period_id,
                     "REAL-INV-"+uuid4().hex[:12],reviewer,self.actor),
                )
        return invoice_id

    def make_approved_sales_invoice(self, *, builder=None):
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        from one_erp_core.invoice_source_workflow import (
            create_trusted_invoice_source,independently_approve_invoice_source,
        )
        with self.connect() as conn:
            create_trusted_invoice_source(
                conn,identity=AuthenticatedFinanceIdentity(
                    self.actor,frozenset({self.tenant})),
                tenant_id=self.tenant,document_id=self.doc_id,
                builder=builder or PostgresDispatchedSalesInvoiceBuilder(),
            )
            independently_approve_invoice_source(
                conn,identity=AuthenticatedFinanceIdentity(
                    self.reviewer,frozenset({self.tenant})),
                tenant_id=self.tenant,document_id=self.doc_id,
            )

    def test_real_dispatched_sale_invoice_link_and_replay_are_one_transaction(self):
        self.prepare_real_dispatched_sale()
        self.prepare_reviewed_sales_intent()
        self.make_approved_sales_invoice()
        with self.connect() as conn:
            first=self.execute(conn)
            self.assertFalse(first.replayed)
        with self.connect() as conn:
            replay=self.execute(conn)
            self.assertTrue(replay.replayed)
        self.assertEqual(self.count("one_invoice_docs","id"),1)
        self.assertEqual(self.count("one_journal_entries","id"),1)
        self.assertEqual(self.count("one_finance_requests","request_id"),1)
        with self.connect() as conn:
            linkage=conn.execute(
                """SELECT a.order_id,a.order_line_no,a.billed_qty
                     FROM one_sales_invoice_allocations a
                    WHERE a.tenant_id=%s AND a.invoice_id=%s""",
                (self.tenant,self.doc_id),
            ).fetchall()
            doc=conn.execute(
                """SELECT gross_total,status FROM one_invoice_docs
                   WHERE tenant_id=%s AND id=%s""",
                (self.tenant,self.doc_id),
            ).fetchone()
            self.assertEqual(linkage,[
                (self.sales_order,1,Decimal("2.000000"))])
            self.assertEqual(doc, (Decimal("200.000"),"issued"))
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM one_sales_invoice_allocations
                   WHERE tenant_id=%s AND invoice_id=%s""",
                (self.tenant,self.doc_id),
            ).fetchone()[0],1)

    def test_full_purchase_receipt_grni_dispatch_cogs_and_invoice_journal(self):
        self.prepare_real_dispatched_sale()
        self.prepare_reviewed_sales_intent()
        self.make_approved_sales_invoice()
        with self.connect() as conn:
            self.execute(conn)
            events=conn.execute(
                """SELECT movement_kind,qty,amount
                     FROM one_stock_value_events
                    WHERE tenant_id=%s AND stock_event_id IN (%s,%s)
                    ORDER BY movement_kind""",
                (self.tenant,self.purchase_event,self.sales_event),
            ).fetchall()
            self.assertEqual(events,[
                ("purchase_receipt",Decimal("10"),Decimal("500.000")),
                ("sale_dispatch",Decimal("2"),Decimal("100.000")),
            ])
            valuation=conn.execute(
                """SELECT qty,book_value FROM one_stock_value_positions
                   WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s""",
                (self.tenant,self.sales_warehouse,self.sales_product),
            ).fetchone()
            self.assertEqual(valuation,(Decimal("8.000"),Decimal("400.000")))
            for journal in (self.purchase_journal,self.cost_journal,self.doc_id):
                with self.subTest(journal=journal):
                    self.assertEqual(conn.execute(
                        """SELECT status FROM one_journal_entries
                           WHERE tenant_id=%s AND id=%s""",
                        (self.tenant,journal),
                    ).fetchone()[0],"posted")
            cogs=conn.execute(
                """SELECT account_id,debit,credit FROM one_journal_lines
                   WHERE tenant_id=%s AND entry_id=%s
                   ORDER BY line_number""",
                (self.tenant,self.cost_journal),
            ).fetchall()
            self.assertEqual(cogs,[
                ("cogs",Decimal("100.000"),Decimal("0")),
                ("inventory",Decimal("0"),Decimal("100.000")),
            ])
            ar=conn.execute(
                """SELECT gross_total FROM one_invoice_docs
                   WHERE tenant_id=%s AND id=%s""",
                (self.tenant,self.doc_id),
            ).fetchone()
            self.assertEqual(ar,(Decimal("200.000"),))

    def test_unvalued_sales_dispatch_blocks_invoice_at_maker(self):
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        from one_erp_core.invoice_source_workflow import create_trusted_invoice_source
        self.prepare_real_dispatched_sale(value_dispatch=False)
        self.prepare_reviewed_sales_intent()
        with self.connect() as conn:
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        self.actor,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id,
                    builder=PostgresDispatchedSalesInvoiceBuilder(),
                )
        self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)
        self.assertEqual(self.count("one_invoice_docs","id"),0)

    def test_unshipped_order_is_not_invoiced_and_no_immutable_source_created(self):
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        from one_erp_core.invoice_source_workflow import create_trusted_invoice_source
        self.prepare_real_dispatched_sale(dispatch_quantity="0")
        self.prepare_reviewed_sales_intent()
        with self.connect() as conn:
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        self.actor,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id,
                    builder=PostgresDispatchedSalesInvoiceBuilder(),
                )
        self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)
        self.assertEqual(self.count("one_invoice_docs","id"),0)

    def test_second_invoice_cannot_bill_already_dispatched_stock_twice(self):
        from one_erp_core.invoice_source_workflow import create_trusted_invoice_source
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        self.prepare_real_dispatched_sale()
        self.prepare_reviewed_sales_intent()
        self.make_approved_sales_invoice()
        with self.connect() as conn:
            self.execute(conn)
        self.doc_id=uuid4()
        self.prepare_reviewed_sales_intent()
        with self.connect() as conn:
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        self.actor,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id,
                    builder=PostgresDispatchedSalesInvoiceBuilder(),
                )
        self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)

    def test_malformed_order_line_mapping_rolls_back_invoice_journal_and_receipt(self):
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        from one_erp_core.invoice_source_workflow import BuiltInvoiceSource
        self.prepare_real_dispatched_sale()
        self.prepare_reviewed_sales_intent()
        class WrongMap:
            def build(builder_self,connection,*,tenant_id,document_id,actor_id):
                real=PostgresDispatchedSalesInvoiceBuilder().build(
                    connection,tenant_id=tenant_id,document_id=document_id,
                    actor_id=actor_id,
                )
                return BuiltInvoiceSource(
                    real.period_id,real.snapshot,real.sales_order_id,(999,),
                )
        self.make_approved_sales_invoice(builder=WrongMap())
        with self.connect() as conn:
            with self.assertRaises(self.pg.Error):
                self.execute(conn)
        for table,col in (
            ("one_invoice_docs","id"),("one_journal_entries","id"),
            ("one_finance_requests","request_id"),
            ("one_sales_invoice_allocations","invoice_id"),
        ):
            self.assertEqual(self.count(table,col),0)

    def test_revoked_sale_tax_review_prevents_source_creation(self):
        from one_erp_core.commercial_invoice_builder import PostgresDispatchedSalesInvoiceBuilder
        from one_erp_core.invoice_source_workflow import create_trusted_invoice_source
        self.prepare_real_dispatched_sale()
        self.prepare_reviewed_sales_intent()
        with self.connect() as conn:
            conn.execute(
                """UPDATE one_memberships SET active=false
                   WHERE tenant_id=%s AND actor_id=%s""",
                (self.tenant,self.reviewer))
            with self.assertRaises(PermissionError):
                create_trusted_invoice_source(
                    conn,identity=AuthenticatedFinanceIdentity(
                        self.actor,frozenset({self.tenant})),
                    tenant_id=self.tenant,document_id=self.doc_id,
                    builder=PostgresDispatchedSalesInvoiceBuilder(),
                )
        self.assertEqual(self.count("one_invoice_source_requests","document_id"),0)


if __name__=="__main__":
    unittest.main()
