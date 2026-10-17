"""ONE ERP NEXT 056 secure read-only operational routes, no real IdP."""
from __future__ import annotations

import json
from datetime import date
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.operations_read_wsgi import OneOperationsWSGI

HOST="one.private.example.test"
TOKEN="a.b.c"


class FakeConnection:
    def __init__(self):
        self.closed=False
    def close(self):
        self.closed=True


class FakeIdentity:
    def __init__(self, identity):
        self.identity=identity
        self.calls=0
    def verify_access_token(self, token):
        self.calls+=1
        if token!=TOKEN:
            raise PermissionError("Invalid JWT")
        return self.identity


class OperationsWSGITests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor=uuid4(),uuid4()
        self.opened=0
        self.db=FakeConnection()
        def factory():
            self.opened+=1
            return self.db
        self.verifier=FakeIdentity(
            AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant})))
        self.connections=TenantFinanceConnections({self.tenant:factory})
        self.app=OneOperationsWSGI(
            verifier=self.verifier,connections=self.connections,
            allowed_hosts=frozenset({HOST}))
        self.params=f"tenant={self.tenant}&limit=2&offset=0"

    def request(self,**overrides):
        environ={
            "REQUEST_METHOD":"GET",
            "wsgi.url_scheme":"https",
            "HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/operations/inventory",
            "QUERY_STRING":self.params,
            "HTTP_AUTHORIZATION":"Bearer "+TOKEN,
            "CONTENT_LENGTH":"",
        }
        environ.update(overrides)
        captured={}
        def reply(status,headers):
            captured["status"]=status
            captured["headers"]=dict(headers)
        raw=b"".join(self.app(environ,reply))
        return captured["status"],captured["headers"],json.loads(raw)

    def test_verified_tenant_reads_real_service_and_serializes_json(self):
        sample={
            "module":"inventory","tenant_id":str(self.tenant),
            "items":[{"sku":"س-001","on_hand":"7.000","reserved":"2.000",
                      "available":"5.000"}],
            "page":{"limit":2,"offset":0,"has_more":False,
                    "next_offset":None},"read_only":True,
            "source":"one_erp_next_postgresql",
        }
        with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                   return_value=sample) as read:
            status,headers,body=self.request()
        self.assertEqual(status,"200 OK")
        self.assertEqual(body,sample)
        self.assertEqual(self.opened,1)
        self.assertEqual(self.verifier.calls,1)
        self.assertTrue(self.db.closed)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertIn("Authorization",headers["Vary"])
        self.assertEqual(headers["Content-Type"],"application/json; charset=utf-8")
        self.assertEqual(read.call_args.kwargs["actor_id"],self.actor)
        self.assertEqual(read.call_args.kwargs["tenant_id"],self.tenant)
        self.assertEqual(read.call_args.kwargs["module"],"inventory")
        self.assertNotIn(TOKEN,json.dumps(body))

    def test_all_four_module_paths_are_allowlisted(self):
        with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                   return_value={"items":[]}) as read:
            for module in ("sales","purchases","inventory","returns"):
                status,_,_=self.request(
                    PATH_INFO=f"/api/v1/operations/{module}")
                self.assertEqual(status,"200 OK")
                self.assertEqual(read.call_args.kwargs["module"],module)
        self.assertEqual(self.opened,4)

    def test_receivables_route_requires_exact_report_date_and_signed_tenant(self):
        sample={
            "module":"receivables","tenant_id":str(self.tenant),
            "items":[],"read_only":True,"source":"one_erp_next_postgresql",
            "as_of":"2026-10-20",
        }
        query=self.params+"&as_of=2026-10-20"
        with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                   return_value=sample) as load:
            status,headers,body=self.request(
                PATH_INFO="/api/v1/operations/receivables",
                QUERY_STRING=query)
        self.assertEqual(status,"200 OK")
        self.assertEqual(body,sample)
        self.assertEqual(load.call_args.kwargs["as_of"],date(2026,10,20))
        self.assertEqual(load.call_args.kwargs["module"],"receivables")
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertTrue(self.db.closed)

    def test_receivables_rejects_invalid_asof_smuggling_and_other_tenant(self):
        for asof in (
            "","2026-10-32","20261020","2026-1-01","2026-10-20%0D",
            "2026-10-20&as_of=2026-10-18","today",
        ):
            with self.subTest(asof=asof):
                status,_,_=self.request(
                    PATH_INFO="/api/v1/operations/receivables",
                    QUERY_STRING=self.params+"&as_of="+asof)
                self.assertEqual(status,"400 Bad Request")
        self.assertEqual(self.opened,0)
        status,_,_=self.request(
            PATH_INFO="/api/v1/operations/receivables",
            QUERY_STRING=f"tenant={uuid4()}&as_of=2026-10-20")
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)
        status,_,_=self.request(
            PATH_INFO="/api/v1/operations/sales",
            QUERY_STRING=self.params+"&as_of=2026-10-20")
        self.assertEqual(status,"400 Bad Request")

    def test_auth_and_tenant_denial_before_database_connection(self):
        foreign=uuid4()
        for auth,query,expected in (
            ("","tenant="+str(self.tenant),"401 Unauthorized"),
            ("Basic abc",self.params,"401 Unauthorized"),
            ("Bearer false",self.params,"401 Unauthorized"),
            ("Bearer "+TOKEN,"tenant="+str(foreign),"403 Forbidden"),
        ):
            with self.subTest(auth=auth,tenant=query):
                status,_,payload=self.request(
                    HTTP_AUTHORIZATION=auth,QUERY_STRING=query)
                self.assertEqual(status,expected)
                self.assertIn("error",payload)
        self.assertEqual(self.opened,0)

    def test_reject_http_host_spoof_method_mutation_and_unknown_paths(self):
        cases=(
            ({"wsgi.url_scheme":"http",
              "HTTP_X_FORWARDED_PROTO":"https"},"403 Forbidden"),
            ({"HTTP_HOST":"evil.example.test"},"403 Forbidden"),
            ({"REQUEST_METHOD":"POST"},"405 Method Not Allowed"),
            ({"PATH_INFO":"/api/v1/operations/payments"},"404 Not Found"),
            ({"PATH_INFO":"/api/v1/operations/inventory/1"},"404 Not Found"),
            ({"CONTENT_LENGTH":"3"},"400 Bad Request"),
        )
        for changed,expected in cases:
            with self.subTest(changed=changed):
                status,h,_=self.request(**changed)
                self.assertEqual(status,expected)
                self.assertIn("no-store",h["Cache-Control"])
        self.assertEqual(self.opened,0)

    def test_reject_parameter_smuggling_and_excessive_pagination(self):
        bad=(
            self.params+"&token=attacker",
            self.params+"&tenant="+str(self.tenant),
            "tenant="+str(self.tenant)+"&limit=999",
            "tenant="+str(self.tenant)+"&offset=5001",
            "tenant="+str(self.tenant)+"&limit=-1",
            "tenant="+str(self.tenant)+"&offset=01",
            "tenant="+str(self.tenant)+"&limit=1.5",
            "tenant=not-a-uuid",
            "",
        )
        for query in bad:
            with self.subTest(query=query):
                status,_,_=self.request(QUERY_STRING=query)
                self.assertEqual(status,"400 Bad Request")
        self.assertEqual(self.opened,0)

    def test_role_denial_returns_403_and_always_closes_db(self):
        with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                   side_effect=PermissionError("Secret role map")):
            status,_,body=self.request()
        self.assertEqual(status,"403 Forbidden")
        self.assertNotIn("Secret",json.dumps(body))
        self.assertTrue(self.db.closed)

    def test_database_exception_is_generic_no_secrets(self):
        with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                   side_effect=RuntimeError("postgres://secret")):
            status,_,body=self.request()
        self.assertEqual(status,"503 Service Unavailable")
        self.assertNotIn("secret",json.dumps(body))
        self.assertTrue(self.db.closed)

    def test_missing_tenant_database_never_falls_back_to_other_company(self):
        other=uuid4()
        self.verifier=FakeIdentity(
            AuthenticatedFinanceIdentity(self.actor,frozenset({other})))
        self.app=OneOperationsWSGI(
            verifier=self.verifier,connections=self.connections,
            allowed_hosts=frozenset({HOST}))
        status,_,body=self.request(QUERY_STRING=f"tenant={other}")
        self.assertEqual(status,"503 Service Unavailable")
        self.assertEqual(self.opened,0)
        self.assertNotIn(str(other),json.dumps(body))

    def test_invalid_host_allowlist_denied_during_setup(self):
        for allowed in (frozenset(),frozenset({"evil.com/path"}),
                        frozenset({"*.example.test"}),
                        frozenset({"UPPER.test"})):
            with self.assertRaises(ValueError):
                OneOperationsWSGI(
                    verifier=self.verifier,connections=self.connections,
                    allowed_hosts=allowed)

    def test_denied_authenticator_never_opens_database(self):
        self.verifier.identity=AuthenticatedFinanceIdentity(
            uuid4(),frozenset())
        status,_,_=self.request()
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)


if __name__=="__main__":
    unittest.main()
