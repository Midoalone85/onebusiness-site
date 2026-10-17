"""Signed invoice approval endpoint checks; not a deployed route."""
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.invoice_approval_wsgi import (
    OneInvoiceApprovalWSGI,parse_invoice_approval_command,
)

HOST="staging.erp.example.net"
TOKEN="header.claims.signature"


class Verifier:
    def __init__(self,identity):
        self.identity=identity
        self.calls=0
        self.refuse=False

    def verify_access_token(self,token):
        self.calls+=1
        if self.refuse or token!=TOKEN:
            raise PermissionError("Untrusted reviewer")
        return self.identity


class Connection:
    def __init__(self):
        self.closed=False
    def close(self):
        self.closed=True


class InvoiceApprovalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.doc,self.actor=uuid4(),uuid4(),uuid4()
        self.verifier=Verifier(AuthenticatedFinanceIdentity(
            self.actor,frozenset({self.tenant})))
        self.opened=0
        self.connections=[]
        def factory():
            self.opened+=1
            conn=Connection()
            self.connections.append(conn)
            return conn
        self.app=OneInvoiceApprovalWSGI(
            verifier=self.verifier,
            approval_connections=TenantFinanceConnections({self.tenant:factory}),
            allowed_hosts=frozenset({HOST}),
        )

    def make_request(self, *, body=None, **updates):
        if body is None:
            body=json.dumps({"tenant":str(self.tenant),
                             "document_id":str(self.doc)}).encode("utf-8")
        env={
            "wsgi.url_scheme":"https","HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/finance/internal-invoice-approvals",
            "REQUEST_METHOD":"POST","QUERY_STRING":"",
            "HTTP_COOKIE":"","HTTP_TRANSFER_ENCODING":"",
            "CONTENT_TYPE":"application/json",
            "CONTENT_LENGTH":str(len(body)),
            "HTTP_AUTHORIZATION":"Bearer "+TOKEN,
            "wsgi.input":BytesIO(body),
        }
        env.update(updates)
        capture={}
        content=b"".join(self.app(env,lambda status,headers:
            capture.update(status=status,headers=dict(headers))))
        return capture["status"],capture["headers"],json.loads(content)

    def test_correct_reviewer_only_passes_reference_and_closes_connection(self):
        with patch("one_erp_core.invoice_approval_wsgi.independently_approve_invoice_source",
                   return_value=self.doc) as fn:
            status,headers,body=self.make_request()
        self.assertEqual(status,"200 OK")
        self.assertTrue(body["ok"])
        self.assertEqual(body["document_id"],str(self.doc))
        self.assertEqual(body["status"],"approved_internal_source")
        self.assertEqual(fn.call_args.kwargs["identity"].actor_id,self.actor)
        self.assertEqual(fn.call_args.kwargs["tenant_id"],self.tenant)
        self.assertEqual(fn.call_args.kwargs["document_id"],self.doc)
        self.assertEqual(self.opened,1)
        self.assertTrue(self.connections[0].closed)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertNotIn(TOKEN,str(body))

    def test_unsafe_body_never_contacts_auth_or_database(self):
        samples=(
            b"{}",b"[]",b"{bad}",b"",
            b'{"tenant":"'+str(self.tenant).encode()+b'","document_id":null}',
            json.dumps({"tenant":str(self.tenant),"document_id":str(self.doc),
                        "amount":"999999.00"}).encode(),
            (b'{"tenant":"'+str(self.tenant).encode()+b'",'
             b'"tenant":"'+str(self.tenant).encode()+b'",'
             b'"document_id":"'+str(self.doc).encode()+b'"}'),
            json.dumps({"tenant":str(uuid4()),"document_id":str(uuid4()).upper()}).encode(),
            b'{"tenant":"00000000-0000-0000-0000-000000000000",'
            b'"document_id":"'+str(self.doc).encode()+b'"}',
        )
        for body in samples:
            with self.subTest(body=body[:30]):
                status,_,_=self.make_request(body=body)
                self.assertEqual(status,"400 Bad Request")
        self.assertEqual(self.opened,0)
        self.assertEqual(self.verifier.calls,0)

    def test_server_enforces_https_host_method_no_query_cookie_or_chunking(self):
        cases=(
            ("403 Forbidden",{"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"}),
            ("403 Forbidden",{"HTTP_HOST":"attacker.test"}),
            ("404 Not Found",{"PATH_INFO":"/api/v1/finance/internal-invoice-approvals/1"}),
            ("405 Method Not Allowed",{"REQUEST_METHOD":"GET"}),
            ("400 Bad Request",{"QUERY_STRING":"actor=attacker"}),
            ("400 Bad Request",{"HTTP_COOKIE":"auth=yes"}),
            ("400 Bad Request",{"HTTP_TRANSFER_ENCODING":"chunked"}),
            ("400 Bad Request",{"CONTENT_TYPE":"application/x-www-form-urlencoded"}),
            ("400 Bad Request",{"CONTENT_LENGTH":"not-a-number"}),
            ("400 Bad Request",{"CONTENT_LENGTH":"513"}),
            ("400 Bad Request",{"CONTENT_LENGTH":"3"}),
        )
        for status,changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.make_request(**changes)[0],status)
        self.assertEqual(self.opened,0)
        self.assertEqual(self.verifier.calls,0)

    def test_unsigned_or_cross_company_reviewer_denied_before_database(self):
        for auth in ("","Basic alice","Bearer malformed"):
            self.assertEqual(self.make_request(
                HTTP_AUTHORIZATION=auth)[0],"401 Unauthorized")
        self.verifier.refuse=True
        self.assertEqual(self.make_request()[0],"403 Forbidden")
        self.verifier.refuse=False
        self.verifier.identity=AuthenticatedFinanceIdentity(
            self.actor,frozenset({uuid4()}))
        self.assertEqual(self.make_request()[0],"403 Forbidden")
        self.assertEqual(self.opened,0)

    def test_untrusted_or_duplicate_source_refused_without_information_leak(self):
        with patch("one_erp_core.invoice_approval_wsgi.independently_approve_invoice_source",
                   side_effect=PermissionError("private customer")):
            status,_,body=self.make_request()
        self.assertEqual(status,"403 Forbidden")
        self.assertNotIn("private customer",str(body))
        self.assertTrue(self.connections[-1].closed)
        with patch("one_erp_core.invoice_approval_wsgi.independently_approve_invoice_source",
                   side_effect=RuntimeError("postgres://secret")):
            status,_,body=self.make_request()
        self.assertEqual(status,"409 Conflict")
        self.assertNotIn("secret",str(body))
        self.assertTrue(self.connections[-1].closed)

    def test_invalid_ctor_does_not_accept_unpinned_connections(self):
        for mapping in (None,object()):
            with self.assertRaises(ValueError):
                OneInvoiceApprovalWSGI(
                    verifier=self.verifier,approval_connections=mapping,
                    allowed_hosts=frozenset({HOST}))

if __name__=="__main__":
    unittest.main()
