import test from "node:test";
import assert from "node:assert/strict";
import {createOperationsView} from "./operations-view.mjs";
const tenant="11111111-1111-4111-8111-111111111111";
const token="a.b.c";
const ok=(module,items=[])=>({ok:true,status:200,headers:{get:()=>"application/json"},
  json:async()=>({module,tenant_id:tenant,items,
    page:{limit:25,offset:0,has_more:false,next_offset:null},
    source:"one_erp_next_postgresql",read_only:true})});
const denied={ok:false,status:403,headers:{get:()=>"application/json"}};
test("loads authenticated actual source items without demo data",async()=>{
  let path;
  const view=createOperationsView({baseUrl:"https://erp.example.test/",tenant,
    getToken:async()=>token,fetchImpl:async url=>{path=url.pathname;return ok("sales",[{number:"SO1"}]);}});
  await view.load("sales");
  assert.equal(path,"/api/v1/operations/sales");
  assert.equal(view.snapshot().status,"ready");
  assert.equal(view.snapshot().data.items[0].number,"SO1");
});
test("failed tenant authorization immediately clears the previous company data",async()=>{
  let calls=0;
  const view=createOperationsView({baseUrl:"https://erp.example.test/",tenant,
    getToken:async()=>token,
    fetchImpl:async()=>++calls===1?ok("sales",[{id:"secret"}]):denied});
  await view.load("sales");await view.load("inventory");
  assert.equal(view.snapshot().data,null);
  assert.equal(view.snapshot().error,"unauthorized");
});
test("clear and new navigation invalidates inflight responses",async()=>{
  let resolve;
  const view=createOperationsView({baseUrl:"https://erp.example.test/",tenant,
    getToken:async()=>token,
    fetchImpl:()=>new Promise(r=>{resolve=r;})});
  const pending=view.load("returns");await Promise.resolve();await Promise.resolve();
  view.clear();
  if(resolve)resolve(ok("returns",[{id:"private"}]));
  await pending;
  assert.equal(view.snapshot().data,null);
  assert.equal(view.snapshot().status,"idle");
});
