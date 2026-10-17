import test from "node:test";
import assert from "node:assert/strict";
import {mountOperationsPage} from "./operations-page.mjs";
const tenant="11111111-1111-4111-8111-111111111111";
class Element{
  constructor(ownerDocument,tag){this.ownerDocument=ownerDocument;this.tagName=tag;
    this.children=[];this.attributes={};this.textContent="";this.listeners={};}
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(type,cb){this.listeners[type]=cb;}
}
const document={createElement(tag){return new Element(document,tag)}};
const response=(module,items)=>({ok:true,status:200,headers:{get:()=>"application/json"},
  json:async()=>({module,tenant_id:tenant,items,
    page:{limit:25,offset:0,has_more:false,next_offset:null},read_only:true,
    source:"one_erp_next_postgresql"})});
test("renders actual source items safely as DOM text-only Arabic RTL",async()=>{
  const root=document.createElement("main");
  const page=mountOperationsPage({root,tenant,language:"ar",baseUrl:"https://erp.example.test/",
    getToken:async()=>"a.b.c",fetchImpl:async()=>response("sales",[
      {customer:"<img src=x onerror=alert(1)>",total:"100"}])});
  await page.load("sales");
  assert.equal(root.attributes.dir,"rtl");
  const table=root.children[3].children[0];
  assert.equal(table.tagName,"table");
  assert.equal(table.children[1].children[0].children[0].textContent,
    "<img src=x onerror=alert(1)>");
  page.dispose();assert.equal(root.children.length,0);
});
test("English navigation is compact and shows only four operational modules",()=>{
  const root=document.createElement("main");
  const page=mountOperationsPage({root,tenant,language:"en",baseUrl:"https://erp.example.test/",
    getToken:async()=>"a.b.c"});
  assert.equal(root.attributes.dir,"ltr");
  assert.deepEqual(root.children[0].children.map(el=>el.textContent),
    ["Sales","Purchases","Inventory","Returns"]);
  page.dispose();
});
test("denial immediately removes displayed company data",async()=>{
  let count=0;const root=document.createElement("main");
  const page=mountOperationsPage({root,tenant,baseUrl:"https://erp.example.test/",
    getToken:async()=>"a.b.c",
    fetchImpl:async()=>++count===1?response("sales",[{id:"private"}]):
      ({ok:false,status:403})});
  await page.load("sales");await page.load("inventory");
  assert.equal(root.children[3].children.length,0);
  assert.equal(root.children[2].textContent,"لا تملك صلاحية الاطلاع على هذه البيانات");
  page.dispose();
});

test("commercial order button reads persisted confirmation activity with independent API access",async()=>{
  const root=document.createElement("main");
  const order="22222222-2222-4222-8222-222222222222";
  let reads=[];
  const page=mountOperationsPage({root,tenant,language:"ar",baseUrl:"https://erp.example.test/",
    getToken:async()=>"a.b.c",fetchImpl:async(url,options)=>{
      reads.push({path:url.pathname,method:options.method,record:url.searchParams.get("record_id")});
      if(url.pathname==="/api/v1/operations/sales")return response("sales",[
        {order_id:order,number:"SO-17",status:"confirmed"},
        {order_id:"not-a-real-reference",number:"ignored"}]);
      if(url.pathname==="/api/v1/activity/timeline")return {ok:true,status:200,
        headers:{get:()=>"application/json"},json:async()=>({
          module:"activity",tenant_id:tenant,record_type:"sales_order",record_id:order,
          items:[{event_id:"confirmed:"+order,action:"sales_order.confirmed",
            actor_id:tenant,occurred_at:"2026-10-17T18:00:00.000000Z"}],
          page:{limit:20,offset:0,has_more:false,next_offset:null},
          source:"one_erp_next_postgresql",read_only:true})};
      return {ok:false,status:403};
    }});
  assert.equal(await page.load("sales"),true);
  const table=root.children[3].children[0],historyRoot=root.children[3].children[1];
  assert.equal(historyRoot.hidden,true);
  assert.equal(table.children[0].children[0].children.at(-1).textContent,"سجل العمليات");
  assert.equal(table.children[1].children[1].children.at(-1).children.length,0);
  table.children[1].children[0].children.at(-1).children[0].listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(historyRoot.hidden,false);
  assert.equal(historyRoot.children[3].children.length,1);
  assert.equal(historyRoot.children[3].children[0].children[0].textContent,"تأكيد أمر البيع");
  assert.deepEqual(reads.map(r=>r.path),[
    "/api/v1/operations/sales","/api/v1/activity/timeline"]);
  assert.equal(reads[1].record,order);assert.equal(reads[1].method,"GET");
  page.clear();assert.equal(root.children[3].children.length,0);
  page.dispose();
});

test("commercial history is hidden if source rows lack canonical order UUID",async()=>{
  const root=document.createElement("main");
  const page=mountOperationsPage({root,tenant,baseUrl:"https://erp.example.test/",
    getToken:async()=>"a.b.c",fetchImpl:async()=>response("purchases",[
      {order_id:"bad",number:"PO-1"}])});
  assert.equal(await page.load("purchases"),true);
  assert.equal(root.children[3].children.length,1);
  page.dispose();
});
