"""Private signed GET reviewer evidence and information-leak regression."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.invoice_review_wsgi import OneInvoiceReviewWSGI

HOST="staging.erp.example.net"
TOKEN="header.claims.signature"


class Verifier:
    def __init__(self,identity):self.identity=identity;self.calls=0
    def verify_access_token(self,token):
        self.calls+=1
        if token!=TOKEN:raise PermissionError("Secret JWT failure")
        return self.identity


class DB:
    def __init__(self):self.closed=False
    def close(self):self.closed=True


class InvoiceReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.doc,self.actor=uuid4(),uuid4(),uuid4()
        self.verifier=Verifier(AuthenticatedFinanceIdentity(
            self.actor,frozenset({self.tenant})))
        self.opened=[]
        def factory():
            db=DB();self.opened.append(db);return db
        self.app=OneInvoiceReviewWSGI(
            verifier=self.verifier,
            review_connections=TenantFinanceConnections({self.tenant:factory}),
            allowed_hosts=frozenset({HOST}),
        )

    def request(self,**kwargs):
        env={
            "wsgi.url_scheme":"https","HTTP_HOST":HOST,"SCRIPT_NAME":"",
            "PATH_INFO":"/api/v1/finance/internal-invoice-source",
            "REQUEST_METHOD":"GET",
            "QUERY_STRING":f"tenant={self.tenant}&document_id={self.doc}",
            "CONTENT_LENGTH":"","HTTP_COOKIE":"","HTTP_TRANSFER_ENCODING":"",
            "HTTP_AUTHORIZATION":"Bearer "+TOKEN,
        }
        env.update(kwargs)
        capture={}
        body=b"".join(self.app(env,lambda status,headers:
            capture.update(status=status,headers=dict(headers))))
        return capture["status"],capture["headers"],json.loads(body)

    def test_verified_review_reads_only_its_tenant_and_closes_connection(self):
        expected={"tenant":str(self.tenant),"document_id":str(self.doc),
                  "reviewable":True,"fields":[{"label":"Net","value":"100.00"}],"lines":[]}
        with patch("one_erp_core.invoice_review_wsgi.load_invoice_review_summary",
                   return_value=expected) as load:
            status,headers,body=self.request()
        self.assertEqual(status,"200 OK")
        self.assertEqual(body,expected)
        self.assertEqual(load.call_args.kwargs["actor_id"],self.actor)
        self.assertEqual(load.call_args.kwargs["tenant_id"],self.tenant)
        self.assertEqual(load.call_args.kwargs["document_id"],self.doc)
        self.assertEqual(len(self.opened),1);self.assertTrue(self.opened[0].closed)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertNotIn(TOKEN,str(body))

    def test_unauthorized_and_malformed_requests_have_no_db_lookup(self):
        cases=(
            ("403 Forbidden",{"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"}),
            ("403 Forbidden",{"HTTP_HOST":"attack.test"}),
            ("403 Forbidden",{"SCRIPT_NAME":"/admin"}),
            ("404 Not Found",{"PATH_INFO":"/api/v1/finance/internal-invoice-source/1"}),
            ("405 Method Not Allowed",{"REQUEST_METHOD":"POST"}),
            ("400 Bad Request",{"QUERY_STRING":"tenant=zzz"}),
            ("400 Bad Request",{"QUERY_STRING":f"tenant={self.tenant}&document_id={self.doc}&role=admin"}),
            ("400 Bad Request",{"QUERY_STRING":f"tenant={self.tenant}&tenant={self.tenant}&document_id={self.doc}"}),
            ("400 Bad Request",{"QUERY_STRING":f"tenant={self.tenant}&document_id={str(self.doc).upper()}"}),
            ("400 Bad Request",{"HTTP_COOKIE":"session=bad"}),
            ("400 Bad Request",{"CONTENT_LENGTH":"1"}),
            ("400 Bad Request",{"HTTP_TRANSFER_ENCODING":"chunked"}),
            ("401 Unauthorized",{"HTTP_AUTHORIZATION":""}),
            ("401 Unauthorized",{"HTTP_AUTHORIZATION":"Bearer INVALID"}),
        )
        for expected,updates in cases:
            with self.subTest(updates=updates):
                self.assertEqual(self.request(**updates)[0],expected)
        self.assertEqual(self.verifier.calls,0)
        self.assertFalse(self.opened)
        self.verifier.identity=AuthenticatedFinanceIdentity(
            self.actor,frozenset({uuid4()}))
        self.assertEqual(self.request()[0],"403 Forbidden")
        self.assertFalse(self.opened)

    def test_never_disclose_sql_secrets_or_wrong_source(self):
        samples=(
            (PermissionError("customer number 123"),"403 Forbidden"),
            (RuntimeError("postgres://root:secret"),"503 Service Unavailable"),
        )
        for error,status in samples:
            with patch("one_erp_core.invoice_review_wsgi.load_invoice_review_summary",
                       side_effect=error):
                result,_,body=self.request()
                self.assertEqual(result,status)
                self.assertNotIn("secret",str(body))
                self.assertNotIn("123",str(body))
                self.assertTrue(self.opened[-1].closed)
        with patch("one_erp_core.invoice_review_wsgi.load_invoice_review_summary",
                   return_value={"tenant":str(uuid4()),
                                 "document_id":str(self.doc),"reviewable":True}):
            self.assertEqual(self.request()[0],"403 Forbidden")

    def test_no_unpinned_reader_registry(self):
        with self.assertRaises(ValueError):
            OneInvoiceReviewWSGI(verifier=self.verifier,
                review_connections=None,allowed_hosts=frozenset({HOST}))


if __name__=="__main__":
    unittest.main()
