'use strict';
const {spawn}=require('node:child_process'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const password='LOCAL_TEST_ONLY_not_a_real_key',token='LOCAL_AGENT_TEST_ONLY_abcdefghijklmnopqrstuvwxyz123456';
const port=39841,base='http://127.0.0.1:'+port;
const proc=spawn(process.execPath,['server.js'],{cwd:__dirname,env:{...process.env,PORT:String(port),OC_ADMIN_HASH:crypto.createHash('sha256').update(password).digest('hex'),OC_COOKIE_SECRET:'LOCAL_TEST_COOKIE_SIGNER_SUITABLY_LONG_STRING_ONLY',OC_AGENT_TOKEN:token,OPENAI_API_KEY:''},windowsHide:true,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function req(path,opt={}){const r=await fetch(base+path,opt);return {code:r.status,data:await r.json(),cookie:r.headers.get('set-cookie')}}
const post=(path,obj={},cookie='',origin)=>req(path,{method:'POST',headers:{'content-type':'application/json',...(cookie?{cookie}:{}),...(origin?{origin}:{})},body:JSON.stringify(obj)});
async function main(){
 try{
  let ready=false;for(let i=0;i<30;i++){try{const r=await req('/healthz');ready=r.code===200;if(ready)break}catch{}await sleep(120)}assert.ok(ready,'service starts');
  assert.equal((await req('/api/state')).code,401);
  assert.equal((await post('/api/login',{password:'no'})).code,401);
  let login=await post('/api/login',{password});assert.equal(login.code,200);
  const cookie=login.cookie.split(';')[0];
  assert.equal((await req('/api/state',{headers:{cookie}})).code,200);
  assert.equal((await post('/api/control/start',{},cookie,'https://evil.example')).code,403);
  assert.equal((await post('/api/changes',{title:'Secret change',target:'ONE ERP',details:'Do something sensitive'})).code,401);
  assert.equal((await post('/api/changes',{title:'a',target:'Oracle',details:'too short'},cookie)).code,400);
  const change=await post('/api/changes',{title:'Review accounting workflow',target:'ONE ERP',details:'Review invoice reporting and create a test plan only.'},cookie);
  assert.equal(change.code,201);assert.equal(change.data.executable,false);
  let changes=await req('/api/state',{headers:{cookie}});
  assert.equal(changes.data.changes.length,1);assert.equal(changes.data.changes[0].status,'pending_review');
  let review=await post('/api/changes/review',{id:change.data.id,status:'reviewed'},cookie);
  assert.equal(review.code,200);assert.equal(review.data.executable,false);
  assert.equal((await post('/api/changes/review',{id:change.data.id,status:'reviewed'},cookie)).code,409);
  assert.equal((await post('/api/changes/check',{id:change.data.id},cookie)).code,409,'no diagnostics before session started');
  const backup=await post('/api/changes/import',{changes:[{id:'123e4567-e89b-42d3-a456-426614174000',title:'Restore sample request',target:'Oracle',details:'Inspect the instance readiness without altering production state.',status:'reviewed',created:new Date().toISOString()}]},cookie);
  assert.equal(backup.code,200);assert.equal(backup.data.imported,1);
  assert.equal((await post('/api/changes/import',{changes:[{id:'bad',title:'bad',target:'Oracle',details:'request too small',status:'reviewed',created:new Date().toISOString()}]},cookie)).code,400);

  assert.equal((await post('/api/control/start',{},cookie)).code,200);
  let invalid=await post('/api/changes/check',{id:'123e4567-e89b-42d3-a456-426614174000'},cookie);
  assert.equal(invalid.code,409,'agent must be online before task checks');

  let jobs=[];
  for(let i=0;i<2;i++){const j=await req('/agent/poll',{headers:{authorization:'Bearer '+token}});assert.equal(j.code,200);jobs.push(j.data.job)}
  assert.deepEqual(jobs.map(j=>j.kind),['inventory','erp_ports']);
  let extra=await req('/agent/poll',{headers:{authorization:'Bearer '+token}});assert.equal(extra.data.job.kind,'oracle_network');
  let oracle=await req('/agent/poll',{headers:{authorization:'Bearer '+token}});assert.equal(oracle.data.job.kind,'oracle_health');

  let rep=await req('/agent/report',{method:'POST',headers:{'authorization':'Bearer '+token,'content-type':'application/json'},body:JSON.stringify({id:jobs[0].id,kind:jobs[0].kind,data:{platform:'test',totalMemoryGB:8,freeMemoryGB:5}})});
  assert.equal(rep.code,200);
  let state=await req('/api/state',{headers:{cookie}});assert.equal(state.data.results.length,1);assert.ok(state.data.diagnosis.ok.length>0);
  assert.equal((await post('/api/control/stop',{},cookie)).code,200);
  assert.equal((await post('/api/control/scan',{},cookie)).code,409);
  assert.equal((await post('/api/logout',{},cookie)).code,200);
  assert.equal((await req('/api/state',{headers:{cookie}})).code,401,'logout revoked');
  for(let i=0;i<7;i++)await post('/api/login',{password:'wrong'});
  assert.equal((await post('/api/login',{password})).code,200,'valid owner can recover from bad attempts');
  console.log('PASS: boot, login, origin check, scan, agent, report, stop, logout revocation, lockout recovery, planning-only workflow, backup import, full four-job scan');
 }finally{proc.kill()}
}
main().catch(e=>{console.error('FAIL:',e.stack||e);proc.kill();process.exitCode=1});