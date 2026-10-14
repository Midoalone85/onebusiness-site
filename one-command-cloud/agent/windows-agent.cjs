// ONE COMMAND Windows Agent: outbound-only, allowlisted read-only checks.
'use strict';
const os=require('node:os'),fs=require('node:fs'),net=require('node:net'),path=require('node:path'),{execFile,spawn}=require('node:child_process');
const config=JSON.parse(fs.readFileSync(path.join(__dirname,'config.json'),'utf8'));
const base=new URL(config.url);if(base.protocol!=='https:'||!config.token||config.token.length<32)throw Error('Secure configuration required');
let running=false;
async function api(p,body){const c=new AbortController();const t=setTimeout(()=>c.abort(),7000);try{let r=await fetch(new URL(p,base),{method:body?'POST':'GET',headers:{Authorization:'Bearer '+config.token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:c.signal});if(!r.ok)throw Error('API HTTP '+r.status);return await r.json()}finally{clearTimeout(t)}}
function tcp(port){return new Promise(resolve=>{let s=net.connect({host:'127.0.0.1',port});const done=ok=>{s.destroy();resolve(ok)};s.setTimeout(1500);s.once('connect',()=>done(true));s.once('timeout',()=>done(false));s.once('error',()=>done(false))})}
async function httpCheck(port,url){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3500);try{const r=await fetch('http://127.0.0.1:'+port+url,{method:'HEAD',signal:controller,redirect:'manual'});return {status:r.status,description:r.status<400?'HTTP response':'HTTP error'}}catch(e){return {status:null,description:e.name==='AbortError'?'HTTP timeout':'Connection failed'}}finally{clearTimeout(timer)}}
function oracleTcp(){return new Promise(resolve=>{const socket=net.connect({host:'141.147.137.107',port:22});let finished=false;const complete=(ok,detail)=>{if(finished)return;finished=true;socket.destroy();resolve({sshTcpReachable:ok,detail,host:'141.147.137.107',port:22,checkedAt:new Date().toISOString(),note:'TCP only. Does not authenticate or execute commands.'})};socket.setTimeout(3500);socket.once('connect',()=>complete(true,'TCP 22 reachable'));socket.once('timeout',()=>complete(false,'Connection timeout'));socket.once('error',e=>complete(false,e.code||'Connection error'))})}
function oracleHealth(){return new Promise(resolve=>{
 // Inspect the real ONE ERP Docker services. Host-level nginx may be absent by design.
 const command="hostname; uptime -p; df -P / | tail -n 1; free -m | grep Mem:; sudo -n docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null || true";
 const args=['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5','oneerp',command];
 execFile('ssh',args,{timeout:15000,windowsHide:true,maxBuffer:12000},(err,stdout)=>{
  if(err)return resolve({connected:false,reason:'SSH inaccessible or host verification failed',checkedAt:new Date().toISOString()});
  const lines=String(stdout).trim().split(/\r?\n/),disk=(lines[2]||'').trim().split(/\s+/),mem=(lines[3]||'').trim().split(/\s+/);
  const names=['oneerp-v3','oneerp-db','oneerp-v3-https','oneerp-v3-public','oneerp-v3-acme','oneerp-v3-gzip'];
  const found=new Map(lines.slice(4).map(line=>{const m=line.trim().match(/^(\S+)\s+(.+)$/);return m?[m[1],m[2]]:[]}).filter(x=>x.length===2));
  const services=Object.fromEntries(names.map(name=>[name,{running:found.has(name)&&/^Up\b/.test(found.get(name)),status:found.get(name)||'not shown as running'}]));
  resolve({connected:true,host:'oneerp',hostname:(lines[0]||'').slice(0,100),uptime:(lines[1]||'').slice(0,160),diskUsePercent:Number.parseInt((disk[4]||'').replace('%',''),10)||null,freeDiskKB:Number(disk[3])||null,memoryAvailableMB:Number(mem[6])||null,services,serviceSource:'docker ps (running containers only)',checkedAt:new Date().toISOString(),readonly:true});
 })
})}
function launchAllowedApp(kind){
 return new Promise(resolve=>{
  if(process.platform!=='win32')return resolve({requested:false,error:'Windows required'});
  const exe=kind==='windows_notepad'?'notepad.exe':'calc.exe';
  try{
   const child=spawn(exe,[],{detached:true,stdio:'ignore',windowsHide:false});
   child.once('error',()=>resolve({requested:false,app:exe,error:'Could not launch approved app'}));
   child.once('spawn',()=>{child.unref();resolve({requested:true,app:exe,note:'Launch request accepted; visible window not independently verified'})});
  }catch{resolve({requested:false,app:exe,error:'Process launch failed'})}
 })
}
async function execute(j){if(j.kind==='windows_notepad'||j.kind==='windows_calculator')return await launchAllowedApp(j.kind);if(j.kind==='oracle_health')return await oracleHealth();if(j.kind==='oracle_network')return await oracleTcp();
if(j.kind==='inventory'){const out={hostname:os.hostname(),platform:os.platform(),release:os.release(),arch:os.arch(),uptimeHours:Math.round(os.uptime()/360)/10,freeMemoryGB:Math.round(os.freemem()/1e8)/10,totalMemoryGB:Math.round(os.totalmem()/1e8)/10,cpus:os.cpus().length,at:new Date().toISOString()};try{const stat=fs.statfsSync(os.homedir());out.homeFreeGB=Math.round(stat.bfree*stat.bsize/1e8)/10}catch{}return out}
if(j.kind==='erp_ports'){let out={at:new Date().toISOString(),checks:[]};for(const [port,page] of [[17881,'/odoo/action-716'],[8069,'/web/login']]){let open=await tcp(port);out.checks.push({port,tcp:open,http:open?await httpCheck(port,page):{status:null,description:'TCP closed'}})}return out}throw Error('Unknown job')}
async function poll(){if(running)return;running=true;try{const {job}=await api('/agent/poll');if(job&&['inventory','erp_ports','oracle_network','oracle_health','windows_notepad','windows_calculator'].includes(job.kind)&&typeof job.id==='string'){if(job.kind==='windows_notepad'||job.kind==='windows_calculator'){const permit=await api('/agent/authorize',{id:job.id,kind:job.kind});if(permit.ok!==true)throw Error('Action authorization denied')}
const data=await execute(job);await api('/agent/report',{id:job.id,kind:job.kind,data});console.log('Completed '+job.kind+' at '+new Date().toISOString())}}catch(e){console.log('Connection check: '+e.message)}finally{running=false}}
console.log('ONE COMMAND read-only agent running. No remote shell enabled.');
poll();setInterval(poll,6000);
process.on('SIGINT',()=>process.exit(0));