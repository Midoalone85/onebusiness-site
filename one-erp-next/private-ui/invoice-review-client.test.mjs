import test from "node:test";
import assert from "node:assert/strict";
import {readInternalInvoiceSource,validateInternalInvoiceSource} from "./invoice-review-client.mjs";
import {mountInvoiceApprovalPage} from "./invoice-approval-page.mjs";

const tenant="11111111-1111-4111-8111-111111111111";
const document_id="22222222-2222-4222-8222-222222222222";
const lines=[{
  line_number:1,description:"Genuine <strong>source</strong>",
  quantity:"2",unit_price:"100.00",discount_amount:"10.00",
  tax_category:"standard",tax_rate:"0.150000",tax_code:"KSA_STANDARD",
  net:"190.00",tax:"28.50",gross:"218.50"
}];
const source={
  tenant,document_id,reviewable:true,source:"verified_internal_postgresql",
  fields:[{label:"Gross",value:"218.50"}],lines,
};
const ok=(body)=>({ok:true,status:200,headers:{get:()=>"application/json; charset=utf-8"},
  json:async()=>body});

test("GET only asks for canonical immutable references, never supplied monetary payload",async()=>{
  let request;
  const result=await readInternalInvoiceSource({baseUrl:"https://erp.example.test/",
    tenant,document_id,token:"a.b.c",
    fetchImpl:async(url,options)=>{request={url,options};return ok(source);}});
  assert.equal(request.url.pathname,"/api/v1/finance/internal-invoice-source");
  assert.equal(request.url.searchParams.get("tenant"),tenant);
  assert.equal(request.url.searchParams.get("document_id"),document_id);
  assert.equal(request.url.searchParams.has("amount"),false);
  assert.equal(request.options.method,"GET");assert.equal(request.options.credentials,"omit");
  assert.equal(request.options.cache,"no-store");assert.equal(request.options.redirect,"error");
  assert.equal(request.options.headers.Authorization,"Bearer a.b.c");
  assert.deepEqual(result.lines,lines);
});

test("wrong-source, missing lines, malformed line and not reviewable fail closed",async()=>{
  for(const bad of [
    {...source,tenant:document_id},
    {...source,document_id:tenant},
    {...source,source:"mocked_browser_json"},
    {...source,reviewable:false},
    {...source,lines:[]},
    {...source,lines:[{...lines[0],gross:123}]},
    {...source,lines:[lines[0],lines[0]]},
    {...source,fields:[{label:"Untrusted",value:{secret:"x"}}]},
  ]){
    await assert.rejects(readInternalInvoiceSource({
      baseUrl:"https://erp.example.test/",tenant,document_id,token:"a.b.c",
      fetchImpl:async()=>ok(bad),
    }));
  }
});

test("never calls network for unsafe host, bad JWT or UUID",async()=>{
  let requests=0;const fetchImpl=async()=>{requests++;return ok(source);};
  for(const opts of [
    {baseUrl:"http://erp.example.test/"},
    {baseUrl:"https://erp.example.test/fake"},
    {token:"fake"},
    {document_id:"not-uuid"},
    {tenant:"00000000-0000-0000-0000-000000000000"},
  ]){
    await assert.rejects(readInternalInvoiceSource({
      baseUrl:"https://erp.example.test/",tenant,document_id,token:"a.b.c",fetchImpl,...opts
    }));
  }
  assert.equal(requests,0);
});

test("denied HTTP, HTML and network failures never return reviewer source",async()=>{
  for(const fake of [
    {ok:false,status:403},
    {...ok(source),headers:{get:()=>"text/html"}},
    {...ok(source),json:async()=>{throw Error("secret");}},
  ]){
    await assert.rejects(readInternalInvoiceSource({
      baseUrl:"https://erp.example.test/",tenant,document_id,token:"a.b.c",
      fetchImpl:async()=>fake
    }));
  }
  await assert.rejects(readInternalInvoiceSource({
    baseUrl:"https://erp.example.test/",tenant,document_id,token:"a.b.c",
    fetchImpl:async()=>{throw Error("network");}
  }));
});

test("invoice reviewer rejects forged numeric text, impossible line totals and tax categories",()=>{
  const cases=[
    {gross:"Infinity"},{net:"NaN"},{tax:"1e309"},{quantity:"0"},
    {quantity:"-2"},{quantity:"2,000"},{unit_price:"-100.00"},
    {discount_amount:"abc"},{net:"190.00",gross:"219.50"},
    {tax_category:"invented"},{tax_category:"exempt",tax_rate:"0.150000"},
    {tax_rate:"1.25"},{tax_rate:"-0.1"},{tax_rate:"0"},
    {tax_code:""},{tax_code:"NONE"},
  ];
  for(const edits of cases){
    const altered={...source,lines:[{...lines[0],...edits}]};
    assert.throws(()=>validateInternalInvoiceSource(altered,{tenant,document_id}),
      {name:"InternalApprovalError"},JSON.stringify(edits));
  }
  assert.deepEqual(validateInternalInvoiceSource(source,{tenant,document_id}).lines,lines);
  const noTax={...lines[0],tax_category:"out_of_scope",tax_rate:"0",
    net:"190.00",tax:"0.00",gross:"190.00",tax_code:"NONE"};
  const valid={...source,lines:[noTax]};
  assert.equal(validateInternalInvoiceSource(valid,{tenant,document_id}).reviewable,true);
});

class Element {
  constructor(ownerDocument,tag){this.ownerDocument=ownerDocument;this.tagName=tag;this.children=[];this.attributes={};this.textContent="";this.listeners={};this.disabled=false;this.checked=false;}
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,cb){this.listeners[type]=cb;}
}
const document={createElement(tag){return new Element(document,tag);}};
test("Arabic private reviewer loads verified line evidence and requires explicit approval click",async()=>{
  const root=document.createElement("main");
  const log=[];
  const page=mountInvoiceApprovalPage({
    root,baseUrl:"https://erp.example.test/",getToken:async()=> "a.b.c",
    fetchImpl:async(url,opts)=>{
      log.push({path:url.pathname,opts});
      return opts.method==="GET"?ok(source):ok({
        ok:true,tenant,document_id,status:"approved_internal_source"
      });
    },
  });
  assert.equal(root.attributes.dir,"rtl");
  assert.equal(await page.open({tenant,document_id}),true);
  assert.equal(log.length,1);assert.equal(log[0].opts.method,"GET");
  const details=root.children[4];
  assert.equal(details.children.length,2);
  const table=details.children[1];
  assert.equal(table.tagName,"table");
  assert.equal(table.children[1].children[1].textContent,"Genuine <strong>source</strong>");
  const approve=root.children[7],confirm=root.children[6];
  assert.equal(root.children[5].attributes.for,confirm.attributes.id);
  assert.match(confirm.attributes.id,/^one-private-approval-ack-[0-9]+$/);
  assert.equal(approve.disabled,true);
  await approve.listeners.click();assert.equal(log.length,1);
  confirm.checked=true;confirm.listeners.change();assert.equal(approve.disabled,false);
  await approve.listeners.click();
  assert.equal(log.length,2);assert.equal(log[1].opts.method,"POST");
  assert.equal(page.snapshot().approved,true);assert.equal(approve.disabled,true);
  page.dispose();
});

test("unreconciled invoice source cannot enable approval even when custom loader returns a trusted marker",async()=>{
  const root=document.createElement("main");
  let calls=0;
  const page=mountInvoiceApprovalPage({root,baseUrl:"https://erp.example.test/",
    getToken:async()=> "a.b.c",
    loadSummary:async()=>({...source,lines:[{...lines[0],gross:"999999.99"}]}),
    fetchImpl:async()=>{calls++;return ok({ok:true,status:"approved_internal_source",tenant,document_id});}
  });
  assert.equal(await page.open({tenant,document_id}),false);
  assert.equal(page.snapshot().ready,false);
  const approve=root.children[7],ack=root.children[6];
  ack.checked=true;ack.listeners.change();
  assert.equal(approve.disabled,true);
  await approve.listeners.click();
  assert.equal(calls,0);
  page.dispose();
});

test("stale browser source denied and clear stops approvals before server POST",async()=>{
  const root=document.createElement("main");
  let resolveFetch,calls=0;
  const page=mountInvoiceApprovalPage({
    root,baseUrl:"https://erp.example.test/",getToken:async()=> "a.b.c",
    fetchImpl:()=>{calls++;return new Promise(resolve=>{resolveFetch=resolve;});}
  });
  const pending=page.open({tenant,document_id});
  await Promise.resolve();await Promise.resolve();
  page.clear();
  if(resolveFetch)resolveFetch(ok(source));
  assert.equal(await pending,false);
  assert.equal(page.snapshot().ready,false);
  assert.equal(root.children[7].disabled,true);
  assert.equal(calls,1);
  page.dispose();
});
