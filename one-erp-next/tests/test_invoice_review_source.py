"""Isolated, mocked review-source contract tests; no Oracle or production data."""
import unittest
from decimal import Decimal
from uuid import uuid4

from one_erp_core.invoice_review_source import load_invoice_review_summary


class Tx:
    def __enter__(self):return self
    def __exit__(self,*exc):return False


class Cursor:
    def __init__(self,source,permission=True,role=(False,False)):
        self.source=source
        self.permission=permission
        self.role=role
        self.last=""
        self.statements=[]
    def __enter__(self):return self
    def __exit__(self,*exc):return False
    def execute(self,sql,args=None):
        self.last=sql
        self.statements.append((sql,args))
    def fetchone(self):
        if "FROM pg_roles" in self.last:return self.role
        if "one_actor_has_permission" in self.last:return (self.permission,)
        if "FROM public.one_invoice_source_requests r" in self.last:return self.source
        if "FROM public.one_currency_precision" in self.last:return (2,)
        return None
    def fetchall(self):
        if "FROM public.one_memberships" in self.last:
            return [(self.source[0],),(self.source[12],)]
        return []


class DB:
    def __init__(self,cur):self.cur=cur
    def transaction(self):return Tx()
    def cursor(self):return self.cur


class InvoiceReviewSourceTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.document=uuid4(),uuid4(),uuid4()
        self.requester,self.tax_reviewer,self.period,self.order=[
            uuid4() for _ in range(4)
        ]
        self.payload={
            "kind":"customer_invoice","number":"SALES-REVIEW-01",
            "partner_id":"customer-test","issued_on":"2026-10-17",
            "due_on":"2026-11-17","currency":"SAR","source_invoice_id":None,
            "accounts":{
                "receivable_id":"ar","payable_id":"ap",
                "output_vat_id":"vatoutput","input_vat_id":"vatinput",
            },
            "lines":[{
                "description":"Verified sale line","account_id":"sales",
                "quantity":"2","unit_price":"100.00",
                "discount_amount":"10.00",
                "tax_category":"standard","tax_rate":"0.150000",
                "tax_code":"KSA_STANDARD","input_tax_recoverable":False,
            }],
        }
        self.source=(
            self.requester,self.payload,self.period,self.order,[1],
            "SALES-REVIEW-01",
            __import__("datetime").date(2026,10,17),
            __import__("datetime").date(2026,11,17),
            "standard",Decimal("0.150000"),"KSA_STANDARD",
            "internal-policy-v01",self.tax_reviewer,
            "ar","ap","vatoutput","vatinput","SAR","customer-test",
        )

    def run_source(self,**options):
        cursor=Cursor(options.pop("source",self.source),
                      permission=options.pop("permission",True),
                      role=options.pop("role",(False,False)))
        return load_invoice_review_summary(
            DB(cursor),tenant_id=self.tenant,actor_id=options.pop("actor",self.actor),
            document_id=self.document),cursor

    def test_full_exact_ledger_evidence_and_line_level_rounding(self):
        result,cur=self.run_source()
        self.assertTrue(result["reviewable"])
        self.assertEqual(result["tenant"],str(self.tenant))
        self.assertEqual(result["document_id"],str(self.document))
        self.assertEqual(result["source"],"verified_internal_postgresql")
        self.assertEqual(result["lines"][0]["quantity"],"2")
        self.assertEqual(result["lines"][0]["net"],"190.00")
        self.assertEqual(result["lines"][0]["tax"],"28.50")
        self.assertEqual(result["lines"][0]["gross"],"218.50")
        self.assertIn({"label":"Gross","value":"218.50"},result["fields"])
        sql="\n".join(x[0] for x in cur.statements)
        self.assertIn("READ ONLY",sql)
        self.assertIn("rolbypassrls",sql)
        self.assertIn("one_actor_has_permission",sql)
        self.assertNotIn("INSERT INTO",sql)
        self.assertNotIn("UPDATE ",sql)

    def test_role_bypass_permission_and_unapproved_record_refuse_evidence(self):
        for opts in ({"role":(True,False)},{"role":(False,True)},
                     {"permission":False},{"source":None}):
            with self.subTest(opts=opts),self.assertRaises(PermissionError):
                self.run_source(**opts)

    def test_self_approval_discrepant_intent_and_invalid_amount_denied(self):
        for source in (
            (self.actor,)+self.source[1:],
            self.source[:5]+("tampered-number",)+self.source[6:],
            self.source[:4]+([1,1],)+self.source[5:],
            self.source[:8]+("zero_rated",)+self.source[9:],
        ):
            with self.subTest(source=str(source)[:30]),self.assertRaises(PermissionError):
                self.run_source(source=source)
        bad=dict(self.payload,lines=[dict(self.payload["lines"][0],unit_price="NaN")])
        with self.assertRaises(PermissionError):
            self.run_source(source=(self.source[0],bad)+self.source[2:])

    def test_missing_identity_and_invalid_uuid_refused_before_db(self):
        cur=Cursor(self.source)
        for tenant,actor,doc in (
            (None,self.actor,self.document),
            (self.tenant,None,self.document),
            (self.tenant,self.actor,"not-a-uuid"),
        ):
            with self.assertRaises(PermissionError):
                load_invoice_review_summary(DB(cur),tenant_id=tenant,
                                            actor_id=actor,document_id=doc)
        self.assertFalse(cur.statements)


if __name__=="__main__":
    unittest.main()
