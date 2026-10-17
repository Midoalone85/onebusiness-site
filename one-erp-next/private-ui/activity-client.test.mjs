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

test("commercial order activity requires canonical signed references",async()=>{
  for(const record_type of ["sales_order","purchase_order"]){
    const expected={...body,record_type,items:[{...item,action:record_type+".confirmed"}]};
    const result=await readActivity({...args,record_type,
      fetchImpl:async(url)=>{
        assert.equal(url.searchParams.get("record_type"),record_type);
        return ok(expected);
      }});
    assert.equal(result.items[0].action,record_type+".confirmed");
  }
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

test("request before-after values only for specifically authorized order document",async()=>{
  const data={...body,record_type:"sales_order",items:[{...item,
    event_id:"change:"+record_id,action:"sales_order.line_changed",
    changes:{before:{ordered_qty:"5.000",unit_price:"10.000"},
      after:{ordered_qty:"7.000",unit_price:"12.000"}}}]};
  let url;
  const result=await readActivity({...args,record_type:"sales_order",
    include_changes:true,fetchImpl:async(u)=>{url=u;return ok(data);}});
  assert.equal(url.searchParams.get("include_changes"),"1");
  assert.equal(result.items[0].changes.after.ordered_qty,"7.000");
  await assert.rejects(readActivity({...args,record_type:"sales_order",
    fetchImpl:async()=>ok(data)}),ActivityReadError);
  for(const invalid of [
    {...data,items:[{...data.items[0],
      changes:{before:{secret:"bad"},after:{}}}]},
    {...data,items:[{...data.items[0],
      changes:{before:[],after:{}}}]},
    {...data,items:[{...data.items[0],
      changes:{before:{unit_price:{nested:"bad"}},after:{}}}]},
  ]){
    await assert.rejects(readActivity({...args,record_type:"sales_order",
      include_changes:true,fetchImpl:async()=>ok(invalid)}),ActivityReadError);
  }
  await assert.rejects(readActivity({...args,include_changes:true,
    fetchImpl:async()=>ok(body)}),TypeError);
});
