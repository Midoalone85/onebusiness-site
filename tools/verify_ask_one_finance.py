#!/usr/bin/env python3
"""Offline Ask ONE checks; does not require Odoo or connect to company data."""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "one_custom/one_ui/models/ask_one.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneAskWizard")


def isolated_method(name, namespace):
    method = next(
        node for node in klass.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class NoAccess(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


finance_access = isolated_method(
    "_require_finance_access",
    {"AccessError": NoAccess, "_": lambda message: message},
)


class TestUser:
    def __init__(self, allowed):
        self.allowed = allowed

    def has_group(self, group):
        return self.allowed and group == "account.group_account_user"


for allowed in (False, True):
    context = SimpleNamespace(env=SimpleNamespace(user=TestUser(allowed)))
    try:
        finance_access(context)
        denied = False
    except NoAccess:
        denied = True
    require(denied == (not allowed), "Finance authorization guard failed")


class TestAsk:
    def __init__(self):
        self.ranges = []

    def _contains_any(self, question, terms):
        return any(term in question for term in terms)

    def _period_metrics(self, start, end):
        self.ranges.append((start, end))
        if start.month == 10:
            return {"revenue": 100, "profit": 25}
        return {"revenue": 80, "profit": 12}

    def _money(self, amount):
        return f"{amount:.2f} SAR"


compare = isolated_method(
    "_financial_period_answer",
    {
        "timedelta": timedelta,
        "fields": SimpleNamespace(Date=SimpleNamespace(context_today=lambda _self: date(2026, 10, 12))),
    },
)
ask = TestAsk()
response = compare(ask, "قارن مبيعات الشهر بالشهر السابق", "revenue", True)
require("2026-10-01" in response and "2026-10-12" in response, "Current MTD dates missing")
require("2026-09-01" in response and "2026-09-12" in response, "Previous MTD dates missing")
require("+25.0%" in response, "Revenue percentage comparison incorrect")
require(ask.ranges == [(date(2026, 10, 1), date(2026, 10, 12)), (date(2026, 9, 1), date(2026, 9, 12))], "Comparison incorrectly uses a full preceding month")

ask.ranges.clear()
response = compare(ask, "مبيعات الشهر اللي فات", "revenue", True)
require(ask.ranges == [(date(2026, 9, 1), date(2026, 9, 30))], "Full preceding month selection failed")

ask.ranges.clear()
response = compare(ask, "profit this month", "profit", False)
require("25.00 SAR" in response, "Profit calculation selection failed")
require(ask.ranges == [(date(2026, 10, 1), date(2026, 10, 12))], "Current month selection failed")

source = SOURCE.read_text(encoding="utf-8")
answer_start = source.index("    def _answer_question(")
answer_head = source[answer_start:source.index("        if self._contains_any(q, (", answer_start)]
require("one_get_pulse" not in answer_head, "Unconditional dashboard financial data read reintroduced")
require(source.count("self._require_finance_access()") >= 6, "Sensitive finance branches are not access-checked")
require("sudo()" not in source, "Unexpected Odoo sudo() in Ask ONE")


# Verify the dashboard's server-side JSON/RPC path is not readable just
# because a user can call res.company from the browser.
dashboard_source = (
    SOURCE.parent / "dashboard_pulse.py"
).read_text(encoding="utf-8")
dashboard_tree = ast.parse(dashboard_source)
dashboard_class = next(
    node for node in dashboard_tree.body
    if isinstance(node, ast.ClassDef) and node.name == "ResCompany"
)
dashboard_method = next(
    node for node in dashboard_class.body
    if isinstance(node, ast.FunctionDef) and node.name == "one_get_pulse"
)
require(
    "has_group" in ast.unparse(dashboard_method)
    and "raise AccessError" in ast.unparse(dashboard_method),
    "Financial dashboard server RPC missing accounting permission validation",
)

aging = isolated_method(
    "_aged_receivables",
    {
        "fields": SimpleNamespace(Date=SimpleNamespace(
            context_today=lambda _self: date(2026, 10, 12)
        )),
        "timedelta": timedelta,
    },
)


class Ledger:
    def __init__(self):
        self.domains = []
        self.amounts = [100, 200, 300, 400]

    def _read_group(self, domain, aggregates):
        self.domains.append(domain)
        assert aggregates == ["amount_residual:sum"]
        return [(self.amounts[len(self.domains) - 1],)]


ledger = Ledger()


class TestEnv:
    company = SimpleNamespace(id=123)

    def __getitem__(self, model):
        require(model == "account.move.line", "Aging must read journal entry lines")
        return ledger


dummy = SimpleNamespace(env=TestEnv())
buckets = aging(dummy)
require(buckets == {
    "1_30": 100, "31_60": 200, "61_90": 300, "over_90": 400
}, "Aging totals are incorrect")
require(len(ledger.domains) == 4, "Expected four exclusive aging buckets")

today = date(2026, 10, 12)
for domain in ledger.domains:
    require(("company_id", "=", 123) in domain, "Aging missing company filter")
    require(("move_id.state", "=", "posted") in domain, "Aging includes unposted entries")
    require(("amount_residual", ">", 0) in domain, "Aging includes customer credit balances")
    require(("date_maturity", "<", today) in domain, "Aging includes amounts not overdue")

require(
    ("date_maturity", ">=", date(2026, 9, 12)) in ledger.domains[0]
    and ("date_maturity", "<", date(2026, 9, 12)) in ledger.domains[1]
    and ("date_maturity", ">=", date(2026, 8, 13)) in ledger.domains[1]
    and ("date_maturity", "<", date(2026, 8, 13)) in ledger.domains[2]
    and ("date_maturity", ">=", date(2026, 7, 14)) in ledger.domains[2]
    and ("date_maturity", "<", date(2026, 7, 14)) in ledger.domains[3],
    "Aging day boundaries overlap or have gaps",
)
require("sudo()" not in dashboard_source, "Dashboard finance RPC unexpectedly uses sudo")

advice = isolated_method(
    "_advice_answer",
    {"fields": SimpleNamespace(Date=SimpleNamespace(
        context_today=lambda _self: date(2026, 10, 12)
    ))},
)


class FakeAdvice:
    def _period_metrics(self, start, end):
        assert start == date(2026, 10, 1) and end == date(2026, 10, 12)
        return {"revenue": 1000, "profit": -120}

    def _aged_receivables(self):
        return {"1_30": 10, "31_60": 20, "61_90": 30, "over_90": 400}

    def _money(self, value):
        return f"{value:.2f} SAR"


recommendation = advice(FakeAdvice(), True)
require("أكثر من 90 يوم" in recommendation, "90-day collection alert missing")
require("خسارة دفترية" in recommendation, "Ledger loss warning missing")
require("القيود المرحلة" in recommendation, "Advice lacks accounting provenance")
english = advice(FakeAdvice(), False)
require("over 90 days" in english and "book loss" in english, "English insights missing")

# Intent routing must gate access BEFORE calculating amounts.
dispatch = isolated_method("_answer_question", {})
class FakeDispatch:
    def __init__(self, allowed):
        self.allowed = allowed
        self.called = []

    def _is_arabic(self, query):
        return True

    def _contains_any(self, question, terms):
        return any(term in question for term in terms)

    def _require_finance_access(self):
        self.called.append("permission")
        if not self.allowed:
            raise NoAccess()

    def _aging_answer(self, is_ar):
        self.called.append("aging")
        return "aged"

    def _advice_answer(self, is_ar):
        self.called.append("advice")
        return "advised"

    def _explain_profit_change(self, is_ar):
        self.called.append("bridge")
        return "explained"


for question, expected in (("أعمار الديون", "aging"), ("تنصحني بإيه", "advice"), ("ليه الأرباح قلت؟", "bridge")):
    denied = FakeDispatch(False)
    try:
        dispatch(denied, question)
        raise AssertionError("Non-accountant saw finance output")
    except NoAccess:
        pass
    require(denied.called == ["permission"], "Finance query read data before checking ACL")

    permitted = FakeDispatch(True)
    result = dispatch(permitted, question)
    require(result in ("aged", "advised", "explained"), "Approved question did not route")
    require(permitted.called == ["permission", expected], "Wrong secure intent routing")


profit_bridge = isolated_method(
    "_explain_profit_change",
    {
        "fields": SimpleNamespace(Date=SimpleNamespace(
            context_today=lambda _self: date(2026, 10, 12)
        )),
        "timedelta": timedelta,
    },
)


class FakeBridge:
    def _period_metrics(self, start, end):
        if start.month == 10:
            return {"revenue": 900, "profit": 70}
        require(
            start == date(2026, 9, 1) and end == date(2026, 9, 12),
            "Profit comparison must use the same elapsed days",
        )
        return {"revenue": 1100, "profit": 200}

    def _money(self, value):
        return f"{value:.2f} SAR"


bridge = profit_bridge(FakeBridge(), True)
require("-130.00 SAR" in bridge, "Incorrect profit change")
require("-200.00 SAR" in bridge and "70.00 SAR" in bridge, "Revenue/expense decomposition does not reconcile")
require("ليس إثباتًا" in bridge, "Accounting bridge presented as causal fact")

print("PASS: Dashboard RPC ACL, aged receivable buckets and grounded advice")


# ONE Intelligence must stay local and non-destructive, with a working action.
import xml.etree.ElementTree as ET

workspace = SOURCE.parent.parent / "static/src/xml/intelligence_workspace.xml"
root = ET.parse(workspace).getroot()
require(any(node.get("t-name") == "one_ui.OneIntelligence" for node in root.iter()), "Intelligence QWeb template missing")
manifest_text = (SOURCE.parent.parent / "__manifest__.py").read_text(encoding="utf-8")
require("one_ui/static/src/xml/intelligence_workspace.xml" in manifest_text, "Missing assistant template asset")
require("one_ui/static/src/scss/one_intelligence.scss" in manifest_text, "Missing assistant stylesheet")
dash_xml = (workspace.parent / "dashboard.xml").read_text(encoding="utf-8")
dash_js = (workspace.parent.parent / "js/dashboard.js").read_text(encoding="utf-8")
require('t-call="one_ui.OneIntelligence"' in dash_xml, "Dashboard is not linked to assistant")
require("async askOne(" in dash_js and "this.orm.call(" in dash_js, "Assistant lacks live ORM wiring")
require("showIntelligence()" in dash_js, "Assistant navigation action missing")
require("fetch(" not in dash_js, "Assistant must not use third-party browser fetch")
require("one_answer" in source and "has_group(\"base.group_user\")" in source, "Backend read-only assistant access check missing")

rpc = isolated_method(
    "one_answer",
    {"api": SimpleNamespace(model=lambda fn: fn), "AccessError": NoAccess, "_": lambda msg: msg},
)


class RpcFake:
    def __init__(self, internal, error=False):
        self.env = SimpleNamespace(user=TestUser(internal))
        self.seen = []
        self.error = error

    def _answer_question(self, prompt):
        self.seen.append(prompt)
        if self.error:
            raise NoAccess()
        return "Posted ledger response"

    def _is_arabic(self, value):
        return "ربح" in value


public = RpcFake(False)
try:
    rpc(public, "مبيعات الشهر")
    raise AssertionError("Public user reached assistant endpoint")
except NoAccess:
    pass
require(not public.seen, "Public user triggered a business data query")
member = RpcFake(True)
require(rpc(member, "مبيعات الشهر") == "Posted ledger response", "Authenticated RPC broken")
require(member.seen == ["مبيعات الشهر"], "RPC failed to use original question")
require("250" in rpc(member, "x" * 251), "RPC length bound missing")
require(rpc(RpcFake(True, True), "ربح الشهر") == "ليس لديك صلاحية لعرض البيانات المطلوبة.", "Denied ledger query leaked an error")

print("PASS: ONE Intelligence QWeb, zero-API routing and read-only RPC")

print("PASS: Ask ONE finance authorization and MTD comparison unit checks")
print("NOTE: Live Odoo ORM, access rules and Oracle deployment still require integration tests.")
