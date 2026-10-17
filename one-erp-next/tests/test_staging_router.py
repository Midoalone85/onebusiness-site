"""Staging WSGI composition regression; never opens Oracle or real tenants."""
from __future__ import annotations

from io import BytesIO
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.finance_http_boundary import AuthenticatedFinanceIdentity
from one_erp_core.finance_wsgi import TenantFinanceConnections
from one_erp_core.staging_health import StagingReadiness
from one_erp_core.staging_router import OneERPStagingRouter

HOST = "staging.erp.example.net"
SHA = "9" * 40
TOKEN = "header.claims.signature"
_CHECKS = (
    "trusted_identity_provider", "isolated_tenant_routing",
    "restricted_database_roles", "approved_migrations",
    "restored_backup_verified", "live_https_certificate",
)


class FakeDB:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeVerifier:
    def __init__(self, identity):
        self.identity = identity
        self.calls = 0
        self.deny = False

    def verify_access_token(self, token):
        self.calls += 1
        if self.deny or token != TOKEN:
            raise PermissionError("Denied")
        return self.identity


class StagingRouterTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.actor = uuid4(), uuid4()
        self.identity = AuthenticatedFinanceIdentity(
            self.actor, frozenset({self.tenant}))
        self.verifier = FakeVerifier(self.identity)
        self.db_calls = 0
        self.created = []
        self.ready_values = {name: True for name in _CHECKS}
        self.fail_probe = False

        def factory():
            self.db_calls += 1
            db = FakeDB()
            self.created.append(db)
            return db

        self.connections = TenantFinanceConnections({self.tenant: factory})
        self.report_db_calls = 0
        def report_factory():
            self.report_db_calls += 1
            return FakeDB()
        self.report_connections=TenantFinanceConnections({self.tenant:report_factory})
        self.app = self.router()

    def probe(self):
        if self.fail_probe:
            raise RuntimeError("Secret database endpoint")
        return StagingReadiness(self.ready_values)

    def router(self, **options):
        return OneERPStagingRouter(
            allowed_hosts=frozenset({HOST}), deployed_sha=SHA,
            verifier=self.verifier, operations_connections=self.connections,
            finance_report_connections=self.report_connections,
            readiness_probe=self.probe, **options)

    def request(self, path="/api/v1/operations/sales", *, app=None,
                method="GET", body=b"", **overrides):
        env = {
            "wsgi.url_scheme": "https", "HTTP_HOST": HOST,
            "SCRIPT_NAME": "", "REQUEST_METHOD": method,
            "PATH_INFO": path, "QUERY_STRING": f"tenant={self.tenant}&limit=1&offset=0",
            "CONTENT_LENGTH": "", "HTTP_AUTHORIZATION": "Bearer " + TOKEN,
            "HTTP_COOKIE": "", "wsgi.input": BytesIO(body),
        }
        if path in ("/healthz", "/readyz"):
            env["QUERY_STRING"] = ""
            env["HTTP_AUTHORIZATION"] = ""
        if method == "POST":
            env["CONTENT_LENGTH"] = str(len(body))
            env["CONTENT_TYPE"] = "application/json"
            env["QUERY_STRING"] = ""
        env.update(overrides)
        captured = {}
        data = b"".join((app or self.app)(env, lambda status, headers:
                           captured.update(status=status, headers=dict(headers))))
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            # The existing finance dashboard responds with plain-text errors
            # or private HTML, unlike the JSON operations/health endpoints.
            parsed = data.decode("utf-8", errors="replace")
        return captured["status"], captured["headers"], parsed

    def test_health_reachable_without_token_but_readiness_requires_probe(self):
        status,headers,payload = self.request("/healthz")
        self.assertEqual(status,"200 OK")
        self.assertEqual(payload["commit_sha"],SHA)
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertEqual(self.request("/readyz")[0],"200 OK")
        self.assertEqual(self.db_calls,0)
        self.assertEqual(self.verifier.calls,0)

    def test_all_four_authorized_module_routes_source_backed(self):
        for module in ("sales","purchases","inventory","returns"):
            with self.subTest(module=module):
                source={
                    "module":module, "tenant_id":str(self.tenant), "items":[],
                    "read_only":True, "source":"one_erp_next_postgresql",
                }
                with patch("one_erp_core.operations_read_wsgi.load_operations_page",
                           return_value=source) as load:
                    status,headers,payload = self.request(
                        "/api/v1/operations/" + module)
                self.assertEqual(status,"200 OK")
                self.assertEqual(payload,source)
                self.assertEqual(load.call_args.kwargs["actor_id"],self.actor)
                self.assertEqual(load.call_args.kwargs["tenant_id"],self.tenant)
                self.assertIn("no-store",headers["Cache-Control"])
        self.assertEqual(self.verifier.calls,4)
        self.assertEqual(self.db_calls,4)
        self.assertTrue(all(db.closed for db in self.created))

    def test_missing_probe_never_opens_tenant_or_trusts_url_actor(self):
        app=OneERPStagingRouter(
            allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
            verifier=self.verifier,operations_connections=self.connections,
            finance_report_connections=self.report_connections,
        )
        self.assertEqual(self.request("/readyz",app=app)[0],
                         "503 Service Unavailable")
        status,headers,_=self.request(app=app)
        self.assertEqual(status,"503 Service Unavailable")
        self.assertIn("no-store",headers["Cache-Control"])
        self.assertEqual((self.verifier.calls,self.db_calls),(0,0))

    def test_deny_every_module_when_one_readiness_gate_fails(self):
        for key in _CHECKS:
            self.ready_values[key]=False
            with self.subTest(key=key):
                for module in ("sales","purchases","inventory","returns"):
                    status,_,payload=self.request("/api/v1/operations/"+module)
                    self.assertEqual(status,"503 Service Unavailable")
                    self.assertNotIn("tenant",str(payload))
                self.assertEqual(self.request("/readyz")[0],
                                 "503 Service Unavailable")
            self.ready_values[key]=True
        self.assertEqual((self.verifier.calls,self.db_calls),(0,0))

    def test_bad_readiness_probe_does_not_disclose_secrets(self):
        self.fail_probe=True
        status,headers,result=self.request()
        self.assertEqual(status,"503 Service Unavailable")
        self.assertNotIn("Secret",str(result))
        self.assertNotIn("database",str(result).lower())
        self.assertEqual((self.verifier.calls,self.db_calls),(0,0))

    def test_untrusted_host_insecure_transport_and_prefix_are_rejected(self):
        cases=(
            {"HTTP_HOST":"stolen.example.net"},
            {"wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"},
            {"SCRIPT_NAME":"/admin"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.request(**changes)[0],"403 Forbidden")
        self.assertEqual((self.verifier.calls,self.db_calls),(0,0))

    def test_unknown_path_or_methods_never_hit_financial_database(self):
        for path in (
            "/admin", "/internal", "/api/v1/operations/unknown",
            "/api/v1/finance/banks","/api/v1/finance/internal-documents",
            "/api/v1/operations/sales/../returns",
        ):
            self.assertEqual(self.request(path)[0],"404 Not Found")
        self.assertEqual(self.db_calls,0)

    def test_operations_only_allow_get_and_verified_company(self):
        self.assertEqual(self.request(method="POST")[0],"405 Method Not Allowed")
        self.assertEqual(self.request(HTTP_AUTHORIZATION="")[0],
                         "401 Unauthorized")
        self.verifier.deny=True
        self.assertEqual(self.request()[0],"403 Forbidden")
        self.verifier.deny=False
        self.assertEqual(self.request(
            QUERY_STRING=f"tenant={uuid4()}&limit=1&offset=0")[0],
            "403 Forbidden")
        self.assertEqual(self.db_calls,0)

    def test_finance_report_is_mounted_but_must_be_authorized(self):
        self.assertEqual(self.request(
            "/api/v1/finance/dashboard",QUERY_STRING=""
        )[0],"400 Bad Request")
        self.assertEqual(self.db_calls,0)
        self.assertEqual(self.request(
            "/api/v1/finance/dashboard",
            QUERY_STRING=(
                f"tenant={self.tenant}&from=2026-10-01&as_of=2026-10-17"
                "&currency=SAR"),
            HTTP_AUTHORIZATION=""
        )[0],"401 Unauthorized")
        self.assertEqual(self.db_calls,0)

    def test_finance_commands_disabled_by_default_and_cannot_toggle_accidentally(self):
        status,_,_=self.request("/api/v1/finance/internal-documents",method="POST")
        self.assertEqual(status,"404 Not Found")
        for kwargs in (
            {"enable_internal_finance_writes": True},
            {"enable_internal_finance_writes": 1},
        ):
            with self.assertRaises(ValueError):
                self.router(**kwargs)

    def test_opted_in_finance_command_still_denies_unready_and_bad_auth(self):
        loader=object()
        app=self.router(
            approved_source_loader=loader,
            finance_write_connections=TenantFinanceConnections({self.tenant:FakeDB}),
            enable_internal_finance_writes=True)
        command=json.dumps({
            "action":"issue_internal_invoice",
            "tenant":str(self.tenant),"document_id":str(uuid4()),
        }).encode()
        self.ready_values["restored_backup_verified"]=False
        self.assertEqual(self.request(
            "/api/v1/finance/internal-documents",
            method="POST",body=command,app=app)[0],
            "503 Service Unavailable")
        self.ready_values["restored_backup_verified"]=True
        self.assertEqual(self.request(
            "/api/v1/finance/internal-documents",
            method="POST",body=command,app=app,
            HTTP_AUTHORIZATION=""
        )[0],"401 Unauthorized")
        self.assertEqual(self.db_calls,0)

    def test_report_uses_separate_reader_factory_not_operations_login(self):
        # Reports may error against FakeDB, but only the dedicated report
        # connection factory may be selected by a verified tenant request.
        status,_,_ = self.request(
            "/api/v1/finance/dashboard",
            QUERY_STRING=(
                f"tenant={self.tenant}&from=2026-10-01"
                "&as_of=2026-10-17&currency=SAR"),
        )
        self.assertEqual(status,"503 Service Unavailable")
        self.assertEqual(self.report_db_calls,1)
        self.assertEqual(self.db_calls,0)

    def test_read_operations_and_finance_report_cannot_share_registry(self):
        with self.assertRaises(ValueError):
            OneERPStagingRouter(
                allowed_hosts=frozenset({HOST}),deployed_sha=SHA,
                verifier=self.verifier,operations_connections=self.connections,
                finance_report_connections=self.connections,
                readiness_probe=self.probe)

    def test_internal_write_registry_must_be_separate_from_readers(self):
        for write_registry in (self.connections,self.report_connections):
            with self.subTest(write_registry=write_registry):
                with self.assertRaises(ValueError):
                    self.router(
                        approved_source_loader=object(),
                        finance_write_connections=write_registry,
                        enable_internal_finance_writes=True)
        with self.assertRaises(ValueError):
            self.router(finance_write_connections=TenantFinanceConnections({
                self.tenant:FakeDB,
            }))

    def test_config_rejects_unknown_tenant_registry_and_bad_hosts(self):
        for connection in (None,object()):
            with self.assertRaises(ValueError):
                OneERPStagingRouter(
                    allowed_hosts=frozenset({HOST}),
                    deployed_sha=SHA,
                    verifier=self.verifier,
                    operations_connections=connection,
                    finance_report_connections=self.report_connections)
        for hosts in (frozenset(),frozenset({"BAD.example.net"})):
            with self.assertRaises(ValueError):
                OneERPStagingRouter(
                    allowed_hosts=hosts,deployed_sha=SHA,
                    verifier=self.verifier,operations_connections=self.connections,
                    finance_report_connections=self.report_connections)

if __name__=="__main__":
    unittest.main()
