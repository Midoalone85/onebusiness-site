"""WSGI security boundary tests: deny bad routes before DB access."""
import unittest
from dataclasses import dataclass
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_wsgi import (
    OneFinanceWSGI, TenantFinanceConnections,
)
from one_erp_core.finance_http_boundary import (
    AuthenticatedFinanceIdentity, PrivateFinanceResponse,
)


TOKEN="eyJhbGciOiJSUzI1NiJ9.e30.signature"
HOST="finance.example.test"


class FakeVerifiedToken:
    def __init__(self, identity):
        self.identity=identity
        self.calls=0

    def verify_access_token(self, raw_token):
        self.calls+=1
        if raw_token != TOKEN:
            raise ValueError("Invalid signature")
        return self.identity


class FakeConnection:
    def __init__(self):
        self.closed=False

    def close(self):
        self.closed=True


class WSGISecurityTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.connection=FakeConnection()
        self.opened=0
        def factory():
            self.opened+=1
            return self.connection
        self.registry=TenantFinanceConnections({self.tenant:factory})
        self.verifier=FakeVerifiedToken(
            AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        )
        self.app=OneFinanceWSGI(
            verifier=self.verifier,connections=self.registry,
            allowed_hosts=frozenset({HOST}),
        )
        self.query=(
            f"tenant={self.tenant}&from=2026-10-01&as_of=2026-10-15&"
            "currency=SAR&locale=ar"
        )

    def call(self, **changes):
        env={
            "REQUEST_METHOD":"GET",
            "wsgi.url_scheme":"https",
            "HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/finance/dashboard",
            "QUERY_STRING":self.query,
            "HTTP_AUTHORIZATION":"Bearer "+TOKEN,
            "CONTENT_LENGTH":"",
        }
        env.update(changes)
        record={}
        def started(status,headers):
            record["status"]=status
            record["headers"]=dict(headers)
        data=b"".join(self.app(env,started))
        return record["status"],record["headers"],data.decode("utf-8")

    def test_successful_authenticated_dashboard_uses_exact_tenant_database(self):
        with patch("one_erp_core.finance_wsgi.render_private_dashboard_response") as render:
            render.return_value=PrivateFinanceResponse(
                200,
                {"Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'"},
                "<html><body>ONE ERP report</body></html>",
            )
            status,headers,body=self.call()
            self.assertEqual(status,"200 OK")
            self.assertEqual(self.opened,1)
            self.assertTrue(self.connection.closed)
            self.assertEqual(self.verifier.calls,1)
            self.assertIn("no-store",headers["Cache-Control"])
            self.assertIn("Authorization",headers["Vary"])
            self.assertIn("application", "application")
            self.assertEqual(headers["Content-Type"],"text/html; charset=utf-8")
            self.assertIn("ONE ERP report",body)
            self.assertNotIn(TOKEN,body)
            self.assertEqual(
                render.call_args.kwargs["requested_tenant"],self.tenant
            )
            self.assertEqual(
                render.call_args.kwargs["from_date"],date(2026,10,1)
            )

    def test_reject_http_even_if_forwarded_proto_spoofed(self):
        status,_,_=self.call(**{
            "wsgi.url_scheme":"http",
            "HTTP_X_FORWARDED_PROTO":"https",
        })
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)
        self.assertEqual(self.verifier.calls,0)

    def test_reject_unknown_host_method_path_and_body(self):
        for changes,expected in (
            ({"HTTP_HOST":"evil.example.test"},"403 Forbidden"),
            ({"REQUEST_METHOD":"POST"},"405 Method Not Allowed"),
            ({"PATH_INFO":"/other"},"404 Not Found"),
            ({"CONTENT_LENGTH":"42"},"400 Bad Request"),
        ):
            with self.subTest(changes=changes):
                status,h,body=self.call(**changes)
                self.assertEqual(status,expected)
                self.assertIn("no-store",h["Cache-Control"])
                self.assertEqual(self.opened,0)

    def test_no_query_token_duplicates_or_invalid_date(self):
        for value in (
            self.query+"&token=secret",
            self.query+"&tenant="+str(self.tenant),
            self.query.replace("as_of=2026-10-15","as_of=2026-15-50"),
            self.query.replace("from=2026-10-01","from=2026-11-01"),
            self.query.replace("currency=SAR","currency=SR"),
            self.query.replace("locale=ar","locale=xx"),
            "tenant="+str(self.tenant),
        ):
            with self.subTest(query=value):
                status,_,body=self.call(QUERY_STRING=value)
                self.assertEqual(status,"400 Bad Request")
                self.assertEqual(self.opened,0)

    def test_reject_bad_or_missing_bearer_credentials(self):
        for header in ("","Bearer incorrect","Basic abc","Bearer "+TOKEN+" "+TOKEN):
            with self.subTest(auth=header):
                status,_,body=self.call(HTTP_AUTHORIZATION=header)
                self.assertEqual(status,"401 Unauthorized")
                self.assertEqual(self.opened,0)

    def test_reject_cross_tenant_even_with_valid_token(self):
        foreign=uuid4()
        target_query=self.query.replace(str(self.tenant),str(foreign))
        status,h,body=self.call(QUERY_STRING=target_query)
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)

    def test_registered_tenant_without_verifier_grant_denied(self):
        verifier=FakeVerifiedToken(
            AuthenticatedFinanceIdentity(self.actor,frozenset({uuid4()}))
        )
        app=OneFinanceWSGI(
            verifier=verifier,connections=self.registry,
            allowed_hosts=frozenset({HOST})
        )
        status={}
        list(app({
            "REQUEST_METHOD":"GET","wsgi.url_scheme":"https","HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/finance/dashboard",
            "QUERY_STRING":self.query,"HTTP_AUTHORIZATION":"Bearer "+TOKEN,
            "CONTENT_LENGTH":"",
        },lambda line,headers:status.update(value=line)))
        self.assertEqual(status["value"],"403 Forbidden")
        self.assertEqual(self.opened,0)

    def test_missing_tenant_connection_fails_without_db_fallback(self):
        second=uuid4()
        verifier=FakeVerifiedToken(
            AuthenticatedFinanceIdentity(self.actor,frozenset({second}))
        )
        app=OneFinanceWSGI(
            verifier=verifier,connections=self.registry,
            allowed_hosts=frozenset({HOST})
        )
        record={}
        body=b"".join(app({
            "REQUEST_METHOD":"GET","wsgi.url_scheme":"https",
            "HTTP_HOST":HOST,"PATH_INFO":"/api/v1/finance/dashboard",
            "QUERY_STRING":self.query.replace(str(self.tenant),str(second)),
            "HTTP_AUTHORIZATION":"Bearer "+TOKEN,"CONTENT_LENGTH":"",
        },lambda code,headers:record.update(code=code)))
        self.assertEqual(record["code"],"503 Service Unavailable")
        self.assertNotIn(str(second),body.decode("utf-8"))
        self.assertEqual(self.opened,0)

    def test_database_failure_returns_generic_error_and_closes(self):
        with patch("one_erp_core.finance_wsgi.render_private_dashboard_response",
                   side_effect=RuntimeError("Secret connection string")):
            status,headers,body=self.call()
        self.assertEqual(status,"503 Service Unavailable")
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertNotIn("Secret",body)
        self.assertTrue(self.connection.closed)


if __name__=="__main__":
    unittest.main()
