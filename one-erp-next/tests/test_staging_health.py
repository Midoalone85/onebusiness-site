"""Non-deploying health/readiness tests and smoke-client contract.

Health readiness is a claimed probe result only. The hosting operator must
implement real checks; these unit tests never authorize production deployment.
"""
import json
from unittest import TestCase
from uuid import uuid4

from one_erp_core.staging_health import (
    OneERPStagingHealthWSGI, StagingReadiness,
)
from one_erp_core.staging_smoke import (
    StagingSmokeConfig, run_staging_smoke,
)

HOST = "staging.erp.example.net"
SHA = "b" * 40
KEYS = (
    "trusted_identity_provider", "isolated_tenant_routing",
    "restricted_database_roles", "approved_migrations",
    "restored_backup_verified", "live_https_certificate",
)


class HealthContractTests(TestCase):
    def setUp(self):
        self.good = StagingReadiness({k: True for k in KEYS})
        self.app = OneERPStagingHealthWSGI(
            allowed_hosts=frozenset((HOST,)), deployed_sha=SHA)

    def call(self, path="/healthz", app=None, **updates):
        env={
            "wsgi.url_scheme":"https", "HTTP_HOST":HOST,
            "REQUEST_METHOD":"GET", "PATH_INFO":path,
            "QUERY_STRING":"", "HTTP_COOKIE":"",
            "CONTENT_LENGTH":"", "HTTP_AUTHORIZATION":"",
        }
        env.update(updates)
        result={}
        data=b"".join((app or self.app)(
            env,lambda status,headers:result.update(
                status=status,headers=dict(headers))))
        return result, json.loads(data)

    def test_health_only_reports_literal_staging_service_and_deployed_sha(self):
        result, data=self.call()
        self.assertEqual(result["status"],"200 OK")
        self.assertEqual(data,{
            "service":"one-erp-next","environment":"staging",
            "commit_sha":SHA})
        self.assertIn("no-store",result["headers"]["Cache-Control"])
        self.assertNotIn("Authorization",result["headers"])
        self.assertNotIn("tenant_id",data)

    def test_readiness_denied_by_default(self):
        result, data=self.call("/readyz")
        self.assertEqual(result["status"],"503 Service Unavailable")
        self.assertEqual(data,{"ready":False})

    def test_readiness_requires_every_independently_verified_fact(self):
        for missing in KEYS:
            with self.subTest(missing=missing):
                row={k:True for k in KEYS}
                row[missing]=False
                app=OneERPStagingHealthWSGI(
                    allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
                    readiness_probe=lambda row=row: StagingReadiness(row))
                self.assertEqual(self.call("/readyz",app=app)[0]["status"],
                                 "503 Service Unavailable")
        app=OneERPStagingHealthWSGI(
            allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
            readiness_probe=lambda:self.good)
        self.assertEqual(self.call("/readyz",app=app),
                         (self.call("/readyz",app=app)[0],{"ready":True}))

    def test_unexpected_mapping_fields_truthy_strings_and_raw_bool_denied(self):
        for outcome in (
            StagingReadiness({**self.good.checks,"extra":"true"}),
            StagingReadiness({k:("true" if k==KEYS[0] else True) for k in KEYS}),
            StagingReadiness({k:True for k in KEYS[:-1]}),
            True, {"ready":True}, None,
        ):
            with self.subTest(outcome=outcome):
                app=OneERPStagingHealthWSGI(
                    allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
                    readiness_probe=lambda outcome=outcome:outcome)
                self.assertEqual(self.call("/readyz",app=app)[0]["status"],
                                 "503 Service Unavailable")

    def test_errors_and_secrets_from_probe_never_leak(self):
        def broken():
            raise RuntimeError("postgres://secret:password@private-db")
        app=OneERPStagingHealthWSGI(
            allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
            readiness_probe=broken)
        result,data=self.call("/readyz",app=app)
        self.assertEqual(result["status"],"503 Service Unavailable")
        self.assertEqual(data,{"ready":False})
        self.assertNotIn("password",str(result)+str(data))

    def test_https_host_authorization_and_get_only(self):
        cases=(
            ("403 Forbidden",{"wsgi.url_scheme":"http",
                 "HTTP_X_FORWARDED_PROTO":"https"}),
            ("403 Forbidden",{"HTTP_HOST":"attacker.test"}),
            ("404 Not Found",{"PATH_INFO":"/some/other/path"}),
            ("405 Method Not Allowed",{"REQUEST_METHOD":"POST"}),
            ("400 Bad Request",{"QUERY_STRING":"token=secret"}),
            ("400 Bad Request",{"HTTP_COOKIE":"session=xyz"}),
            ("400 Bad Request",{"CONTENT_LENGTH":"1"}),
            ("400 Bad Request",{"HTTP_AUTHORIZATION":"Bearer token"}),
        )
        for expected,changes in cases:
            with self.subTest(changes=changes):
                result,data=self.call(**changes)
                self.assertEqual(result["status"],expected)
                self.assertEqual(data,{"ok":False})

    def test_bad_constructor_fails_closed(self):
        for kwargs in (
            {"allowed_hosts":frozenset(),"deployed_sha":SHA},
            {"allowed_hosts":frozenset({"Attacker.test"}),"deployed_sha":SHA},
            {"allowed_hosts":frozenset({HOST}),"deployed_sha":"latest"},
            {"allowed_hosts":frozenset({HOST}),"deployed_sha":SHA,
             "readiness_probe":True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    OneERPStagingHealthWSGI(**kwargs)

    def test_smoke_agrees_with_health_and_ready_wsgi_shapes(self):
        tenant=uuid4()
        ready_app=OneERPStagingHealthWSGI(
            allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
            readiness_probe=lambda:self.good)
        def transport(host,path,headers,timeout):
            if path in ("/healthz","/readyz"):
                result,data=self.call(path,app=ready_app)
                return int(result["status"][:3]),{
                    k.lower():v for k,v in result["headers"].items()
                },json.dumps(data).encode()
            self.assertIn("Authorization",headers)
            return 200,{
                "content-type":"application/json","cache-control":"private,no-store",
            },json.dumps({
                "module":"sales","tenant_id":str(tenant),
                "source":"one_erp_next_postgresql",
                "read_only":True,"items":[],
            }).encode()
        cfg=StagingSmokeConfig(
            "https://"+HOST,HOST,SHA,tenant,
            "header.payload.signature")
        report=run_staging_smoke(cfg,transport=transport)
        self.assertEqual(report["status"],"SMOKE_PASS_NOT_PRODUCTION_APPROVAL")
        self.assertFalse(report["release_approved"])
        # Removing real readiness evidence blocks the same client.
        unready=OneERPStagingHealthWSGI(
            allowed_hosts=frozenset({HOST}),deployed_sha=SHA)
        def blocked(host,path,headers,timeout):
            if path=="/readyz":
                result,data=self.call(path,app=unready)
                return 503,{
                    "content-type":"application/json",
                    "cache-control":"private,no-store",
                },json.dumps(data).encode()
            return transport(host,path,headers,timeout)
        self.assertEqual(run_staging_smoke(cfg,transport=blocked)["status"],"BLOCKED")
