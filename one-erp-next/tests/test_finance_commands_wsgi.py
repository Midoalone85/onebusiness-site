"""052: signed-user finance command API regression and forgery tests.

All public keys and tokens here are generated in memory for TESTS only.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import unittest
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from one_erp_core.finance_commands_wsgi import (
    OneFinanceCommandsWSGI, parse_finance_command, InvalidCommand,
    execute_verified_finance_command,
)
from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.oidc_identity import (
    RS256VerifierConfig, VerifiedRS256FinanceAccess,
)

HOST = "finance.example.test"


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, args):
        self.query = query
        self.connection.calls.append((query, args))
        if self.connection.fail:
            raise RuntimeError("PRIVATE SQL password and database details")
        if query.startswith("SELECT set_config("):
            return
        if "SELECT 1 FROM one_soft_adjustment_requests" in query:
            return

    def fetchone(self):
        if "SELECT 1 FROM one_soft_adjustment_requests" in self.query:
            return (1,) if self.connection.can_post else None
        if self.query.startswith("SELECT one_"):
            query_args = self.connection.calls[-1][1]
            # The returned ID is the domain function's source identifier.
            return (query_args[1],)
        return None


class FakeConn:
    def __init__(self, *, fail=False, can_post=True):
        self.closed = False
        self.fail = fail
        self.can_post = can_post
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    @contextmanager
    def transaction(self):
        try:
            yield self
        except Exception:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def close(self):
        self.closed = True


class VerifiedDirectory:
    def __init__(self, identity):
        self.identity = identity
        self.calls = 0

    def resolve(self, issuer, subject):
        self.calls += 1
        if subject != "session-user":
            raise PermissionError("Unknown subject")
        return self.identity


class Revocations:
    def __init__(self):
        self.revoked = False

    def is_revoked(self, issuer, jwt_id):
        return self.revoked


class FinanceCommandsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.pem = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def setUp(self):
        self.actor, self.tenant = uuid4(), uuid4()
        self.db = FakeConn()
        self.opened = 0
        self.dir = VerifiedDirectory(
            AuthenticatedFinanceIdentity(
                self.actor, frozenset({self.tenant})
            )
        )
        self.rev = Revocations()
        self.config = RS256VerifierConfig(
            issuer="https://identity.example.test",
            audience="one-erp-finance-api",
            signing_keys_by_id={"finance-test":self.pem},
        )
        self.verifier = VerifiedRS256FinanceAccess(
            self.config, self.dir, self.rev
        )
        self.app = OneFinanceCommandsWSGI(
            verifier=self.verifier,
            connections=TenantFinanceConnections({
                self.tenant: self.connection_factory
            }),
            allowed_hosts=frozenset({HOST}),
        )
        self.period = uuid4()

    def connection_factory(self):
        self.opened += 1
        return self.db

    def token(self, *, signed_by=None, claims=None):
        now = int(datetime.now(timezone.utc).timestamp())
        claims = claims or {
            "iss":self.config.issuer,"aud":self.config.audience,
            "sub":"session-user","iat":now-4,"nbf":now-4,
            "exp":now+180,"jti":"052-authenticated-ci","token_use":"access",
        }
        return jwt.encode(claims, signed_by or self.key,
                          algorithm="RS256", headers={"kid":"finance-test"})

    def call(self, payload=None, *, raw=None, token=None, **changes):
        if payload is None:
            payload = {"action":"request_soft_close",
                       "tenant":str(self.tenant),
                       "period_id":str(self.period)}
        body = (json.dumps(payload).encode("utf-8")
                if raw is None else raw)
        env = {
            "REQUEST_METHOD":"POST",
            "wsgi.url_scheme":"https",
            "HTTP_HOST":HOST,
            "PATH_INFO":"/api/v1/finance/commands",
            "QUERY_STRING":"",
            "HTTP_COOKIE":"",
            "HTTP_AUTHORIZATION":"Bearer "+(
                token if token is not None else self.token()
            ),
            "CONTENT_TYPE":"application/json",
            "CONTENT_LENGTH":str(len(body)),
            "wsgi.input":BytesIO(body),
        }
        env.update(changes)
        started = {}
        def response(status, headers):
            started["status"] = status
            started["headers"] = dict(headers)
        raw_response = b"".join(self.app(env, response))
        return started["status"],started["headers"],json.loads(raw_response)

    def test_success_verified_actor_sets_local_database_context(self):
        status,headers,data=self.call()
        self.assertEqual(status,"200 OK")
        self.assertTrue(data["ok"])
        self.assertEqual(data["id"],str(self.period))
        self.assertEqual(self.opened,1)
        self.assertTrue(self.db.closed)
        self.assertEqual(self.db.commits,1)
        self.assertEqual(self.db.rollbacks,0)
        self.assertEqual(self.dir.calls,1)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertEqual(self.db.calls[:2],[
            ("SELECT set_config('one.tenant_id',%s,true)",
             (str(self.tenant),)),
            ("SELECT set_config('one.actor_id',%s,true)",
             (str(self.actor),)),
        ])
        self.assertEqual(self.db.calls[2],(
            "SELECT one_request_period_soft_close(%s,%s,%s)",
            (self.tenant,self.period,self.actor),
        ))

    def test_unknown_fields_duplicate_actor_role_or_sql_rejected_before_auth(self):
        extra = {"action":"request_soft_close","tenant":str(self.tenant),
                 "period_id":str(self.period)}
        for key,value in (
            ("actor_id",str(uuid4())),
            ("role","company_admin"),("sql","DROP TABLE everything"),
            ("connection_string","postgresql://evil"),
            ("permission","finance.period.hard_close"),
        ):
            with self.subTest(key=key):
                status,_,_=self.call(dict(extra,**{key:value}))
                self.assertEqual(status,"400 Bad Request")
                self.assertEqual(self.opened,0)
                self.assertEqual(self.dir.calls,0)
        dup = (
            '{"action":"request_soft_close","tenant":"' +
            str(self.tenant) + '","period_id":"' + str(self.period) +
            '","action":"request_soft_close"}'
        ).encode("utf-8")
        status,_,_=self.call(raw=dup)
        self.assertEqual(status,"400 Bad Request")
        self.assertEqual(self.dir.calls,0)

    def test_untrusted_token_tenant_claim_cannot_authorize_other_company(self):
        other = uuid4()
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "iss":self.config.issuer,"aud":self.config.audience,
            "sub":"session-user","iat":now-1,"nbf":now-1,
            "exp":now+90,"jti":"052-tenant-ci","token_use":"access",
            "tenant_ids":[str(other)],
        }
        payload={"action":"request_soft_close","tenant":str(other),
                 "period_id":str(self.period)}
        status,_,_=self.call(payload,token=self.token(claims=claims))
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.dir.calls,1)
        self.assertEqual(self.opened,0)

    def test_signature_revocation_and_expiry_block_db_access(self):
        untrusted=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        status,_,_=self.call(token=self.token(signed_by=untrusted))
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)
        self.rev.revoked=True
        status,_,_=self.call()
        self.assertEqual(status,"403 Forbidden")
        self.rev.revoked=False
        now=int(datetime.now(timezone.utc).timestamp())
        expired={
            "iss":self.config.issuer,"aud":self.config.audience,
            "sub":"session-user","iat":now-300,"nbf":now-300,
            "exp":now-120,"jti":"expired-ci","token_use":"access",
        }
        status,_,_=self.call(token=self.token(claims=expired))
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.opened,0)

    def test_unapproved_posting_checks_source_and_rolls_back(self):
        self.db.can_post=False
        journal=uuid4()
        status,_,_=self.call({
            "action":"post_soft_adjustment","tenant":str(self.tenant),
            "journal_id":str(journal),
        })
        self.assertEqual(status,"403 Forbidden")
        self.assertEqual(self.db.commits,0)
        self.assertEqual(self.db.rollbacks,1)
        self.assertTrue(self.db.closed)
        self.assertEqual(len(self.db.calls),3)
        self.assertEqual(self.db.calls[2][1],
                         (self.tenant,journal,self.actor))

    def test_exact_decimal_json_and_parameterized_sql_for_multiline(self):
        request,journal=uuid4(),uuid4()
        payload={
            "action":"request_soft_adjustment","tenant":str(self.tenant),
            "request_id":str(request),"journal_id":str(journal),
            "period_id":str(self.period),"occurred_on":"2026-10-15",
            "currency":"SAR",
            "lines":[
                {"account_id":"safe'; DROP TABLE journals;--",
                 "debit":0.2,"credit":0},
                {"account_id":"liability","debit":0,"credit":0.2},
            ],
            "reason":"Customer requested a late documented correction",
        }
        status,_,_=self.call(payload)
        self.assertEqual(status,"200 OK")
        self.assertEqual(len(self.db.calls),3)
        statement,args=self.db.calls[2]
        self.assertEqual(args[-1],self.actor)
        self.assertEqual(args[1],request)
        self.assertEqual(args[3],journal)
        self.assertIn('"debit":0.2',args[6])
        self.assertIn('"credit":0.2',args[6])
        self.assertNotIn("DROP TABLE",statement)
        self.assertTrue(statement.endswith("(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"))

    def test_reject_malformed_http_body_without_database(self):
        payload={"action":"request_soft_close","tenant":str(self.tenant),
                 "period_id":str(self.period)}
        cases=[
            ({"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"},
             "403 Forbidden"),
            ({"HTTP_HOST":"evil.example.test"},"403 Forbidden"),
            ({"REQUEST_METHOD":"GET"},"405 Method Not Allowed"),
            ({"PATH_INFO":"/v2/untrusted"},"404 Not Found"),
            ({"CONTENT_TYPE":"text/plain"},"415 Unsupported Media Type"),
            ({"QUERY_STRING":"token=abcdef"},"400 Bad Request"),
            ({"HTTP_COOKIE":"sid=secret"},"400 Bad Request"),
            ({"HTTP_TRANSFER_ENCODING":"chunked"},"400 Bad Request"),
            ({"CONTENT_LENGTH":"-1"},"400 Bad Request"),
            ({"CONTENT_LENGTH":"99999"},"400 Bad Request"),
            ({"CONTENT_LENGTH":"500"},"400 Bad Request"),
            ({"HTTP_AUTHORIZATION":""},"401 Unauthorized"),
        ]
        for change,expected in cases:
            with self.subTest(change=change):
                status,headers,response=self.call(payload,**change)
                self.assertEqual(status,expected)
                self.assertFalse(response["ok"])
                self.assertIn("no-store",headers["Cache-Control"])
        self.assertEqual(self.opened,0)

    def test_invalid_financial_payloads_do_not_touch_database(self):
        p={
            "action":"request_soft_adjustment","tenant":str(self.tenant),
            "request_id":str(uuid4()),"journal_id":str(uuid4()),
            "period_id":str(self.period),"occurred_on":"2026-10-15",
            "currency":"SAR",
            "reason":"Reason for a documented balanced journal",
            "lines":[{"account_id":"cash","debit":10,"credit":0},
                     {"account_id":"sales","debit":0,"credit":10}],
        }
        bad=[
            dict(p, lines=p["lines"][:1]),
            dict(p, lines=[dict(p["lines"][0],debit=-10),p["lines"][1]]),
            dict(p, lines=[dict(p["lines"][0],debit="10"),p["lines"][1]]),
            dict(p, occurred_on="2026-55-55"),
            dict(p, currency="SAr"),
            dict(p, reason="tiny"),
            dict(p, lines=[dict(p["lines"][0],extra="not allowed"),
                           p["lines"][1]]),
            dict(p, lines=[dict(p["lines"][0],debit=True),p["lines"][1]]),
        ]
        for item in bad:
            with self.subTest(fields=item):
                self.assertEqual(self.call(item)[0],"400 Bad Request")
        self.assertEqual(self.opened,0)

    def test_backend_db_failure_returns_generic_conflict_without_leakage(self):
        self.db.fail=True
        status,_,body=self.call()
        self.assertEqual(status,"409 Conflict")
        self.assertFalse(body["ok"])
        self.assertNotIn("SQL",str(body))
        self.assertNotIn("password",str(body))
        self.assertEqual(self.db.rollbacks,1)
        self.assertTrue(self.db.closed)

    def test_verified_command_rejects_unentitled_tenant_without_sql(self):
        fake=FakeConn()
        item=parse_finance_command(json.dumps({
            "action":"request_soft_close",
            "tenant":str(self.tenant),"period_id":str(self.period),
        }).encode())
        invalid=AuthenticatedFinanceIdentity(uuid4(),frozenset({uuid4()}))
        with self.assertRaises(PermissionError):
            execute_verified_finance_command(
                fake,identity=invalid,command=item
            )
        self.assertEqual(fake.calls,[])


if __name__ == "__main__":
    unittest.main()
