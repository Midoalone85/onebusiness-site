import test from "node:test";
import assert from "node:assert/strict";
import {readReceivablesPage,validReportDate,toMillionths,fromMillionths,
  currentPageBalances,ReceivablesReadError} from "./receivables-client.mjs";
import {mountReceivablesPage} from "./receivables-page.mjs";

const tenant="11111111-1111-4111-8111-111111111111";
const id="22222222-2222-4222-8222-222222222222";
const as_of="2026-10-20";
const item={invoice_id:id,number:"INV-01 <fake>",partner_reference:"Real & Partners",
  currency:"SAR",issued_on:"2026-10-17",due_on:"2026-11-17",
  gross:"200.000",applied_receipts:"60.000",issued_credits:"0",
  outstanding:"140.000",days_overdue:0,aging_bucket:"current",status:"open"};
function reply(items=[item],more=false,offset=0){
  return {module:"receivables",tenant_id:tenant,as_of,items,
    page:{limit:25,offset,has_more:more,next_offset:more?offset+25:null},
    read_only:true,source:"one_erp_next_postgresql"};
}
const ok=(body)=>({ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>body});

test("exact decimals use bigint, preserving three- and six-place currencies",()=>{
  assert.equal(toMillionths("1.123456"),1123456n);
  assert.equal(fromMillionths(1123456n),"1.123456");
  assert.equal(toMillionths("90000000000000000.999999"),
    90000000000000000999999n);
  assert.equal(fromMillionths(toMillionths("1.230")),"1.23");
  for(const val of ["1e9","NaN","-5","2.1234567",2.4,"0x10"]){
    assert.throws(()=>toMillionths(val));
  }
  assert.deepEqual(currentPageBalances([item,
    {...item,invoice_id:"33333333-3333-4333-8333-333333333333",currency:"USD",
      gross:"100.123456",applied_receipts:"0",outstanding:"100.123456"}]),[
    {currency:"SAR",outstanding:"140.00"},
    {currency:"USD",outstanding:"100.123456"},
  ]);
});

test("strict report as-of date, signed GET and exact tenant-only parameters",async()=>{
  for(const invalid of ["2026-02-30","2026-13-01","17/10/2026","2026-1-1"]){
    assert.equal(validReportDate(invalid),false);
  }
  let call;
  const data=await readReceivablesPage({
    baseUrl:"https://private.erp.test/",tenant,as_of,token:"a.b.c",
    fetchImpl:async(url,options)=>{call={url,options};return ok(reply());},
  });
  assert.equal(data.items[0].outstanding,"140.000");
  assert.equal(call.url.pathname,"/api/v1/operations/receivables");
  assert.deepEqual([...call.url.searchParams.keys()],["tenant","as_of","limit","offset"]);
  assert.equal(call.url.searchParams.get("tenant"),tenant);
  assert.equal(call.url.searchParams.get("as_of"),as_of);
  assert.equal(call.options.method,"GET");assert.equal(call.options.credentials,"omit");
  assert.equal(call.options.redirect,"error");assert.equal(call.options.cache,"no-store");
  assert.equal(call.options.headers.Authorization,"Bearer a.b.c");
});

test("cross-company reports, fake balances, inconsistent ages and mixed status denied",async()=>{
  for(const bad of [
    {...reply(),tenant_id:"33333333-3333-4333-8333-333333333333"},
    {...reply(),as_of:"2026-11-17"},
    {...reply(),source:"browser-mock"},
    {...reply(),read_only:false},
    {...reply(),page:{...reply().page,offset:25}},
    reply([{...item,outstanding:"140.001"}]),
    reply([{...item,aging_bucket:"over_90"}]),
    reply([{...item,status:"no_open_balance_internal"}]),
    reply([{...item,applied_receipts:"200.000",outstanding:"0.000",
      aging_bucket:"internally_cleared",days_overdue:30,status:"no_open_balance_internal"}]),
  ]){
    await assert.rejects(readReceivablesPage({
      baseUrl:"https://private.erp.test/",tenant,as_of,token:"a.b.c",
      fetchImpl:async()=>ok(bad),
    }),ReceivablesReadError);
  }
});

test("bad origins/UUID/dates/auth prevent network entirely; denied network fails closed",async()=>{
  let calls=0;const fetchImpl=async()=>{calls++;return ok(reply());};
  for(const props of [
    {baseUrl:"http://private.erp.test/"},
    {baseUrl:"https://private.erp.test/evil"},
    {tenant:"fake"},
    {as_of:"2026-02-30"},
    {token:"bad"},
    {limit:51},
    {offset:-1},
  ]){
    await assert.rejects(readReceivablesPage({
      baseUrl:"https://private.erp.test/",tenant,as_of,token:"a.b.c",fetchImpl,...props
    }),TypeError);
  }
  assert.equal(calls,0);
  await assert.rejects(readReceivablesPage({
    baseUrl:"https://private.erp.test/",tenant,as_of,token:"a.b.c",
    fetchImpl:async()=>({ok:false,status:403}),
  }),ReceivablesReadError);
});

class Element{
  constructor(ownerDocument,tag){this.ownerDocument=ownerDocument;this.tagName=tag;
    this.textContent="";this.children=[];this.attributes={};this.listeners={};
    this.disabled=false;this.value="";}
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,callback){this.listeners[type]=callback;}
}
const document={createElement(tag){return new Element(document,tag);}};

test("private Arabic report shows actual backend row text, not HTML or bank-verified labels",async()=>{
  const root=document.createElement("main");
  const page=mountReceivablesPage({root,baseUrl:"https://private.erp.test/",
    tenant,as_of,getToken:async()=> "a.b.c",fetchImpl:async()=>ok(reply())});
  assert.equal(root.attributes.dir,"rtl");
  assert.match(root.children[1].textContent,/ليس إثبات/);
  assert.equal(await page.load(),true);
  assert.equal(root.children[7].children[0].textContent,
    "أرصدة الصفحة الحالية فقط: SAR 140.00");
  assert.equal(root.children[8].children[0].children[1].children[0].children[0].textContent,
    "INV-01 <fake>");
  assert.equal(page.snapshot().hasLoaded,true);
  page.dispose();assert.equal(root.children.length,0);
});

test("previous/next paging uses bounded query, clears stale sensitive data on denial",async()=>{
  const root=document.createElement("main"),offsets=[];
  const items=Array.from({length:25},(_,i)=>({...item,invoice_id:
    (i+1).toString(16).padStart(8,"0")+"-1111-4111-8111-111111111111"}));
  const page=mountReceivablesPage({root,baseUrl:"https://private.erp.test/",
    tenant,as_of,getToken:async()=> "a.b.c",
    fetchImpl:async(url)=>{
      const offset=Number(url.searchParams.get("offset"));offsets.push(offset);
      return offset===0?ok(reply(items,true,0)):({ok:false,status:403});
    }
  });
  assert.equal(await page.load(),true);
  const nav=root.children[9],next=nav.children[2];
  assert.equal(next.disabled,false);
  await next.listeners.click();
  await Promise.resolve();await Promise.resolve();
  assert.deepEqual(offsets,[0,25]);
  assert.equal(root.children[8].children.length,0);
  page.clear();assert.equal(page.snapshot().hasLoaded,false);
  assert.equal(root.children[7].children.length,0);
  page.dispose();
});
