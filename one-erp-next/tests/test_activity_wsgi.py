"""Private audit API must authenticate actor/tenant and never accept caller writes."""
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.activity_wsgi import OneActivityWSGI
from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections


class FakeDB:
    def __init__(self):self.closed=False
    def close(self):self.closed=True


class Verifier:
    def __init__(self,actor,tenant):
        self.identity=AuthenticatedFinanceIdentity(actor,frozenset({tenant}))
    def verify_access_token(self,token):
        if token!="a.b.c":raise PermissionError("Access denied")
        return self.identity


class PrivateActivityHttpTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.record=uuid4(),uuid4(),uuid4()
        self.instances=[]
        def factory():
            c=FakeDB();self.instances.append(c);return c
        self.router=OneActivityWSGI(
            verifier=Verifier(self.actor,self.tenant),
            connections=TenantFinanceConnections({self.tenant:factory}),
            allowed_hosts=frozenset({"private.example.test"}))
    def call(self,**changes):
        env={
            "wsgi.url_scheme":"https","HTTP_HOST":"private.example.test",
            "PATH_INFO":"/api/v1/activity/timeline","REQUEST_METHOD":"GET",
            "CONTENT_LENGTH":"","QUERY_STRING":(
                f"tenant={self.tenant}&record_type=invoice&record_id={self.record}"
                "&limit=20&offset=0"),
            "HTTP_AUTHORIZATION":"Bearer a.b.c",
            "wsgi.input":BytesIO(b""),
        }
        env.update(changes)
        status={}
        data=b"".join(self.router(env,lambda s,h:status.update(status=s,headers=dict(h))))
        return status["status"],status["headers"],json.loads(data)

    def test_valid_signed_request_uses_verified_actor_and_private_db(self):
        body={"module":"activity","items":[]}
        with patch("one_erp_core.activity_wsgi.load_record_activity",return_value=body) as read:
            status,headers,result=self.call()
        self.assertEqual(status,"200 OK")
        self.assertEqual(result,body)
        self.assertEqual(read.call_args.kwargs["actor_id"],self.actor)
        self.assertEqual(read.call_args.kwargs["tenant_id"],self.tenant)
        self.assertEqual(read.call_args.kwargs["record_id"],self.record)
        self.assertTrue(self.instances[0].closed)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertNotIn("Access-Control-Allow-Origin",headers)

    def test_malformed_and_cross_tenant_requests_never_open_database(self):
        for env in (
            {"wsgi.url_scheme":"http"},
            {"HTTP_HOST":"outside.example.test"},
            {"REQUEST_METHOD":"POST"},
            {"HTTP_AUTHORIZATION":""},
            {"QUERY_STRING":f"tenant={uuid4()}&record_type=invoice&record_id={self.record}"},
            {"QUERY_STRING":f"tenant={self.tenant}&record_type=bad&record_id={self.record}"},
            {"QUERY_STRING":f"tenant={self.tenant}&record_type=invoice&record_id=invalid"},
            {"QUERY_STRING":f"tenant={self.tenant}&record_type=invoice&record_id={self.record}&actor_id={self.actor}"},
            {"QUERY_STRING":f"tenant={self.tenant}&record_type=invoice&record_id={self.record}&limit=999"},
        ):
            with self.subTest(env=env):
                status,headers,payload=self.call(**env)
                self.assertNotEqual(status,"200 OK")
                self.assertNotIn(str(self.actor),str(payload))
        self.assertFalse(self.instances)

    def test_permission_errors_remain_generic_and_db_closes(self):
        with patch("one_erp_core.activity_wsgi.load_record_activity",
                   side_effect=PermissionError("PRIVATE DB")):
            status,headers,body=self.call()
        self.assertEqual(status,"403 Forbidden")
        self.assertNotIn("PRIVATE DB",str(body))
        self.assertTrue(self.instances[-1].closed)



    def test_order_change_details_flag_is_explicit_and_signed(self):
        query=(f"tenant={self.tenant}&record_type=sales_order"
               f"&record_id={self.record}&include_changes=1")
        with patch("one_erp_core.activity_wsgi.load_record_activity",
                   return_value={"module":"activity","items":[]}) as read:
            status,_,_=self.call(QUERY_STRING=query)
        self.assertEqual(status,"200 OK")
        self.assertEqual(read.call_args.kwargs["include_changes"],True)
        self.assertEqual(read.call_args.kwargs["actor_id"],self.actor)
        for bad in (
            query.replace("include_changes=1","include_changes=2"),
            query.replace("record_type=sales_order","record_type=invoice"),
            query+"&include_changes=1",
        ):
            status,_,_=self.call(QUERY_STRING=bad)
            self.assertEqual(status,"400 Bad Request")


if __name__=="__main__":unittest.main()
