"""Per-document audit timeline cannot invent actors, bypass tenants or write data."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from one_erp_core.activity_repository import load_record_activity


class Cursor:
    def __init__(self, *, role=(False,False), permitted=True, order_permitted=True,
                 details_permitted=True, rows=None):
        self.role,self.permitted,self.order_permitted,self.details_permitted,self.rows=(
            role,permitted,order_permitted,details_permitted,rows or [])
        self.queries=[]
        self.step=0
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def execute(self,sql,args=None): self.queries.append((sql,args))
    def fetchone(self):
        self.step+=1
        return (self.role if self.step==1 else
                (self.permitted,) if self.step==2 else
                (self.order_permitted,) if self.step==3 else
                (self.details_permitted,))
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, cursor): self.cur=cursor
    def transaction(self): return nullcontext()
    def cursor(self): return self.cur


class ActivityAuditReaderTests(unittest.TestCase):
    def setUp(self):
        self.tenant,self.actor,self.record,self.author=(
            uuid4(),uuid4(),uuid4(),uuid4())
    def read(self, cursor, **changes):
        return load_record_activity(Connection(cursor),**{
            "tenant_id":self.tenant,"actor_id":self.actor,
            "record_type":"invoice","record_id":self.record,
            **changes,
        })

    def test_immutable_source_actor_and_timestamp_only(self):
        stamp=datetime(2026,10,17,18,30,2,123456,
                       tzinfo=timezone(timedelta(hours=3)))
        cursor=Cursor(rows=[("44","invoice.issued",self.author,stamp)])
        page=self.read(cursor)
        self.assertEqual(page["items"],[{
            "event_id":"44","action":"invoice.issued",
            "actor_id":str(self.author),
            "occurred_at":"2026-10-17T15:30:02.123456Z",
        }])
        self.assertEqual(page["record_id"],str(self.record))
        self.assertTrue(page["read_only"])
        self.assertEqual(page["source"],"one_erp_next_postgresql")
        sqls=[sql for sql,args in cursor.queries]
        self.assertEqual(sqls[0],"SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertTrue(any("one_actor_has_permission" in sql for sql in sqls))
        self.assertEqual(cursor.queries[-1][1],(self.tenant,self.record)*3+(21,0))
        self.assertIn("FROM public.one_invoice_audit",cursor.queries[-1][0])

    def test_empty_and_paginated_results_do_not_invent_history(self):
        cursor=Cursor(rows=[])
        report=self.read(cursor)
        self.assertEqual(report["items"],[])
        self.assertEqual(report["page"]["has_more"],False)
        stamp=datetime.now(timezone.utc)
        cursor=Cursor(rows=[(str(i),"journal.posted",self.author,stamp)
                            for i in range(3)])
        report=self.read(cursor,record_type="journal",limit=2,offset=1)
        self.assertEqual(len(report["items"]),2)
        self.assertEqual(report["page"]["next_offset"],3)

    def test_denied_membership_or_privileged_db_role_never_queries_history(self):
        for cfg in ({"permitted":False},{"role":(True,False)},
                    {"role":(False,True)}):
            with self.subTest(cfg=cfg):
                cur=Cursor(**cfg)
                with self.assertRaises(PermissionError):self.read(cur)
                self.assertFalse(any("FROM public.one_invoice_audit" in sql
                                     for sql,_ in cur.queries))

    def test_unknown_table_injection_bad_identity_and_paging_denied(self):
        for changes in (
            {"record_type":"invoice; DROP TABLE one_tenants"},
            {"record_type":"unknown"},
            {"record_id":"forged"},
            {"actor_id":None},
            {"tenant_id":None},
            {"limit":100},{"offset":-1},{"offset":True},
        ):
            with self.subTest(changes=changes):
                cur=Cursor()
                with self.assertRaises(ValueError):self.read(cur,**changes)
                self.assertFalse(cur.queries)

    def test_invalid_audit_rows_fail_instead_of_misattributing_user(self):
        for rows in ([("1","invoice.issued",None,datetime.now(timezone.utc))],
                     [("1","invoice.issued",self.author,datetime.now())],
                     [("1","",self.author,datetime.now(timezone.utc))]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    self.read(Cursor(rows=rows))

    def test_commercial_confirmation_only_from_persisted_order_source(self):
        stamp=datetime(2026,10,17,17,0,tzinfo=timezone.utc)
        for record_type,kind,permission in (
            ("sales_order","sale","sales.order.view"),
            ("purchase_order","purchase","purchase.order.view"),
        ):
            with self.subTest(record_type=record_type):
                cursor=Cursor(rows=[("confirmed:"+str(self.record),
                    record_type+".confirmed",self.author,stamp)])
                result=self.read(cursor,record_type=record_type)
                self.assertEqual(result["items"][0]["action"],record_type+".confirmed")
                self.assertEqual(result["items"][0]["actor_id"],str(self.author))
                self.assertIn("2026-10-17T17:00:00",result["items"][0]["occurred_at"])
                self.assertTrue(any(args==(self.tenant,self.actor,permission)
                    for sql,args in cursor.queries))
                sql,args=cursor.queries[-1]
                self.assertIn("FROM public.one_commercial_orders",sql)
                self.assertIn("kind=\'"+kind+"\'",sql)
                self.assertIn("status=\'confirmed\'",sql)
                self.assertEqual(args,(self.tenant,self.record)*2+(21,0))
                self.assertIn("FROM public.one_commercial_change_audit",sql)

    def test_stored_draft_line_actions_are_displayable_without_disclosing_values(self):
        stamp=datetime(2026,10,17,17,3,tzinfo=timezone.utc)
        rows=[("change:"+str(self.record),"sales_order.line_changed",self.author,stamp)]
        result=self.read(Cursor(rows=rows),record_type="sales_order")
        self.assertEqual(result["items"][0]["action"],"sales_order.line_changed")
        self.assertEqual(set(result["items"][0]),{
            "event_id","action","actor_id","occurred_at"})
        self.assertNotIn("before_values",str(result))

    def test_order_change_diff_requires_separate_permission_and_safe_values(self):
        stamp=datetime(2026,10,17,17,0,tzinfo=timezone.utc)
        row=("change:"+str(self.record),"sales_order.line_changed",self.author,stamp,
             {"ordered_qty":"5.000","unit_price":"10.000"},
             {"ordered_qty":"7.000","unit_price":"12.000"})
        cursor=Cursor(rows=[row])
        data=self.read(cursor,record_type="sales_order",include_changes=True)
        self.assertEqual(data["items"][0]["changes"]["before"]["unit_price"],"10.000")
        self.assertEqual(data["items"][0]["changes"]["after"]["ordered_qty"],"7.000")
        self.assertTrue(any(args==(self.tenant,self.actor,"audit.order_changes.view")
                            for sql,args in cursor.queries))
        self.assertEqual(cursor.queries[-1][1],(self.tenant,self.record)*2+(21,0))
        denied=Cursor(details_permitted=False)
        with self.assertRaises(PermissionError):
            self.read(denied,record_type="sales_order",include_changes=True)
        self.assertFalse(any("FROM public.one_commercial_change_audit" in sql
                    for sql,args in denied.queries))
        with self.assertRaises(ValueError):
            self.read(Cursor(rows=[row[:4]+({"patient_secret":"NO"},row[5])]),
                      record_type="sales_order",include_changes=True)
        with self.assertRaises(ValueError):
            self.read(Cursor(),record_type="invoice",include_changes=True)

    def test_commercial_audit_requires_both_activity_and_module_read_permission(self):
        for config in ({"permitted":False},{"order_permitted":False}):
            with self.subTest(config=config):
                cursor=Cursor(**config)
                with self.assertRaises(PermissionError):
                    self.read(cursor,record_type="sales_order")
                self.assertFalse(any("FROM public.one_commercial_orders" in sql
                    for sql,args in cursor.queries))


if __name__=="__main__":
    unittest.main()
