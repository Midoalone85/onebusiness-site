"""ONE ERP NEXT workbench: exact HTTPS asset and API routes, no token leaks."""
import unittest
from uuid import uuid4
from unittest.mock import patch

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.operations_portal_wsgi import OneOperationsPortalWSGI


class Identity:
    def __init__(self,tenant,actor):
        self.value=AuthenticatedFinanceIdentity(actor,frozenset({tenant}))
    def verify_access_token(self,raw_token):
        if raw_token!="abc.def.ghi":
            raise PermissionError("invalid token")
        return self.value


class Conn:
    def __init__(self):
        self.closed=False
    def close(self):
        self.closed=True


class PortalWSGITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.conn=Conn()
        self.app=OneOperationsPortalWSGI(
            verifier=Identity(self.tenant,self.actor),
            connections=TenantFinanceConnections({self.tenant:lambda:self.conn}),
            allowed_hosts=frozenset({"portal.test"}))
        self.env={
            "REQUEST_METHOD":"GET","wsgi.url_scheme":"https",
            "HTTP_HOST":"portal.test","PATH_INFO":"/app",
            "CONTENT_LENGTH":"","QUERY_STRING":"",
        }

    def call(self,**overrides):
        args=dict(self.env);args.update(overrides)
        data={}
        def start(code,headers):
            data["status"]=code
            data["headers"]=dict(headers)
        body=b"".join(self.app(args,start))
        return data["status"],data["headers"],body.decode("utf-8")

    def test_unauthenticated_shell_contains_no_financial_data(self):
        for path in ("/app","/app/","/app/sales","/app/purchases",
                     "/app/inventory","/app/returns","/app/refunds"):
            status,headers,html=self.call(PATH_INFO=path)
            self.assertEqual(status,"200 OK")
            self.assertIn("ONE ERP NEXT",html)
            self.assertIn("operations.js",html)
            self.assertIn("no-store",headers["Cache-Control"])
            self.assertIn("script-src 'self'",headers["Content-Security-Policy"])
            self.assertNotIn(str(self.tenant),html)
            self.assertNotIn("abc.def.ghi",html)
            self.assertFalse(self.conn.closed)

    def test_only_declared_assets_and_app_pages_are_served(self):
        for path,token in (
            ("/assets/operations.css",".sidebar"),
            ("/assets/operations.js","/api/v1/operations/")
        ):
            status,h,content=self.call(PATH_INFO=path)
            self.assertEqual(status,"200 OK")
            self.assertIn(token,content)
            self.assertIn("no-store",h["Cache-Control"])
        for path in ("/app/../../secrets","/assets/../.env",
                     "/app/admin","/assets/unknown.js"):
            status,_,_=self.call(PATH_INFO=path)
            self.assertEqual(status,"404 Not Found")

    def test_static_server_does_not_trust_spoofed_tls_host_or_post(self):
        for x in (
            {"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"},
            {"HTTP_HOST":"evil.test"},
            {"REQUEST_METHOD":"POST"},
            {"CONTENT_LENGTH":"10"},
            {"QUERY_STRING":"token=should-not-be-in-url"},
        ):
            status,_,body=self.call(**x)
            self.assertNotEqual(status,"200 OK")
            self.assertNotIn("JWT",body)

    def test_dispatches_authenticated_json_to_real_read_model(self):
        with patch("one_erp_core.operations_wsgi.read_operations_page") as read:
            read.return_value={"tenant_id":str(self.tenant),
                                "module":"inventory",
                                "fields":[],"total":0,"items":[]}
            status,headers,text=self.call(
                PATH_INFO="/api/v1/operations/inventory",
                QUERY_STRING=f"tenant={self.tenant}&page=0",
                HTTP_AUTHORIZATION="Bearer abc.def.ghi")
        self.assertEqual(status,"200 OK")
        self.assertIn('"module":"inventory"',text)
        self.assertEqual(read.call_args.kwargs["actor_id"],self.actor)
        self.assertEqual(read.call_args.kwargs["tenant_id"],self.tenant)
        self.assertIn("application/json",headers["Content-Type"])


if __name__=="__main__":
    unittest.main()
