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


if __name__=="__main__":
    unittest.main()
