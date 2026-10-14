'use strict';
const assert=require('node:assert/strict'),crypto=require('node:crypto'),{spawn}=require('node:child_process');
const pwd='ONLY_LOCAL_SESSION_APPROVAL_TEST',agent='LOCAL_TEST_AGENT_TOKEN_000000000000000001';
const port=39865,base='http://127.0.0.1:'+port;
const env={...process.env,PORT:String(port),OC_ADMIN_HASH:crypto.createHash('sha256').update(pwd).digest('hex'),OC_COOKIE_SECRET:'LOCAL_TEST_COOKIE_SECRET_SAFE_FOR_TESTS_ABCDE',OC_AGENT_TOKEN:agent};
const child=spawn(process.execPath,['server.js'],{cwd:__dirname,env,windowsHide:true,stdio:'ignore'});
const pause=n=>new Promise(r=>setTimeout(r,n));
async function request(uri,method='GET',payload,cookie='',isAgent=false){
 const response=await fetch(base+uri,{method,headers:{'content-type':'application/json',...(cookie?{cookie}:{}),...(isAgent?{authorization:'Bearer '+agent}:{})},body:payload===undefined?undefined:JSON.stringify(payload)});
 return {status:response.status,data:await response.json(),cookie:response.headers.get('set-cookie')};
}
const post=(path,payload,cookie)=>request(path,'POST',payload,cookie);
async function main(){
 try{
  let up=false;for(let i=0;i<50;i++){try{up=(await request('/healthz')).status===200;if(up)break}catch{}await pause(100)}assert.ok(up);
  const login1=await post('/api/login',{password:pwd});const cookie=login1.cookie.split(';')[0];
  const login2=await post('/api/login',{password:pwd});const other=login2.cookie.split(';')[0];
  assert.equal((await post('/api/control/start',{duration:'hour',target:'computer'},cookie)).status,200);
  const original=await post('/api/assist/preview',{text:'افتح المفكرة'},cookie);
  assert.equal(original.status,200);
  assert.equal((await post('/api/assist/execute',{text:'افتح المفكرة',approved:true,requestId:original.data.requestId},other)).status,403,'other login session cannot use preview');
  assert.equal((await post('/api/assist/execute',{text:'افتح الآلة الحاسبة',approved:true,requestId:original.data.requestId},cookie)).status,403,'cannot swap command');
  assert.equal((await post('/api/assist/execute',{text:'افتح المفكرة',approved:false,requestId:original.data.requestId},cookie)).status,403,'explicit consent is mandatory');
  let polled=[];for(let i=0;i<4;i++){const x=await request('/agent/poll','GET',undefined,'',true);polled.push(x.data.job.kind)}
  assert.deepEqual(polled,['inventory','erp_ports','oracle_network','oracle_health']);
  const approved=await post('/api/assist/execute',{text:'افتح المفكرة',approved:true,requestId:original.data.requestId},cookie);
  assert.equal(approved.status,200);
  assert.equal((await post('/api/assist/execute',{text:'افتح المفكرة',approved:true,requestId:original.data.requestId},cookie)).status,403,'one-time preview is used');
  const job=(await request('/agent/poll','GET',undefined,'',true)).data.job;
  assert.equal(job.kind,'windows_notepad');
  assert.equal((await request('/agent/report','POST',{id:job.id,kind:job.kind,data:{requested:true}},'',true)).status,409,'agent cannot report an unapproved executable job');
  assert.equal((await request('/agent/authorize','POST',{id:job.id,kind:job.kind},'',true)).status,200,'agent permit');
  assert.equal((await request('/agent/authorize','POST',{id:job.id,kind:job.kind},'',true)).status,409,'permit only once');
  assert.equal((await request('/agent/report','POST',{id:job.id,kind:job.kind,data:{requested:true}},'',true)).status,200);
  const next=await post('/api/assist/preview',{text:'افتح الآلة الحاسبة'},cookie);
  assert.equal(next.status,200);
  assert.equal((await post('/api/assist/execute',{text:'افتح الآلة الحاسبة',approved:true,requestId:next.data.requestId},cookie)).status,200);
  const cal=(await request('/agent/poll','GET',undefined,'',true)).data.job;assert.equal(cal.kind,'windows_calculator');
  assert.equal((await post('/api/control/stop',{},cookie)).status,200);
  assert.equal((await request('/agent/authorize','POST',{id:cal.id,kind:cal.kind},'',true)).status,409,'stop invalidates agent action');
  console.log('PASS: single-use approval, browser-session binding, no swap, final agent permit, no replay, stop invalidation');
 }finally{child.kill()}
}
main().catch(err=>{console.error('FAIL:',err.stack||err);child.kill();process.exitCode=1});