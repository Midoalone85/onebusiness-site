"""Two physically separate PostgreSQL tenant databases in disposable CI ONLY.

The suite creates and drops two randomly named databases on the ephemeral
PostgreSQL CI service. It refuses to run without CI=true AND a test-only
database name, to avoid touching an external/production PostgreSQL cluster.
This is a basic isolation acceptance check, not a security certification.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from uuid import uuid4

DSN = os.environ.get("ONE_TEST_PG_DSN","")


@unittest.skipUnless(
    os.environ.get("CI")=="true" and "dbname=one_erp_test" in DSN,
    "Dedicated DB tests require disposable GitHub CI PostgreSQL",
)
class IndependentCompanyDatabasesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.pg=psycopg
        cls.sql=sql
        cls.admin=psycopg.connect(DSN,autocommit=True)
        cls.conn_args=psycopg.conninfo.conninfo_to_dict(DSN)
        if cls.conn_args.get("dbname")!="one_erp_test":
            raise RuntimeError("Refusing to create tenant DBs outside one_erp_test CI")
        suffix=uuid4().hex[:10]
        cls.db_names=[f"one_erp_ci_alpha_{suffix}",f"one_erp_ci_beta_{suffix}"]
        cls.user=f"one_erp_ci_read_{suffix}"
        cls.password="ephemeral_test_only"
        cls.tenant_ids=[uuid4(),uuid4()]
        cls.actor=uuid4()
        cls.admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(
                sql.Identifier(cls.user), sql.Literal(cls.password)
            )
        )
        for name,tenant,company in zip(
            cls.db_names,cls.tenant_ids,("Alpha CI","Beta CI")
        ):
            cls.admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
            )
            settings=dict(cls.conn_args,dbname=name)
            with psycopg.connect(**settings,autocommit=True) as root:
                folder=Path(__file__).resolve().parents[1]
                for file in sorted((folder/"db").glob("[0-9][0-9][0-9]_*.sql")):
                    root.execute(file.read_text(encoding="utf-8"),prepare=False)
                root.execute(
                    (folder/"ops"/"report_reader_privileges.sql").read_text(
                        encoding="utf-8"
                    ),prepare=False,
                )
                root.execute(
                    """INSERT INTO one_tenants(id,company_name)
                       VALUES(%s,%s)""",(tenant,company),
                )
                root.execute(
                    """INSERT INTO one_memberships(tenant_id,actor_id,role)
                       VALUES(%s,%s,'viewer')""",(tenant,cls.actor),
                )
        cls.admin.execute(
            sql.SQL("GRANT one_erp_finance_report_reader TO {}").format(
                sql.Identifier(cls.user)
            )
        )

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls,"admin"):
            return
        for name in getattr(cls,"db_names",()):
            cls.admin.execute(
                cls.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    cls.sql.Identifier(name)
                )
            )
        if hasattr(cls,"user"):
            cls.admin.execute(
                cls.sql.SQL("DROP ROLE IF EXISTS {}").format(cls.sql.Identifier(cls.user))
            )
        cls.admin.close()

    def reader(self,db_name):
        return self.pg.connect(
            **dict(self.conn_args,dbname=db_name,
                   user=self.user,password=self.password),
            autocommit=True
        )

    def test_two_database_names_and_report_tenant_identity_never_mix(self):
        from one_erp_core.report_repository import load_finance_snapshot
        self.assertNotEqual(*self.db_names)
        snapshots=[]
        for name,tenant in zip(self.db_names,self.tenant_ids):
            with self.reader(name) as connection:
                self.assertEqual(
                    connection.execute("SELECT current_database()").fetchone()[0],
                    name,
                )
                snapshot=load_finance_snapshot(
                    connection,tenant_id=tenant,actor_id=self.actor,
                    as_of=__import__("datetime").date(2026,10,15),
                )
                snapshots.append(snapshot)
        self.assertEqual(snapshots[0].company_name,"Alpha CI")
        self.assertEqual(snapshots[1].company_name,"Beta CI")
        self.assertEqual(snapshots[0].tenant_id,self.tenant_ids[0])
        self.assertEqual(snapshots[1].tenant_id,self.tenant_ids[1])

    def test_wrong_database_binding_fails_closed(self):
        from datetime import date
        from one_erp_core.report_repository import load_finance_snapshot
        with self.reader(self.db_names[1]) as connection:
            with self.assertRaises(Exception):
                load_finance_snapshot(
                    connection,tenant_id=self.tenant_ids[0],
                    actor_id=self.actor,as_of=date(2026,10,15),
                )

    def test_two_private_web_routes_are_physically_isolated(self):
        from one_erp_core.finance_wsgi import OneFinanceWSGI, TenantFinanceConnections
        from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
        tenant_a,tenant_b=self.tenant_ids
        registry=TenantFinanceConnections({
            tenant_a:lambda:self.reader(self.db_names[0]),
            tenant_b:lambda:self.reader(self.db_names[1]),
        })
        class CIOnlyVerifier:
            def verify_access_token(_,token):
                if token!="header.payload.signature":
                    raise PermissionError("CI invalid token")
                return AuthenticatedFinanceIdentity(
                    self.actor,frozenset(self.tenant_ids)
                )
        app=OneFinanceWSGI(
            verifier=CIOnlyVerifier(),connections=registry,
            allowed_hosts=frozenset({"finance.example.test"}),
        )
        def request(tenant):
            result={}
            body=b"".join(app({
                "REQUEST_METHOD":"GET","wsgi.url_scheme":"https",
                "HTTP_HOST":"finance.example.test",
                "PATH_INFO":"/api/v1/finance/dashboard",
                "QUERY_STRING":(
                    f"tenant={tenant}&from=2026-10-01"
                    "&as_of=2026-10-15&currency=SAR&locale=en"
                ),
                "HTTP_AUTHORIZATION":"Bearer header.payload.signature",
                "CONTENT_LENGTH":"",
            },lambda code,headers:result.update(status=code,headers=dict(headers))))
            return result,body.decode("utf-8")
        a,body_a=request(tenant_a)
        b,body_b=request(tenant_b)
        self.assertEqual(a["status"],"200 OK")
        self.assertEqual(b["status"],"200 OK")
        self.assertIn("Alpha CI",body_a)
        self.assertNotIn("Beta CI",body_a)
        self.assertIn("Beta CI",body_b)
        self.assertNotIn("Alpha CI",body_b)
        self.assertIn("no-store",a["headers"]["Cache-Control"])
        self.assertIn("no-store",b["headers"]["Cache-Control"])


if __name__=="__main__":
    unittest.main()
