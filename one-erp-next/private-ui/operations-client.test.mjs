import test from "node:test";
import assert from "node:assert/strict";
import { readOperations, OperationsApiError } from "./operations-client.mjs";
const token = "partA.partB.partC";
const response = (body, status=200, contentType="application/json") => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: () => contentType }, json: async () => body,
});
test("fetches real read-only sales API with no credentials or cache", async () => {
  const calls = [];
  const result = await readOperations({baseUrl:"https://erp.example.test/",module:"sales",token,
    fetchImpl:async (...args)=>{calls.push(args); return response({rows:[]})}});
  assert.deepEqual(result,{rows:[]});
  assert.equal(calls[0][0].href,"https://erp.example.test/api/v1/operations/sales");
  assert.equal(calls[0][1].method,"GET");
  assert.equal(calls[0][1].cache,"no-store");
  assert.equal(calls[0][1].credentials,"omit");
  assert.equal(calls[0][1].headers.Authorization,"Bearer "+token);
});
test("refuses unknown or write module without network calls",async()=>{
  await assert.rejects(readOperations({baseUrl:"https://erp.example.test/",module:"payments",token,fetchImpl:()=>{throw Error("called")}}),TypeError);
});
test("rejects HTTP and embedded credentials",async()=>{
  for (const baseUrl of ["http://erp.example.test/","https://u:p@erp.example.test/","https://erp.example.test/other"]) {
    await assert.rejects(readOperations({baseUrl,module:"sales",token,fetchImpl:()=>{throw Error("called")}}),TypeError);
  }
});
test("requires token, never silently falls back to an anonymous sample",async()=>{
  await assert.rejects(readOperations({baseUrl:"https://erp.example.test/",module:"inventory",token:""}),TypeError);
});
test("does not follow redirects or reveal server error bodies",async()=>{
  await assert.rejects(readOperations({baseUrl:"https://erp.example.test/",module:"returns",token,
    fetchImpl:async()=>response({private:"x"},403)}),OperationsApiError);
});
test("rejects non JSON responses",async()=>{
  await assert.rejects(readOperations({baseUrl:"https://erp.example.test/",module:"purchases",token,
    fetchImpl:async()=>response("not-json",200,"text/html")}),OperationsApiError);
});

test("rejects malformed origin before fetching",async()=>{
  await assert.rejects(readOperations({baseUrl:"not-an-origin",module:"sales",token,
    fetchImpl:()=>{throw Error("network should not run")}}),TypeError);
});
test("rejects malformed JSON payload without leaking contents",async()=>{
  await assert.rejects(readOperations({baseUrl:"https://erp.example.test/",module:"sales",token,
    fetchImpl:async()=>({ok:true,status:200,headers:{get:()=>"application/json"},
      json:async()=>{throw SyntaxError("sensitive content")}})}),OperationsApiError);
});
test("passes through cancellation signal to network client",async()=>{
  const controller = new AbortController();
  let received;
  await readOperations({baseUrl:"https://erp.example.test/",module:"inventory",token,
    signal:controller.signal,fetchImpl:async(_url,options)=>{
      received=options.signal;return response({rows:[]});
    }});
  assert.equal(received,controller.signal);
});
