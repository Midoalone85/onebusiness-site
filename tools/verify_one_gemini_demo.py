#!/usr/bin/env python3
"""Offline tests: Gemini demo never needs a real key, network or Odoo database."""
import ast
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib import error

SOURCE = Path(__file__).resolve().parents[1] / "one_custom/one_ui/models/one_gemini_demo.py"
raw = SOURCE.read_text(encoding="utf-8")
tree = ast.parse(raw)
klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneAskGeminiDemo")
method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == "one_gemini_demo")

assert "_inherit" in raw and "one.ask.wizard" in raw
assert "sudo(" not in raw and "account.move" not in raw and "env.company" not in raw
assert "x-goog-api-key" in raw and "https://generativelanguage.googleapis.com/" in raw

class Denied(Exception):
    pass

class Admin:
    def __init__(self, is_admin):
        self.is_admin = is_admin
    def has_group(self, group):
        return self.is_admin and group == "base.group_system"

class Wizard:
    def __init__(self, is_admin=True):
        self.env = SimpleNamespace(user=Admin(is_admin))

class FakeResp:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit):
        assert limit <= 32768
        return json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]
        }).encode("utf-8")

class FakeRequest:
    requests = []
    def __init__(self, url, data, headers, method):
        assert method == "POST"
        assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
        assert headers["x-goog-api-key"] == "FAKE_KEY_FOR_OFFLINE_TEST"
        body = json.loads(data.decode("utf-8"))
        assert body["contents"] == [{"role": "user", "parts": [{"text": "What is a trial balance?"}]}]
        assert len(json.dumps(body)) < 6000
        self.requests.append(body)

def fake_open(http_request, timeout):
    assert timeout <= 15
    return FakeResp()

ns = {
    "api": SimpleNamespace(model=lambda func: func),
    "AccessError": Denied,
    "_": lambda message: message,
    "os": os,
    "re": re,
    "json": json,
    "error": error,
    "request": SimpleNamespace(Request=FakeRequest, urlopen=fake_open),
    "_GEMINI_MODEL_ID": re.compile(r"^[a-z][a-z0-9._-]{1,63}$"),
    "_GEMINI_HOST": "https://generativelanguage.googleapis.com/v1beta/models/",
    "_DEMO_SYSTEM": "Never invent private company information.",
}
isolated = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
exec(compile(isolated, str(SOURCE), "exec"), ns)
call = ns["one_gemini_demo"]
with patch.dict(os.environ, {"ONE_GEMINI_DEMO_ENABLED": "1", "ONE_GEMINI_API_KEY": "FAKE_KEY_FOR_OFFLINE_TEST", "ONE_GEMINI_MODEL": "gemini-3.5-flash-lite"}):
    try:
        call(Wizard(False), "What is a trial balance?", True)
        raise AssertionError("Non-admin was allowed to call Gemini")
    except Denied:
        pass
    assert not FakeRequest.requests
    assert not call(Wizard(), "What is a trial balance?", False)["ok"]
    assert not FakeRequest.requests
    result = call(Wizard(), "What is a trial balance?", True)
    assert result == {"ok": True, "answer": "Gemini answer", "model": "gemini-3.5-flash-lite"}
    assert len(FakeRequest.requests) == 1
    assert not call(Wizard(), "x" * 501, True)["ok"]
    assert len(FakeRequest.requests) == 1

with patch.dict(os.environ, {"ONE_GEMINI_DEMO_ENABLED": "0", "ONE_GEMINI_API_KEY": "FAKE_KEY_FOR_OFFLINE_TEST"}):
    assert not call(Wizard(), "What is a trial balance?", True)["ok"]
    assert len(FakeRequest.requests) == 1

with patch.dict(os.environ, {"ONE_GEMINI_DEMO_ENABLED": "1", "ONE_GEMINI_API_KEY": ""}):
    assert not call(Wizard(), "What is a trial balance?", True)["ok"]
    assert len(FakeRequest.requests) == 1

with patch.dict(os.environ, {"ONE_GEMINI_DEMO_ENABLED": "1", "ONE_GEMINI_API_KEY": "FAKE_KEY_FOR_OFFLINE_TEST", "ONE_GEMINI_MODEL": "../../credentials"}):
    assert not call(Wizard(), "What is a trial balance?", True)["ok"]
    assert len(FakeRequest.requests) == 1

ui = (SOURCE.parent.parent / "static/src/xml/intelligence_workspace.xml").read_text(encoding="utf-8")
js = (SOURCE.parent.parent / "static/src/js/dashboard.js").read_text(encoding="utf-8")
assert 't-if="state.access.admin"' in ui
assert "state.geminiConsent" in ui and "state.geminiConsent = ev.target.checked" in ui
assert "askGeminiDemo()" in ui and "async askGeminiDemo()" in js
assert '"one_gemini_demo", [question, true]' in js
assert 'source: "gemini"' in js
assert "ONE AI" in ui
assert "Gemini Demo" not in ui and "Try Gemini" not in ui and "جرّب Gemini" not in ui

print("PASS: External AI bridge authorization, explicit consent, disabled default, fixed endpoint, mock response, ONE AI branding")
