import test from "node:test";
import assert from "node:assert/strict";
import {readOperations,OperationsApiError} from "./operations-client.mjs";
const tenant="11111111-1111-4111-8111-111111111111",token="a.b.c";
const reply=(items=[],module="sales",tenant_id=tenant)=>({
  module,tenant_id,items,page:{limit:25,offset:0,has_more:false,next_offset:null},
  source:"one_erp_next_postgresql",read_only:true
});
const response=(body,status=200,contentType="application/json")=>({
  ok:status>=200&&status<300,status,
  headers:{get:()=>contentType},json:async()=>body
});
const common={baseUrl:"https://erp.example.test/",module:"sales",tenant,token};
test("matches actual finance backend: tenant, pagination, items, read-only and no cookies",async()=>{
  let calls=[];
  let item={number:"SO-001",currency:"SAR",unit_price:"100.00"};
  const data=reply([item]);
  const result=await readOperations({...common,
    fetchImpl:async(...args)=>{calls.push(args);return response(data);}});
  assert.deepEqual(result,data);
  const [url,opts]=calls[0];
  assert.equal(url.pathname,"/api/v1/operations/sales");
  assert.deepEqual([...url.searchParams.keys()],["tenant","limit","offset"]);
  assert.equal(url.searchParams.get("tenant"),tenant);
  assert.equal(url.searchParams.get("limit"),"25");
  assert.equal(url.searchParams.get("offset"),"0");
  assert.equal(opts.method,"GET");assert.equal(opts.cache,"no-store");
  assert.equal(opts.redirect,"error");assert.equal(opts.credentials,"omit");
  assert.equal(opts.headers.Authorization,"Bearer "+token);
});
test("malformed origin, JWT, tenant, pagination and unknown modules denied BEFORE network",async()=>{
  let calls=0,fetchImpl=()=>{calls++;throw Error("must not call")};
  for(const overrides of [
    {baseUrl:"http://erp.example.test/"},{baseUrl:"https://a:pw@erp.example.test/"},
    {baseUrl:"https://erp.example.test/a"},{baseUrl:"ftp://erp.example.test/"},
    {token:""},{tenant:"wrong"},{tenant:"00000000-0000-0000-0000-000000000000"},
    {module:"receivables"},{module:"payments"},{limit:51},{offset:-1},
  ]){
    await assert.rejects(readOperations({...common,fetchImpl,...overrides}),TypeError);
  }
  assert.equal(calls,0);
});
test("rejects unrelated company, mismatched module, malformed metadata or injected record",async()=>{
  for(const bad of [
    reply([],"inventory"),
    reply([],"sales","33333333-3333-4333-8333-333333333333"),
    {...reply(),source:"browser_mock"},
    {...reply(),read_only:false},
    {...reply(),page:{limit:50,offset:0,has_more:false,next_offset:null}},
    reply([{name:"abc",danger:{object:"unsafe"}}]),
  ]){
    await assert.rejects(readOperations({...common,fetchImpl:async()=>response(bad)}),OperationsApiError);
  }
});
test("rejects denied, HTML, malformed JSON and forwards cancellation signal",async()=>{
  for(const fake of [
    response({},403),response("<html>",200,"text/html"),
    {...response({}),json:async()=>{throw SyntaxError("confidential");}},
  ]){
    await assert.rejects(readOperations({...common,fetchImpl:async()=>fake}),OperationsApiError);
  }
  const controller=new AbortController();
  let got;
  await readOperations({...common,signal:controller.signal,
    fetchImpl:async(url,opts)=>{got=opts.signal;return response(reply());}});
  assert.equal(got,controller.signal);
});
