import test from "node:test";
import assert from "node:assert/strict";
import {readActivity,ActivityReadError} from "./activity-client.mjs";
const tenant="11111111-1111-4111-8111-111111111111";
const record_id="22222222-2222-4222-8222-222222222222";
const actor="33333333-3333-4333-8333-333333333333";
const item={event_id:"74",action:"invoice.issued",actor_id:actor,
  occurred_at:"2026-10-17T19:00:01.123456Z"};
const body={module:"activity",tenant_id:tenant,record_id,record_type:"invoice",
  items:[item],source:"one_erp_next_postgresql",read_only:true,
  page:{limit:20,offset:0,has_more:false,next_offset:null}};
const ok=(data)=>({ok:true,status:200,headers:{get:()=>"application/json"},
  json:async()=>data});
const args={baseUrl:"https://trusted.example.test/",tenant,record_id,
  record_type:"invoice",token:"a.b.c"};

test("never posts or transmits browser-written action or actor details",async()=>{
  let request;
  const data=await readActivity({...args,fetchImpl:async(url,options)=>{
    request={url,options};return ok(body);
  }});
  assert.equal(data.items[0].actor_id,actor);
  assert.equal(request.url.pathname,"/api/v1/activity/timeline");
  assert.equal(request.url.searchParams.get("record_id"),record_id);
  assert.equal(request.url.searchParams.has("actor_id"),false);
  assert.equal(request.options.method,"GET");
  assert.equal(request.options.body,undefined);
  assert.equal(request.options.credentials,"omit");
  assert.equal(request.options.cache,"no-store");
});

test("reject forged company, actor, source, timestamp or unexpected event",async()=>{
  for(const bad of [
    {...body,tenant_id:record_id},{...body,source:"sample_demo"},
    {...body,record_id:tenant},{...body,record_type:"payment"},
    {...body,read_only:false},{...body,items:[{...item,actor_id:"actor name"}]},
    {...body,items:[{...item,occurred_at:"yesterday"}]},
    {...body,items:[{...item,action:"<script>"}]},
    {...body,page:{...body.page,next_offset:20}},
  ]){
    await assert.rejects(readActivity({...args,fetchImpl:async()=>ok(bad)}),
      ActivityReadError);
  }
});

test("requires canonical references, authorized token and HTTPS before network",async()=>{
  let count=0;const fetchImpl=async()=>{count++;return ok(body);};
  for(const changes of [
    {baseUrl:"http://trusted.example.test/"},
    {baseUrl:"https://trusted.example.test/anything"},
    {token:"invalid"},{tenant:"not-uuid"},{record_type:"any"},
    {record_id:"private"},{"limit":0},{offset:6000},
  ]){
    await assert.rejects(readActivity({...args,...changes,fetchImpl}),TypeError);
  }
  assert.equal(count,0);
});

test("unauthorized, HTML and invalid JSON never become a timeline",async()=>{
  for(const failure of [
    {ok:false,status:403},
    {...ok(body),headers:{get:()=>"text/html"}},
    {...ok(body),json:async()=>{throw Error("decode");}},
  ]){
    await assert.rejects(readActivity({...args,fetchImpl:async()=>failure}),
      ActivityReadError);
  }
});
