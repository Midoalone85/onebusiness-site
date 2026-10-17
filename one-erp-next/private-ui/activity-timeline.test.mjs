import test from "node:test";
import assert from "node:assert/strict";
import {mountActivityTimeline} from "./activity-timeline.mjs";
const tenant="11111111-1111-4111-8111-111111111111";
const record_id="22222222-2222-4222-8222-222222222222";
const actor="33333333-3333-4333-8333-333333333333";
class Element{
  constructor(ownerDocument,tag){
    this.ownerDocument=ownerDocument;this.tagName=tag;this.children=[];
    this.attributes={};this.textContent="";this.listeners={};this.disabled=false;
  }
  setAttribute(k,v){this.attributes[k]=v;}
  replaceChildren(...children){this.children=children;this.textContent="";}
  append(...children){this.children.push(...children);}
  addEventListener(event,listener){this.listeners[event]=listener;}
}
const doc={createElement(tag){return new Element(doc,tag);}};
function fixture(fetchImpl){
  const root=doc.createElement("div"),calls=[];
  const result=mountActivityTimeline({root,baseUrl:"https://erp.example.test/",
    tenant,getToken:async()=> "a.b.c",language:"ar",timeZone:"Asia/Riyadh",
    fetchImpl:async(url,opts)=>{calls.push({url,opts});return fetchImpl(url,opts);}});
  return {root,calls,result};
}
const response=(items)=>({ok:true,status:200,
  headers:{get:()=>"application/json"},json:async()=>({
    module:"activity",record_type:"invoice",record_id,tenant_id:tenant,items,
    page:{limit:20,offset:0,has_more:false,next_offset:null},
    source:"one_erp_next_postgresql",read_only:true
  })});
test("real audit actor/time/action rendered next to approved invoice only",async()=>{
  const {root,calls,result}=fixture(()=>response([{
    event_id:"19",action:"invoice.issued",actor_id:actor,
    occurred_at:"2026-10-17T18:00:00.000000Z"
  }]));
  assert.equal(await result.load("invoice",record_id),true);
  assert.equal(calls.length,1);
  const li=root.children[3].children[0];
  assert.equal(li.children[0].textContent,"إصدار الفاتورة");
  assert.match(li.children[1].textContent,/21:00:00/);
  assert.match(li.children[2].textContent,new RegExp(actor));
  result.dispose();
  assert.equal(root.children.length,0);
});
test("denied access never leaves stale actions visible",async()=>{
  let call=0;
  const {root,result}=fixture(()=>{
    if(++call===1)return response([{event_id:"19",action:"invoice.issued",
      actor_id:actor,occurred_at:"2026-10-17T18:00:00.000000Z"}]);
    return {ok:false,status:403};
  });
  await result.load("invoice",record_id);
  assert.equal(root.children[3].children.length,1);
  assert.equal(await result.load("invoice",record_id),false);
  assert.equal(root.children[3].children.length,0);
  assert.equal(result.snapshot().active,null);
  result.dispose();
});
test("clear cancels stale in-flight responses",async()=>{
  let fulfill;
  const {root,result}=fixture(()=>new Promise(resolve=>{fulfill=resolve;}));
  const pending=result.load("invoice",record_id);
  await Promise.resolve();await Promise.resolve();
  result.clear();
  fulfill?.(response([]));
  assert.equal(await pending,false);
  assert.equal(root.children[3].children.length,0);
  result.dispose();
});

test("manual refresh re-reads server facts without creating local events",async()=>{
  let reads=0;
  const {root,result}=fixture(()=>response(++reads===1?[]:[{
    event_id:"approved:"+record_id,action:"invoice.source_approved",actor_id:actor,
    occurred_at:"2026-10-17T19:00:00.000000Z",
  }]));
  assert.equal(root.children[4].children[2].disabled,true);
  await result.load("invoice",record_id);
  assert.equal(reads,1);assert.equal(root.children[3].children.length,0);
  root.children[4].children[2].listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reads,2);assert.equal(root.children[3].children.length,1);
  result.clear();assert.equal(root.children[4].children[2].disabled,true);
  result.dispose();
});

test("authenticated diff click re-reads database and renders only allowlisted fields",async()=>{
  let reads=0;
  const {root,result,calls}=fixture((url)=>{
    reads++;
    return {ok:true,status:200,headers:{get:()=>"application/json"},
      json:async()=>({
        module:"activity",record_type:"sales_order",record_id,
        tenant_id:tenant,source:"one_erp_next_postgresql",read_only:true,
        page:{limit:20,offset:0,has_more:false,next_offset:null},
        items:[{event_id:"change:"+record_id,action:"sales_order.line_changed",
          actor_id:actor,occurred_at:"2026-10-17T18:00:00.000000Z",
          ...(url.searchParams.get("include_changes")==="1"?{
            changes:{before:{unit_price:"10.000"},
              after:{unit_price:"12.000"}}}:{})}],
      })};
  });
  assert.equal(await result.load("sales_order",record_id),true);
  assert.equal(root.children[3].children[0].children.length,3);
  assert.equal(root.children[4].children[3].disabled,false);
  root.children[4].children[3].listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reads,2);
  assert.equal(calls[1].url.searchParams.get("include_changes"),"1");
  assert.equal(root.children[3].children[0].children.length,4);
  const value=root.children[3].children[0].children[3].children[0].children[1].textContent;
  assert.match(value,/10.000/);assert.match(value,/12.000/);
  assert.equal(root.children[4].children[3].disabled,true);
  result.dispose();
});
