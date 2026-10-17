"""Private reference-only finance posting HTTP: signed identity and source safety."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_request_replay import FinanceReplayReceipt
from one_erp_core.finance_replay_commands_wsgi import (
    OneFinanceInternalDocumentsWSGI, TrustedInvoiceSource,
    TrustedPaymentSource, parse_internal_document_request,
    InvalidReplayCommand, execute_verified_document_request,
)
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.invoicing import (
    DocumentType, InvoiceDocument, InvoiceLedgerAccounts, InvoiceLine,
)
from one_erp_core.oidc_identity import RS256VerifierConfig, VerifiedRS256FinanceAccess
from one_erp_core.payments import (
    PaymentRequest, PaymentDirection, PaymentLedgerAccounts,
)
from one_erp_core.tenancy import Membership, Role

HOST = "finance.example.test"


class FakeConn:
    def __init__(self):
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def close(self):
        self.closed = True


class Directory:
    def __init__(self, identity):
        self.identity = identity
        self.called = 0

    def resolve(self, issuer, subject):
        self.called += 1
        if subject != "staff":
            raise PermissionError("Unknown user")
        return self.identity


class Revocations:
    def __init__(self):
        self.revoked = False

    def is_revoked(self, issuer, jwt_id):
        return self.revoked


class Source:
    def __init__(self, actor, tenant, doc_id):
        self.actor, self.tenant, self.doc_id = actor, tenant, doc_id
        self.calls = []
        self.override_tenant = None
        self.member_role = Role.ACCOUNTANT

    def memberships(self):
        return (Membership(self.actor, self.tenant, self.member_role),)

    def load_invoice(self, connection, *, tenant_id, document_id, actor_id):
        self.calls.append(("invoice",tenant_id,document_id,actor_id))
        return TrustedInvoiceSource(
            document=InvoiceDocument(
                document_id,self.override_tenant or tenant_id,"CI-1","partner",
                DocumentType.CUSTOMER_INVOICE,date(2026,10,17),
                date(2026,11,17),"SAR",(
                    InvoiceLine("CI example","revenue",Decimal("1"),Decimal("100")),
                ),
            ),
            accounts=InvoiceLedgerAccounts("ar","ap","outvat","invat"),
            chart={},period=None,period_id=uuid4(),
            memberships=self.memberships(),
        )

    def load_payment(self, connection, *, tenant_id, document_id, actor_id):
        self.calls.append(("payment",tenant_id,document_id,actor_id))
        return TrustedPaymentSource(
            payment=PaymentRequest(
                document_id,self.override_tenant or tenant_id,"partner",
                date(2026,10,17),"SAR",PaymentDirection.CUSTOMER_RECEIPT,
                Decimal("100"),(),"test-reference",
            ),
            invoices=(),accounts=PaymentLedgerAccounts("bank","ar","ap"),
            chart={},period=None,period_id=uuid4(),
            memberships=self.memberships(),
        )


class ReplayPrivateHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pem=cls.key.public_key().public_bytes(
            serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")
        cls.config=RS256VerifierConfig(
            issuer="https://id.example.test",audience="one-erp-finance-api",
            signing_keys_by_id={"ci":pem})

    def setUp(self):
        self.actor,self.tenant,self.document=uuid4(),uuid4(),uuid4()
        self.identity=AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        self.dir=Directory(self.identity)
        self.revocations=Revocations()
        self.db=FakeConn()
        self.opened=0
        self.source=Source(self.actor,self.tenant,self.document)
        self.app=OneFinanceInternalDocumentsWSGI(
            verifier=VerifiedRS256FinanceAccess(
                self.config,self.dir,self.revocations),
            connections=TenantFinanceConnections({
                self.tenant:self.connection_factory}),
            source_loader=self.source,allowed_hosts=frozenset({HOST}))

    def connection_factory(self):
        self.opened+=1
        return self.db

    def token(self):
        now=int(datetime.now(timezone.utc).timestamp())
        return jwt.encode({
            "iss":self.config.issuer,"aud":self.config.audience,
            "sub":"staff","iat":now-3,"nbf":now-3,
            "exp":now+180,"jti":"private-replay-ci","token_use":"access",
        },self.key,algorithm="RS256",headers={"kid":"ci"})

    def request(self,action="issue_internal_invoice",payload=None,raw=None,
                token=None,**overrides):
        if payload is None:
            payload={"action":action,"tenant":str(self.tenant),
                     "document_id":str(self.document)}
        body=json.dumps(payload).encode("utf-8") if raw is None else raw
        env={
            "REQUEST_METHOD":"POST","wsgi.url_scheme":"https","HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/finance/internal-documents",
            "CONTENT_TYPE":"application/json","CONTENT_LENGTH":str(len(body)),
            "HTTP_AUTHORIZATION":"Bearer "+(token if token is not None else self.token()),
            "wsgi.input":BytesIO(body),"QUERY_STRING":"","HTTP_COOKIE":"",
        }
        env.update(overrides)
        result={}
        data=b"".join(self.app(env,lambda status,headers:result.update(
            status=status,headers=dict(headers))))
        return result["status"],result["headers"],json.loads(data)

    def test_invoice_and_payment_use_signed_actor_and_only_server_loaded_sources(self):
        for action, operation, target in (
            ("issue_internal_invoice","internal_invoice",
             "one_erp_core.finance_replay_commands_wsgi.issue_invoice_once"),
            ("apply_internal_payment","internal_payment",
             "one_erp_core.finance_replay_commands_wsgi.apply_internal_payment_once"),
        ):
            with self.subTest(action=action):
                with patch(target,return_value=FinanceReplayReceipt(
                    operation,self.document,False)) as executor:
                    status,headers,body=self.request(action)
                    self.assertEqual(status,"200 OK")
                    self.assertEqual(body,{"ok":True,"action":action,
                        "document_id":str(self.document),"replayed":False})
                    self.assertIn("no-store",headers["Cache-Control"])
                    self.assertEqual(executor.call_count,1)
                    self.assertEqual(executor.call_args.kwargs["actor_id"],self.actor)
                    self.assertEqual(executor.call_args.args,(self.db,))
        self.assertEqual([r[0] for r in self.source.calls],["invoice","payment"])
        self.assertEqual(self.opened,2)
        self.assertTrue(self.db.closed)

    def test_duplicate_retry_result_is_returned_without_new_identity(self):
        with patch(
            "one_erp_core.finance_replay_commands_wsgi.issue_invoice_once",
            side_effect=[
                FinanceReplayReceipt("internal_invoice",self.document,False),
                FinanceReplayReceipt("internal_invoice",self.document,True),
            ]
        ) as wrapped:
            self.assertFalse(self.request()[2]["replayed"])
            self.assertTrue(self.request()[2]["replayed"])
            self.assertEqual(wrapped.call_count,2)
        # Lower level replay/DB tests independently prove one internal write.

    def test_invalid_payload_and_header_rejected_before_verifier_and_db(self):
        valid={"action":"issue_internal_invoice","tenant":str(self.tenant),
               "document_id":str(self.document)}
        cases=[
            (dict(valid,amount=500),"400 Bad Request"),
            (dict(valid,actor_id=str(self.actor)),"400 Bad Request"),
            (dict(valid,sql="DELETE FROM one_journal_entries"),"400 Bad Request"),
            (dict(valid,connection_string="postgres://evil"),"400 Bad Request"),
            (dict(valid,document_id=str(uuid4()).upper()),"400 Bad Request"),
            (dict(valid,action="delete_journal"),"400 Bad Request"),
        ]
        for payload,expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.request(payload=payload)[0],expected)
        duplicate=(
            '{"action":"issue_internal_invoice","action":"issue_internal_invoice",'
            '"tenant":"'+str(self.tenant)+'","document_id":"'+str(self.document)+'"}'
        ).encode()
        self.assertEqual(self.request(raw=duplicate)[0],"400 Bad Request")
        for change,status in [
            ({"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"},
             "403 Forbidden"),
            ({"HTTP_HOST":"attacker.example.test"},"403 Forbidden"),
            ({"REQUEST_METHOD":"GET"},"405 Method Not Allowed"),
            ({"HTTP_COOKIE":"sid=bad"},"400 Bad Request"),
            ({"QUERY_STRING":"token=secret"},"400 Bad Request"),
            ({"CONTENT_TYPE":"text/plain"},"415 Unsupported Media Type"),
            ({"CONTENT_LENGTH":"513"},"413 Payload Too Large"),
            ({"HTTP_AUTHORIZATION":""},"401 Unauthorized"),
        ]:
            with self.subTest(change=change):
                self.assertEqual(self.request(**change)[0],status)
        self.assertEqual(self.opened,0)
        self.assertEqual(self.dir.called,0)

    def test_forged_tenant_and_revoked_token_cannot_open_connection(self):
        status,_,_=self.request(payload={
            "action":"issue_internal_invoice","tenant":str(uuid4()),
            "document_id":str(self.document)})
        self.assertEqual(status,"403 Forbidden")
        self.revocations.revoked=True
        self.assertEqual(self.request()[0],"403 Forbidden")
        self.assertEqual(self.opened,0)
        self.assertEqual(self.source.calls,[])

    def test_wrong_document_tenant_and_viewer_role_denied_before_post(self):
        self.source.override_tenant=uuid4()
        with patch(
            "one_erp_core.finance_replay_commands_wsgi.issue_invoice_once"
        ) as writer:
            self.assertEqual(self.request()[0],"403 Forbidden")
            self.assertEqual(writer.call_count,0)
        self.source.override_tenant=None
        self.source.member_role=Role.VIEWER
        with patch(
            "one_erp_core.finance_replay_commands_wsgi.issue_invoice_once"
        ) as writer:
            self.assertEqual(self.request()[0],"403 Forbidden")
            self.assertEqual(writer.call_count,0)

    def test_server_loader_error_is_generic_with_transaction_rollback(self):
        def broken(*args,**kwargs):
            raise RuntimeError("Secret database password and SQL trace")
        self.source.load_invoice=broken
        status,_,result=self.request()
        self.assertEqual(status,"409 Conflict")
        self.assertNotIn("password",str(result))
        self.assertNotIn("SQL",str(result))
        self.assertTrue(self.db.closed)
        self.assertEqual(self.db.rollbacks,1)

    def test_internal_executor_rejects_untrusted_identity_before_source(self):
        command=parse_internal_document_request(json.dumps({
            "action":"issue_internal_invoice","tenant":str(self.tenant),
            "document_id":str(self.document),
        }).encode())
        with self.assertRaises(PermissionError):
            execute_verified_document_request(
                self.db,identity=AuthenticatedFinanceIdentity(
                    self.actor,frozenset({uuid4()})),
                command=command,source_loader=self.source)
        self.assertEqual(self.source.calls,[])

    def test_unknown_json_shape_rejected(self):
        for raw in (b"[]",b"null",b"{}",b"not json",b'{"action":[]}',
                    b'{"tenant":12}',b""):
            with self.subTest(raw=raw):
                with self.assertRaises((InvalidReplayCommand,TypeError)):
                    parse_internal_document_request(raw)


if __name__=="__main__":
    unittest.main()
