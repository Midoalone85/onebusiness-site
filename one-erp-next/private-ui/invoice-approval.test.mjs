import test from "node:test";
import assert from "node:assert/strict";
import {approveInternalInvoice,validDocumentRef,InternalApprovalError} from "./invoice-approval-client.mjs";
import {mountInvoiceApprovalPage} from "./invoice-approval-page.mjs";
const tenant="11111111-1111-4111-8111-111111111111",document_id="22222222-2222-4222-8222-222222222222";
const response={ok:true,status:200,headers:{get:()=>"application/json; charset=utf-8"},
  json:async()=>({ok:true,tenant,document_id,status:"approved_internal_source"})};
test("exactly two references sent via HTTPS, authenticated memory-only token",async()=>{
  let call;
  const result=await approveInternalInvoice({baseUrl:"https://erp.example.test/",tenant,document_id,token:"a.b.c",
    fetchImpl:async(url,options)=>{call={url,options};return response;}});
  assert.equal(call.url.href,"https://erp.example.test/api/v1/finance/internal-invoice-approvals");
  assert.equal(call.options.method,"POST");assert.equal(call.options.credentials,"omit");
  assert.equal(call.options.cache,"no-store");assert.equal(call.options.redirect,"error");
  assert.deepEqual(JSON.parse(call.options.body),{tenant,document_id});
  assert.deepEqual(result,{tenant,document_id,status:"approved_internal_source"});
});
test("rejects unsafe origin, credentials, invalid refs and missing JWT before network",async()=>{
  let count=0,fetchImpl=()=>{count++;return response;};
  for(const baseUrl of ["http://erp.example.test/","https://a.example/path",
      "https://a.example/?x=1","https://user:pw@erp.example.test/","https://a.example/#frag"]){
    await assert.rejects(approveInternalInvoice({baseUrl,tenant,document_id,token:"a.b.c",fetchImpl}),TypeError);
  }
  for(const invalid of [null,"","00000000-0000-0000-0000-000000000000",
      "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA","not-a-uuid"]){
    assert.equal(validDocumentRef(invalid),false);
    await assert.rejects(approveInternalInvoice({baseUrl:"https://a.example/",tenant:invalid,document_id,token:"a.b.c",fetchImpl}),TypeError);
  }
  await assert.rejects(approveInternalInvoice({baseUrl:"https://a.example/",tenant,document_id,token:"bearer token",fetchImpl}),TypeError);
  assert.equal(count,0);
});
test("denied, HTML, malformed, mismatched or failed response never reports success",async()=>{
  for(const fakeResponse of [{ok:false,status:403},{...response,headers:{get:()=>"text/html"}},
    {...response,json:async()=>({...await response.json(),document_id:tenant})},
    {...response,json:async()=>{throw Error("JSON");}}]){
    await assert.rejects(approveInternalInvoice({baseUrl:"https://a.example/",tenant,document_id,token:"a.b.c",
      fetchImpl:async()=>fakeResponse}),InternalApprovalError);
  }
  await assert.rejects(approveInternalInvoice({baseUrl:"https://a.example/",tenant,document_id,token:"a.b.c",
    fetchImpl:async()=>{throw Error("network");}}),InternalApprovalError);
});
class Element {
  constructor(ownerDocument,tag){this.ownerDocument=ownerDocument;this.tagName=tag;this.children=[];this.attributes={};this.textContent="";this.listeners={};this.disabled=false;this.checked=false;}
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,callback){this.listeners[type]=callback;}
}
const document={createElement(tag){return new Element(document,tag);}};
const verifiedLine={
  line_number:1,description:"Validated accounting source",quantity:"1",
  unit_price:"100.00",discount_amount:"0.00",tax_category:"standard",
  tax_rate:"0.150000",tax_code:"KSA_STANDARD",net:"100.00",
  tax:"15.00",gross:"115.00",
};
const summary={tenant,document_id,reviewable:true,
  source:"verified_internal_postgresql",fields:[
  {label:"Customer",value:"<script>no</script>"},{label:"Subtotal",value:"100"}],
  lines:[verifiedLine]};
function setup(overrides={}){
  const root=document.createElement("main");let calls=0;
  const page=mountInvoiceApprovalPage({root,baseUrl:"https://erp.example.test/",language:"ar",
    getToken:async()=> "a.b.c",loadSummary:async()=>summary,fetchImpl:async()=>{calls++;return response;},...overrides});
  return {root,page,calls:()=>calls,approve:root.children[7],confirm:root.children[6],
    status:root.children[3],details:root.children[4]};
}
test("cannot approve before trusted data and explicit independent reviewer acknowledgment",async()=>{
  const o=setup();assert.equal(o.approve.disabled,true);
  assert.equal(await o.page.open({tenant,document_id}),true);
  assert.equal(o.details.children[0].textContent,"Customer: <script>no</script>");
  assert.equal(o.approve.disabled,true);await o.approve.listeners.click();assert.equal(o.calls(),0);
  o.confirm.checked=true;o.confirm.listeners.change();assert.equal(o.approve.disabled,false);
  await o.approve.listeners.click();assert.equal(o.calls(),1);
  assert.equal(o.approve.disabled,true);assert.equal(o.page.snapshot().approved,true);
  await o.approve.listeners.click();assert.equal(o.calls(),1);
  o.page.dispose();assert.equal(o.root.children.length,0);
});
test("untrusted, wrong tenant, unreviewable or empty details fail closed",async()=>{
  for(const wrong of [
    null,{...summary,tenant:document_id},{...summary,reviewable:false},
    {...summary,fields:[]},{...summary,source:"browser_generated"},
    {...summary,source:undefined},{...summary,lines:undefined},
    {...summary,lines:[]},{...summary,lines:[verifiedLine,verifiedLine]},
    {...summary,lines:[{...verifiedLine,gross:115}]},
    {...summary,fields:[{label:"Gross",value:NaN}]},
  ]){
    const o=setup({loadSummary:async()=>wrong});
    assert.equal(await o.page.open({tenant,document_id}),false);
    o.confirm.checked=true;o.confirm.listeners.change();await o.approve.listeners.click();
    assert.equal(o.calls(),0);assert.equal(o.approve.disabled,true);o.page.dispose();
  }
});
test("clear and dispose cancel stale reviews and prevent approvals",async()=>{
  let fulfill;
  const o=setup({loadSummary:()=>new Promise(resolve=>{fulfill=resolve;})});
  const loading=o.page.open({tenant,document_id});
  await Promise.resolve();await Promise.resolve();
  o.page.clear();if(fulfill)fulfill(summary);await loading;
  assert.equal(o.page.snapshot().ready,false);assert.equal(o.details.children.length,0);
  assert.equal(o.calls(),0);o.page.dispose();
});
test("English independent reviewer displays explicit non-ZATCA notice",()=>{
  const o=setup({language:"en"});
  assert.equal(o.root.attributes.dir,"ltr");
  assert.match(o.root.children[1].textContent,/not a tax invoice/);
  o.page.dispose();
});
