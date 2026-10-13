'use strict';
const http=require('node:http'),crypto=require('node:crypto'),fs=require('node:fs'),path=require('node:path');
const PORT=Number(process.env.PORT||3000),PW=process.env.OC_ADMIN_HASH||'',KEY=process.env.OC_COOKIE_SECRET||'',AGENT=process.env.OC_AGENT_TOKEN||'';
if(!/^[a-f0-9]{64}$/.test(PW)||KEY.length<32||AGENT.length<32){console.error('Missing OC secrets');process.exit(1)}
const events=[],attempts=new Map();let session=null,queue=[],results=[],lastPing=0,issued=new Map();
const sha=s=>crypto.createHash('sha256').update(s).digest('hex'),mac=s=>crypto.createHmac('sha256',KEY).update(s).digest('hex');
const eq=(a,b)=>{let x=Buffer.from(String(a)),y=Buffer.from(String(b));return x.length===y.length&&crypto.timingSafeEqual(x,y)};
function authed(req){let raw=(req.headers.cookie||'').split(';').map(x=>x.trim()).find(x=>x.startsWith('oc_session='))||'';let [exp,sig]=raw.slice(11).split('.');return /^\d+$/.test(exp||'')&&Number(exp)>Date.now()&&eq(mac('owner:'+exp),sig||'')}
function live(){return session&&session.expires>Date.now()}
function evt(s){events.unshift({at:new Date().toISOString(),message:s});events.splice(30)}
function reply(res,c,o,more={}){res.writeHead(c,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff',...more});res.end(JSON.stringify(o))}
async function body(req){return new Promise((resolve,reject)=>{let s='';req.on('data',v=>{s+=v;if(s.length>12000){reject(Error('body too big'));req.destroy()}});req.on('end',()=>{try{resolve(JSON.parse(s||'{}'))}catch{reject(Error('invalid JSON'))}});req.on('error',reject)})}
function allowOrigin(req){if(!req.headers.origin)return true;try{const u=new URL(req.headers.origin);return u.protocol==='https:'&&u.host===req.headers.host}catch{return false}}
function enqueue(kind){const j={id:crypto.randomUUID(),kind,at:Date.now()};queue.push(j);issued.set(j.id,j);if(queue.length>12)queue.shift()}
const server=http.createServer(async(req,res)=>{res.setHeader('X-Frame-Options','DENY');res.setHeader('Referrer-Policy','no-referrer');res.setHeader('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
const p=new URL(req.url,'http://localhost').pathname;
try{
if(p==='/healthz')return reply(res,200,{ok:true,service:'one-command',private:true});
if(p==='/agent/poll'||p==='/agent/report'){
 if(!eq(req.headers.authorization||'','Bearer '+AGENT))return reply(res,401,{error:'agent unauthorized'});
 lastPing=Date.now();
 if(p==='/agent/poll'&&req.method==='GET'){if(!live()){queue=[];return reply(res,200,{job:null})}return reply(res,200,{job:queue.shift()||null})}
 if(p==='/agent/report'&&req.method==='POST'){let d=await body(req),j=issued.get(d.id);
   if(!live()||!j||j.kind!==d.kind||!['inventory','erp_ports','oracle_network'].includes(d.kind))return reply(res,409,{error:'job expired/invalid'});
   if(JSON.stringify(d.data||{}).length>8000)return reply(res,400,{error:'too large'});
   issued.delete(d.id);results.unshift({at:new Date().toISOString(),kind:d.kind,data:d.data});results=results.slice(0,20);evt('تم استلام تقرير '+d.kind);return reply(res,200,{ok:true})}
 return reply(res,405,{error:'method'});
}
if(p==='/api/login'&&req.method==='POST'){
 if(!allowOrigin(req))return reply(res,403,{error:'Origin not accepted'});
 const ip='owner-login-global',a=attempts.get(ip)||{fails:0,blocked:0};
 if(a.blocked>Date.now())return reply(res,429,{error:'انتظر 15 دقيقة قبل المحاولة'});
 const b=await body(req);
 if(typeof b.password!=='string'||!eq(sha(b.password),PW)){a.fails++;if(a.fails>=5){a.blocked=Date.now()+900000;a.fails=0}attempts.set(ip,a);return reply(res,401,{error:'مفتاح الدخول غير صحيح'})}
 attempts.delete(ip);const exp=Date.now()+30*60000;evt('تسجيل دخول المالك');
 return reply(res,200,{ok:true},{'Set-Cookie':'oc_session='+exp+'.'+mac('owner:'+exp)+'; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=1800'})
}
if(p==='/'||p==='/index.html'){res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});return fs.createReadStream(path.join(__dirname,'index.html')).pipe(res)}
if(p==='/app.js'||p==='/style.css'){res.writeHead(200,{'Content-Type':p.endsWith('.js')?'text/javascript; charset=utf-8':'text/css; charset=utf-8','Cache-Control':'no-store'});return fs.createReadStream(path.join(__dirname,p.slice(1))).pipe(res)}
if(!authed(req))return reply(res,401,{error:'الدخول مطلوب'});
if(req.method==='POST'&&!allowOrigin(req))return reply(res,403,{error:'Origin not accepted'});
if(p==='/api/logout'&&req.method==='POST')return reply(res,200,{ok:true},{'Set-Cookie':'oc_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0'});
if(p==='/api/state'&&req.method==='GET')return reply(res,200,{agentOnline:Date.now()-lastPing<20000,active:!!live(),expires:live()?session.expires:null,events,results});
if(p==='/api/control/start'&&req.method==='POST'){session={expires:Date.now()+900000,id:crypto.randomUUID()};queue=[];issued.clear();results=[];enqueue('inventory');enqueue('erp_ports');evt('بدء جلسة فحص لمدة 15 دقيقة');return reply(res,200,{ok:true})}
if(p==='/api/control/scan'&&req.method==='POST'){if(!live())return reply(res,409,{error:'ابدأ الجلسة'});enqueue('inventory');enqueue('erp_ports');evt('طلب فحص جديد');return reply(res,200,{ok:true})}
if(p==='/api/control/oracle'&&req.method==='POST'){if(!live())return reply(res,409,{error:'ابدأ جلسة الفحص أولًا'});enqueue('oracle_network');evt('طلب اختبار اتصال Oracle TCP فقط، دون SSH login');return reply(res,200,{ok:true,scope:'oracle_network'})}
if(p==='/api/control/stop'&&req.method==='POST'){session=null;queue=[];issued.clear();evt('إيقاف الجلسة');return reply(res,200,{ok:true})}
return reply(res,404,{error:'not found'});
}catch(e){console.error(e.message);return reply(res,500,{error:'request failed'})}
});
server.listen(PORT,'0.0.0.0',()=>console.log('ONE COMMAND private console ready on port '+PORT));