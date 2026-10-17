"""Staging smoke negative-path regression. All transports are in memory.

No network, Oracle, external IdP or customer data needed for these tests.
"""
import io
import json
import os
from unittest import TestCase, mock
from uuid import uuid4

from one_erp_core.staging_smoke import (
    InvalidStagingConfiguration, StagingSmokeConfig, main, run_staging_smoke, run_full_staging_smoke,
)

HOST = "staging.erp.example.net"
SHA = "a" * 40
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJjaSJ9.signature"


class StagingSmokeTests(TestCase):
    def setUp(self):
        self.tenant = uuid4()
        self.conf = StagingSmokeConfig(
            origin=f"https://{HOST}/", allowed_host=HOST,
            commit_sha=SHA, tenant_id=self.tenant, access_token=TOKEN)
        self.calls = []
        self.health = {
            "service": "one-erp-next", "environment": "staging",
            "commit_sha": SHA}
        self.ready = {"ready": True}
        self.sales = {
            "module": "sales", "tenant_id": str(self.tenant), "items": [],
            "read_only": True, "source": "one_erp_next_postgresql",
        }

    def responder(self, host, path, headers, timeout):
        self.calls.append((host, path, dict(headers), timeout))
        if path == "/healthz":
            body = self.health
        elif path == "/readyz":
            body = self.ready
        else:
            body = self.sales
        return 200, {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "private, no-store",
        }, json.dumps(body).encode()

    def test_all_three_staging_checks_are_live_read_only_and_never_approve_release(self):
        result = run_staging_smoke(self.conf, transport=self.responder)
        self.assertEqual(result["status"], "SMOKE_PASS_NOT_PRODUCTION_APPROVAL")
        self.assertEqual(result["checks"], dict.fromkeys(
            ("health_sha", "ready", "sales_read"), True))
        self.assertFalse(result["release_approved"])
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(h == HOST for h, *_ in self.calls))
        self.assertTrue(all(p.startswith("/") for _, p, *_ in self.calls))
        self.assertTrue(all("GET" not in p for _, p, *_ in self.calls))
        self.assertTrue(all(self.tenant.hex not in p for _,p,*_ in self.calls
                            if p in ("/healthz","/readyz")))
        for _, path, hdrs, _ in self.calls:
            if path.startswith("/api/v1/operations/sales"):
                self.assertEqual(hdrs["Authorization"], "Bearer "+TOKEN)
                self.assertIn(str(self.tenant),path)
            else:
                self.assertNotIn("Authorization",hdrs)

    def test_target_host_must_be_explicit_https_and_non_private(self):
        bad = (
            ("http://"+HOST, HOST),
            ("https://"+HOST+"/finance", HOST),
            ("https://"+HOST+"?token=secret", HOST),
            ("https://person:secret@"+HOST, HOST),
            ("https://"+HOST+":8443", HOST),
            ("https://"+HOST, "other.erp.example.net"),
            ("https://localhost", "localhost"),
            ("https://127.0.0.1", "127.0.0.1"),
            ("https://127.0.0.1.nip.io", "127.0.0.1.nip.io"),
            ("https://127.0.0.1.sslip.io", "127.0.0.1.sslip.io"),
            ("https://db.internal", "db.internal"),
            ("https://bad.test", "bad.test"),
        )
        for origin,host in bad:
            with self.subTest(origin=origin):
                with self.assertRaises(InvalidStagingConfiguration):
                    StagingSmokeConfig(origin,host,SHA,self.tenant,TOKEN)

    def test_bad_jwt_sha_or_timeout_block_all_network_calls(self):
        for changes in (
            {"access_token": "not-a-jwt"},
            {"access_token": "a.b.c?tenant=evil"},
            {"commit_sha": "latest"},
            {"commit_sha": "B"*40},
            {"timeout_seconds": 1000},
            {"timeout_seconds": False},
        ):
            with self.subTest(changes=changes):
                args = dict(origin=self.conf.origin,allowed_host=HOST,
                            commit_sha=SHA,tenant_id=self.tenant,
                            access_token=TOKEN,timeout_seconds=5)
                args.update(changes)
                with self.assertRaises(InvalidStagingConfiguration):
                    StagingSmokeConfig(**args)
        self.assertEqual(self.calls, [])

    def test_wrong_commit_blocks_even_when_everything_else_responds(self):
        self.health["commit_sha"] = "b"*40
        report = run_staging_smoke(self.conf, transport=self.responder)
        self.assertEqual(report["status"],"BLOCKED")
        self.assertFalse(report["checks"]["health_sha"])
        self.assertFalse(report["release_approved"])

    def test_bearer_token_is_never_sent_if_version_or_readiness_fails(self):
        self.health["commit_sha"] = "b" * 40
        result = run_staging_smoke(self.conf,transport=self.responder)
        self.assertEqual(result["status"],"BLOCKED")
        self.assertTrue(all(
            not path.startswith("/api/v1/operations/")
            for _,path,*_ in self.calls))
        self.assertTrue(all(
            "Authorization" not in headers
            for _,_,headers,_ in self.calls))
        self.calls.clear()
        self.health["commit_sha"] = SHA
        self.ready["ready"] = False
        result = run_staging_smoke(self.conf,transport=self.responder)
        self.assertEqual(result["status"],"BLOCKED")
        self.assertTrue(all(
            not path.startswith("/api/v1/operations/")
            for _,path,*_ in self.calls))
        self.assertTrue(all(
            "Authorization" not in headers
            for _,_,headers,_ in self.calls))

    def test_wrong_environment_or_service_is_not_a_valid_staging_deployment(self):
        for field, value in (("service","legacy-odoo"),("environment","production")):
            with self.subTest(field=field):
                previous=self.health[field]
                self.health[field]=value
                self.assertEqual(run_staging_smoke(
                    self.conf,transport=self.responder)["status"],"BLOCKED")
                self.health[field]=previous

    def test_fake_sales_instead_of_tenant_postgresql_is_rejected(self):
        for field,value in (
            ("source","mock_preview"),("read_only",False),
            ("tenant_id",str(uuid4())),("items",None),
            ("module","payments"),
        ):
            with self.subTest(field=field):
                previous=self.sales[field]
                self.sales[field]=value
                report=run_staging_smoke(self.conf,transport=self.responder)
                self.assertFalse(report["checks"]["sales_read"])
                self.sales[field]=previous

    def test_200_html_wrong_mime_or_cacheable_response_is_rejected(self):
        def corrupt(host,path,headers,timeout):
            status,hdr,body=self.responder(host,path,headers,timeout)
            if path == "/healthz":
                return status, {**hdr, "content-type":"text/html"},body
            return status,hdr,body
        report=run_staging_smoke(self.conf,transport=corrupt)
        self.assertFalse(report["checks"]["health_sha"])
        def cacheable(host,path,headers,timeout):
            status,hdr,body=self.responder(host,path,headers,timeout)
            if path == "/readyz":
                hdr["cache-control"]="public,max-age=86400"
            return status,hdr,body
        self.assertFalse(run_staging_smoke(
            self.conf,transport=cacheable)["checks"]["ready"])

    def test_redirect_login_page_too_large_json_and_transport_error_fail_closed(self):
        for code in (301,302,307,308,401,403,404,429,500):
            def status_fail(host,path,headers,timeout):
                status,hdr,body=self.responder(host,path,headers,timeout)
                return (code if path=="/healthz" else status),hdr,body
            with self.subTest(code=code):
                self.assertFalse(run_staging_smoke(
                    self.conf,transport=status_fail)["checks"]["health_sha"])
        def oversized(host,path,headers,timeout):
            status,hdr,body=self.responder(host,path,headers,timeout)
            return status,hdr,b"x"*8193 if path=="/healthz" else body
        self.assertFalse(run_staging_smoke(
            self.conf,transport=oversized)["checks"]["health_sha"])
        def broken(host,path,headers,timeout):
            if path == "/readyz":
                raise OSError("sensitive SQL password secret")
            return self.responder(host,path,headers,timeout)
        report=run_staging_smoke(self.conf,transport=broken)
        self.assertEqual(report["status"],"BLOCKED")
        self.assertNotIn("password",str(report))
        self.assertNotIn("secret",str(report))

    def test_no_operator_settings_returns_not_run_without_traffic(self):
        env={
            "ONE_STAGING_ORIGIN":"", "ONE_STAGING_ALLOWED_HOST":"",
            "ONE_STAGING_COMMIT_SHA":"", "ONE_STAGING_TENANT_ID":"",
            "ONE_STAGING_ACCESS_TOKEN":"",
        }
        with mock.patch.dict(os.environ,env,clear=False):
            with mock.patch("sys.stdout",new_callable=io.StringIO) as output:
                code=main()
            self.assertEqual(code,2)
            msg=json.loads(output.getvalue())
            self.assertEqual(msg["status"],"NOT_RUN")
            self.assertFalse(msg["release_approved"])
            self.assertNotIn("access_token",str(msg).lower())

    def test_malformed_config_does_not_log_access_token_or_connect(self):
        env={
            "ONE_STAGING_ORIGIN":"http://"+HOST,
            "ONE_STAGING_ALLOWED_HOST":HOST,
            "ONE_STAGING_COMMIT_SHA":SHA,
            "ONE_STAGING_TENANT_ID":str(self.tenant),
            "ONE_STAGING_ACCESS_TOKEN":TOKEN,
        }
        with mock.patch.dict(os.environ,env,clear=False):
            with mock.patch("sys.stdout",new_callable=io.StringIO) as output:
                code=main()
            self.assertEqual(code,1)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn("BLOCKED",output.getvalue())

    def test_no_passing_config_authorizes_oracle_or_legal_release(self):
        result=run_staging_smoke(self.conf,transport=self.responder)
        self.assertFalse(result["release_approved"])
        self.assertEqual(result["scope"],"read_only_staging_http_only")



    def test_full_smoke_requires_all_four_live_modules_and_safe_contract(self):
        calls=[]
        def transport(host,path,headers,timeout):
            calls.append((host,path,dict(headers)))
            if path=="/healthz":
                payload=self.health
            elif path=="/readyz":
                payload=self.ready
            else:
                module=path.split("/api/v1/operations/")[1].split("?")[0]
                payload={
                    "module":module,"tenant_id":str(self.tenant),
                    "items":[],"read_only":True,
                    "source":"one_erp_next_postgresql",
                    "page":{"limit":1,"offset":0,"has_more":False,
                            "next_offset":None},
                }
            return 200,{"content-type":"application/json",
                        "cache-control":"private, no-store"},json.dumps(payload).encode()
        report=run_full_staging_smoke(self.conf,transport=transport)
        self.assertEqual(report["status"],"SMOKE_PASS_NOT_PRODUCTION_APPROVAL")
        self.assertEqual(len(report["checks"]),6)
        self.assertTrue(all(report["checks"].values()))
        self.assertFalse(report["release_approved"])
        self.assertEqual(len(calls),6)
        self.assertEqual([
            c[1].split("/api/v1/operations/")[-1].split("?")[0]
            for c in calls[2:]
        ],["sales","purchases","inventory","returns"])
        self.assertTrue(all("Authorization" not in headers
                            for _,path,headers in calls[:2]))
        self.assertTrue(all(headers.get("Authorization")=="Bearer "+TOKEN
                            for _,path,headers in calls[2:]))
        self.assertEqual(report["scope"],"read_only_staging_four_modules")

    def test_full_smoke_never_sends_token_before_sha_and_readiness(self):
        calls=[]
        self.health["commit_sha"]="0"*40
        def transport(host,path,headers,timeout):
            calls.append((path,dict(headers)))
            return self.responder(host,path,headers,timeout)
        report=run_full_staging_smoke(self.conf,transport=transport)
        self.assertEqual(report["status"],"BLOCKED")
        self.assertTrue(all("Authorization" not in headers
                            for _,headers in calls))
        self.assertTrue(all(not path.startswith("/api/v1/operations/")
                            for path,_ in calls))
        self.assertFalse(report["checks"]["purchases_read"])
        self.health["commit_sha"]=SHA
        self.ready["ready"]=False
        calls.clear()
        report=run_full_staging_smoke(self.conf,transport=transport)
        self.assertEqual(report["status"],"BLOCKED")
        self.assertTrue(all("Authorization" not in headers
                            for _,headers in calls))
        self.assertTrue(all(not path.startswith("/api/v1/operations/")
                            for path,_ in calls))

    def test_full_smoke_rejects_single_module_fake_or_foreign_data(self):
        for altered_module in ("purchases","inventory","returns"):
            def transport(host,path,headers,timeout):
                if path=="/healthz":
                    payload=self.health
                elif path=="/readyz":
                    payload=self.ready
                else:
                    module=path.split("/api/v1/operations/")[1].split("?")[0]
                    payload={
                        "module":module,"tenant_id":str(self.tenant),"items":[],
                        "read_only":True,"source":"one_erp_next_postgresql",
                        "page":{"limit":1,"offset":0},
                    }
                    if module==altered_module:
                        payload["tenant_id"]=str(uuid4())
                return 200,{"content-type":"application/json",
                            "cache-control":"private,no-store"},json.dumps(payload).encode()
            with self.subTest(module=altered_module):
                report=run_full_staging_smoke(self.conf,transport=transport)
                self.assertEqual(report["status"],"BLOCKED")
                self.assertFalse(report["checks"][altered_module+"_read"])
                self.assertFalse(report["release_approved"])


if __name__ == "__main__":
    import unittest
    unittest.main()
