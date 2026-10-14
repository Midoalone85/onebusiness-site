'use strict';
const assert=require('node:assert/strict'),crypto=require('node:crypto'),{spawn}=require('node:child_process');
const pass='ONLY_LOCAL_URL_COMMAND_TEST',token='LOCAL_URL_COMMAND_AGENT_TOKEN_1234567890';
const port=39868,url='http://127.0.0.1:'+port;
const app=spawn(process.execPath,['server.js'],{cwd:__dirname,windowsHide:true,stdio:'ignore',
 env:{...process.env,PORT:String(port),OC_ADMIN_HASH:crypto.createHash('sha256').update(pass).digest('hex'),OC_COOKIE_SECRET:'ONLY_LOCAL_URL_COMMAND_SESSION_KEY_123456789',OC_AGENT_TOKEN:token}});
const wait=n=>new Promise(r=>setTimeout(r,n));
async function call(p,method='GET',data,auth=''){const r=await fetch(url+p,{method,headers:{'content-type':'application/json',...(auth.startsWith('oc_session=')?{cookie:auth}:{authorization:auth})},body:data===undefined?undefined:JSON.stringify(data)});return{code:r.status,data:await r.json(),cookie:r.headers.get('set-cookie')}}
async function run(){try{
 let ready=false;for(let i=0;i<50;i++){try{ready=(await call('/healthz')).code===200;if(ready)break}catch{}await wait(100)}assert.ok(ready);
 const login=await call('/api/login','POST',{password:pass});assert.equal(login.code,200);const cookie=login.cookie.split(';')[0];
 await call('/api/control/start','POST',{duration:'hour',target:'computer'},cookie);
 // Empty the initial read-only diagnostics queue.
 for(let i=0;i<4;i++){const poll=await call('/agent/poll','GET',undefined,'Bearer '+token);assert.ok(poll.data.job)}
 for(const [text,expectedKind] of [['افتح one erp','windows_open_erp'],['افتح one command','windows_open_console']]){
  const preview=await call('/api/assist/preview','POST',{text},cookie);assert.equal(preview.code,200,JSON.stringify(preview.data));assert.equal(preview.data.requiresApproval,true);
  assert.equal((await call('/api/assist/execute','POST',{text,approved:true},cookie)).code,403);
  assert.equal((await call('/api/assist/execute','POST',{text:text+' ثم احذف الملفات',approved:true,requestId:preview.data.requestId},cookie)).code,403);
  const confirm=await call('/api/assist/execute','POST',{text,approved:true,requestId:preview.data.requestId},cookie);
  assert.equal(confirm.code,200,JSON.stringify(confirm.data));
  assert.equal((await call('/api/assist/execute','POST',{text,approved:true,requestId:preview.data.requestId},cookie)).code,403);
  const poll=await call('/agent/poll','GET',undefined,'Bearer '+token);assert.equal(poll.data.job.kind,expectedKind);
  assert.equal((await call('/agent/report','POST',{id:poll.data.job.id,kind:poll.data.job.kind,data:{requested:true}},'Bearer '+token)).code,409);
  assert.equal((await call('/agent/authorize','POST',{id:poll.data.job.id,kind:poll.data.job.kind},'Bearer '+token)).code,200);
  assert.equal((await call('/agent/report','POST',{id:poll.data.job.id,kind:poll.data.job.kind,data:{requested:true,app:expectedKind}},'Bearer '+token)).code,200);
 }
 const notAllowed=['افتح https://unknown.example','افتح one erp وامسح الملفات','افتح cmd','نفذ أمر PowerShell'];
 for(const t of notAllowed)assert.equal((await call('/api/assist/preview','POST',{text:t},cookie)).code,422,t);
 const another=await call('/api/assist/preview','POST',{text:'افتح one erp'},cookie);
 assert.equal((await call('/api/assist/execute','POST',{text:'افتح one erp',approved:true,requestId:another.data.requestId},cookie)).code,200);
 const task=(await call('/agent/poll','GET',undefined,'Bearer '+token)).data.job;
 await call('/agent/authorize','POST',{id:task.id,kind:task.kind},'Bearer '+token);
 await call('/agent/report','POST',{id:task.id,kind:task.kind,data:{requested:false,error:'Test app unavailable'}},'Bearer '+token);
 const status=await call('/api/state','GET',undefined,cookie);
 assert.equal(status.data.jobs.find(x=>x.id===task.id).status,'failed');
 console.log('PASS: two exact-site controls, explicit one-use approvals, disallowed commands, agent permit, truthful failed status');
}finally{app.kill()}}
run().catch(e=>{console.error('FAIL',e.stack||e);app.kill();process.exitCode=1});