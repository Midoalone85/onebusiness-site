import test from "node:test";
import assert from "node:assert/strict";
import { createOperationsView } from "./operations-view.mjs";
const token="a.b.c";
const success=data=>({ok:true,status:200,headers:{get:()=>"application/json"},json:async()=>data});
const denied=()=>({ok:false,status:403,headers:{get:()=>"application/json"}});
test("loads authenticated records without inserting demo data",async()=>{
  const view=createOperationsView({baseUrl:"https://erp.example.test/",getToken:async()=>token,
    fetchImpl:async()=>success({rows:[{id:"sale-1"}]})});
  await view.load("sales");
  assert.deepEqual(view.snapshot(),{status:"ready",module:"sales",data:{rows:[{id:"sale-1"}]},error:null});
});
test("denied user never sees prior company records",async()=>{
  let n=0;
  const view=createOperationsView({baseUrl:"https://erp.example.test/",getToken:async()=>token,
    fetchImpl:async()=>++n===1?success({rows:[{id:"private"}]}):denied()});
  await view.load("sales");await view.load("inventory");
  assert.equal(view.snapshot().data,null);
  assert.equal(view.snapshot().error,"unauthorized");
});
test("clear erases client-side records and invalidates inflight requests",async()=>{
  let resolve;
  const view=createOperationsView({baseUrl:"https://erp.example.test/",getToken:async()=>token,
    fetchImpl:()=>new Promise(r=>{resolve=r})});
  const pending=view.load("returns");await Promise.resolve();await Promise.resolve();
  view.clear();
  if(resolve)resolve(success({rows:[{id:"private"}]}));
  await pending;
  assert.equal(view.snapshot().data,null);
  assert.equal(view.snapshot().status,"idle");
});
