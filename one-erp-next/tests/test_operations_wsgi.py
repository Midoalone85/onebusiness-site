"""Bounded read-only operations API authentication, routing and isolation."""
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.operations_wsgi import OneOperationsWSGI

TOKEN="eyJhbGciOiJSUzI1NiJ9.e30.signature"
HOST="ops.example.test"


class Verifier:
    def __init__(self,identity):
        self.identity=identity
        self.calls=0

    def verify_access_token(self,token):
        self.calls+=1
        if token != TOKEN:
            raise PermissionError("invalid token")
        return self.identity


class Conn:
    def __init__(self):
        self.closed=False

    def close(self):
        self.closed=True


class OperationsAPITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.conn=Conn()
        self.count=0
        def factory():
            self.count+=1
            return self.conn
        self.verify=Verifier(AuthenticatedFinanceIdentity(
            self.actor,frozenset({self.tenant})))
        self.app=OneOperationsWSGI(
            verifier=self.verify,
            connections=TenantFinanceConnections({self.tenant:factory}),
            allowed_hosts=frozenset({HOST}),
        )
        self.env={
            "REQUEST_METHOD":"GET","wsgi.url_scheme":"https",
            "HTTP_HOST":HOST,"PATH_INFO":"/api/v1/operations/sales",
            "QUERY_STRING":f"tenant={self.tenant}&page=0",
            "CONTENT_LENGTH":"","HTTP_AUTHORIZATION":"Bearer "+TOKEN,
        }

    def call(self,**changes):
        import json
        env=dict(self.env)
        env.update(changes)
        response={}
        def started(status,headers):
            response["code"]=status
            response["headers"]=dict(headers)
        body=b"".join(self.app(env,started))
        return response["code"],response["headers"],json.loads(body)

    def test_all_five_source_backed_modules_have_separate_routes(self):
        with patch("one_erp_core.operations_wsgi.read_operations_page") as read:
            for module in ("sales","purchases","inventory","returns","refunds"):
                read.return_value={"tenant_id":str(self.tenant),
                                   "module":module,"total":0,"items":[]}
                status,headers,body=self.call(
                    PATH_INFO="/api/v1/operations/"+module)
                self.assertEqual(status,"200 OK")
                self.assertEqual(body["module"],module)
                self.assertEqual(read.call_args.kwargs["actor_id"],self.actor)
                self.assertEqual(read.call_args.kwargs["page"],0)
                self.assertIn("no-store",headers["Cache-Control"])
                self.assertEqual(headers["Content-Type"],
                                 "application/json; charset=utf-8")
            self.assertEqual(self.count,5)
            self.assertTrue(self.conn.closed)

    def test_cross_company_is_denied_before_db_is_opened(self):
        foreign=uuid4()
        status,_,message=self.call(
            QUERY_STRING=f"tenant={foreign}&page=0")
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.count,0)
        self.assertNotIn(str(foreign),str(message))

    def test_strict_host_tls_path_method_and_post_rejected(self):
        for change,expected in (
            ({"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"},"403 Forbidden"),
            ({"HTTP_HOST":"other.test"},"403 Forbidden"),
            ({"PATH_INFO":"/api/v1/operations/payments"},"404 Not Found"),
            ({"PATH_INFO":"/api/v1/operations/../refunds"},"404 Not Found"),
            ({"REQUEST_METHOD":"POST"},"405 Method Not Allowed"),
            ({"CONTENT_LENGTH":"5"},"400 Bad Request"),
        ):
            with self.subTest(change=change):
                code,headers,_=self.call(**change)
                self.assertEqual(code,expected)
                self.assertEqual(self.count,0)
                self.assertIn("no-store",headers["Cache-Control"])

    def test_query_injection_duplicates_and_unbounded_pagination_rejected(self):
        for query in (
            f"tenant={self.tenant}&page=101",
            f"tenant={self.tenant}&page=-1",
            f"tenant={self.tenant}&page=1.2",
            f"tenant={self.tenant}&page=01",
            f"tenant={self.tenant}&page=0&token=secret",
            f"tenant={self.tenant}&page=0&page=1",
            f"tenant={self.tenant}",
            "tenant=bad&page=0",
            f"tenant={self.tenant}&page=0%3Bdelete",
        ):
            with self.subTest(query=query):
                code,_,_=self.call(QUERY_STRING=query)
                self.assertEqual(code,"400 Bad Request")
                self.assertEqual(self.count,0)

    def test_bearer_strictness_and_forged_identity_fail_closed(self):
        for bearer in ("","Basic abcd","Bearer abc",
                       "Bearer "+TOKEN+" extra"):
            code,_,_=self.call(HTTP_AUTHORIZATION=bearer)
            self.assertEqual(code,"401 Unauthorized")
        self.assertEqual(self.count,0)
        self.verify.identity=AuthenticatedFinanceIdentity(
            uuid4(),frozenset({uuid4()}))
        code,_,_=self.call()
        self.assertEqual(code,"403 Forbidden")
        self.assertEqual(self.count,0)

    def test_db_failure_is_private_and_connection_closed(self):
        with patch("one_erp_core.operations_wsgi.read_operations_page",
                   side_effect=RuntimeError("private db secret")):
            code,headers,body=self.call()
        self.assertEqual(code,"503 Service Unavailable")
        self.assertNotIn("private",str(body))
        self.assertTrue(self.conn.closed)
        self.assertIn("no-store",headers["Cache-Control"])

    def test_forged_tenant_result_fails_closed(self):
        with patch("one_erp_core.operations_wsgi.read_operations_page",
                   return_value={"tenant_id":str(uuid4()),
                                 "module":"sales","items":[]}) as read:
            code,_,_=self.call()
        self.assertEqual(code,"503 Service Unavailable")
        self.assertEqual(read.call_count,1)


if __name__=="__main__":
    unittest.main()
