import test from "node:test";
import assert from "node:assert/strict";
import {mountPrivateFinanceWorkspace} from "./finance-workspace.mjs";

const tenant="11111111-1111-4111-8111-111111111111";
const document_id="22222222-2222-4222-8222-222222222222";
const as_of="2026-10-17";
class Element{
  constructor(ownerDocument,tag){
    this.ownerDocument=ownerDocument;this.tagName=tag;
    this.children=[];this.attributes={};this.textContent="";this.listeners={};
    this.value="";this.checked=false;this.disabled=false;this.hidden=false;
  }
  setAttribute(key,value){this.attributes[key]=value;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(event,handler){this.listeners[event]=handler;}
}
const document={createElement(tag){return new Element(document,tag);}};
const success=body=>({ok:true,status:200,
  headers:{get:()=>"application/json; charset=utf-8"},
  json:async()=>body});
function operations(module){
  return {module,tenant_id:tenant,items:[{number:"SO-REAL-01",total:"100.00"}],
    page:{limit:25,offset:0,has_more:false,next_offset:null},
    source:"one_erp_next_postgresql",read_only:true};
}
const receivables={module:"receivables",tenant_id:tenant,as_of,items:[],
  page:{limit:25,offset:0,has_more:false,next_offset:null},
  source:"one_erp_next_postgresql",read_only:true};
function harness(language="ar",override={}){
  const root=document.createElement("div"),calls=[];
  const workspace=mountPrivateFinanceWorkspace({
    root,tenant,as_of,language,baseUrl:"https://erp.example.test/",
    getToken:async()=> "a.b.c",
    fetchImpl:async(url,opts)=>{
      calls.push({url:url.href,method:opts.method,headers:opts.headers});
      const mod=url.pathname.split("/").at(-1);
      if(["sales","purchases","inventory","returns"].includes(mod))return success(operations(mod));
      if(mod==="receivables")return success(receivables);
      return {ok:false,status:403,headers:{get:()=>"application/json"}};
    },
    ...override,
  });
  return {root,workspace,calls};
}
test("full private Arabic navigation opens separate module pages under one compact menu",async()=>{
  const {root,workspace,calls}=harness();
  assert.equal(root.attributes.dir,"rtl");
  assert.deepEqual(root.children[1].children.map(x=>x.textContent),[
    "المبيعات","المشتريات","المخزون","المرتجعات","أعمار الديون","مراجعة الفواتير",
  ]);
  assert.equal(await workspace.select("sales"),true);
  assert.equal(workspace.snapshot().active,"sales");
  assert.equal(calls[0].method,"GET");
  assert.match(calls[0].url,/tenant=11111111/);
  const page=root.children[3].children[0];
  assert.equal(page.attributes["aria-label"],"المبيعات");
  assert.equal(page.children[0].hidden,true);
  assert.equal(page.children[3].children[0].tagName,"table");
  assert.equal(await workspace.select("inventory"),true);
  assert.equal(workspace.snapshot().active,"inventory");
  assert.equal(root.children[3].children.length,1);
  assert.equal(calls.length,2);
  workspace.clear();
  assert.equal(root.children[3].children.length,0);
  assert.equal(workspace.snapshot().active,null);
  workspace.dispose();assert.equal(root.children.length,0);
});
test("receivables view is source-only, has currency note and is read-only",async()=>{
  const {root,workspace,calls}=harness("en");
  assert.equal(root.attributes.dir,"ltr");
  assert.equal(await workspace.select("receivables"),true);
  const panel=root.children[3].children[0];
  assert.equal(panel.attributes["aria-label"],"Receivables aging");
  assert.match(panel.children[1].textContent,/NOT verified bank deposits/);
  assert.equal(calls.length,0);
  await workspace.select("sales");
  assert.equal(calls.length,1);
  workspace.dispose();
});
test("invoice review cannot approve until protected API provides source and reviewer acknowledges",async()=>{
  const {root,workspace,calls}=harness();
  await workspace.select("review");
  const panel=root.children[3].children[0],controls=panel.children[0];
  const input=controls.children[1],open=controls.children[2];
  input.value="not-a-document";
  open.listeners.click();
  assert.equal(calls.length,0);
  assert.match(controls.children[3].textContent,/غير صالح/);
  input.value=document_id;
  open.listeners.click();
  await Promise.resolve();await Promise.resolve();
  assert.equal(calls.length,1);
  assert.equal(calls[0].method,"GET");
  assert.match(calls[0].url,/internal-invoice-source/);
  assert.equal(workspace.snapshot().active,"review");
  await workspace.select("returns");
  assert.equal(workspace.snapshot().active,"returns");
  workspace.dispose();
});
test("no accepted UI state for invalid company, missing token or malformed date",()=>{
  for(const cfg of [{tenant:"bogus"},{as_of:"2026-02-30"},{getToken:null}]){
    const root=document.createElement("main");
    assert.throws(()=>mountPrivateFinanceWorkspace({
      root,baseUrl:"https://erp.example.test/",tenant,as_of,
      getToken:async()=> "a.b.c",...cfg
    }),TypeError);
  }
});
test("any stopped workspace blocks stale module transitions",async()=>{
  let resolver;
  const {root,workspace}=harness("en",{getToken:()=>new Promise(resolve=>{resolver=resolve;})});
  const pending=workspace.select("sales");
  await Promise.resolve();await Promise.resolve();
  workspace.dispose();
  resolver?.("a.b.c");
  assert.equal(await pending,false);
  assert.equal(workspace.snapshot().disposed,true);
  assert.equal(root.children.length,0);
});
