import express from "express";
import cors from "cors";
import { createClient } from "redis";
import crypto from "crypto";

const app = express();
app.use(cors({origin:true,credentials:false}));
app.use(express.json({limit:"1mb"}));

const PORT = process.env.PORT || 10000;
const ADMIN_PASSWORD_HASH = process.env.ADMIN_PASSWORD_HASH || "5d21ee7f78293ef9d6094c37e7e309ecd12ae6cb45f231f31e792f837f135d85";
const adminSessions = new Map();
const REDIS_URL = process.env.REDIS_URL || "";
const WA_TOKEN = process.env.WHATSAPP_TOKEN || "";
const WA_PHONE_ID = process.env.WHATSAPP_PHONE_NUMBER_ID || "";
const WA_TO = process.env.WHATSAPP_ADMIN_TO || "";

let redis = null;
let mem = new Map();
let memStock = new Map();
let memCatalog = null;

async function initRedis(){
  if(!REDIS_URL) return;
  try{
    redis=createClient({url:REDIS_URL});
    redis.on("error",()=>{});
    await redis.connect();
  }catch(e){redis=null;}
}
await initRedis();

function orderKey(id){return "afia:order:"+id}
async function saveOrder(o){
  if(redis){await redis.set(orderKey(o.id),JSON.stringify(o));await redis.zAdd("afia:orders",{score:Date.parse(o.createdAt)||Date.now(),value:o.id});}
  else mem.set(o.id,o);
}
async function getOrder(id){
  if(redis){const s=await redis.get(orderKey(id));return s?JSON.parse(s):null;}
  return mem.get(id)||null;
}
async function allOrders(){
  if(redis){
    const ids=await redis.zRange("afia:orders",0,-1,{REV:true});
    const vals=await Promise.all(ids.map(getOrder));
    return vals.filter(Boolean);
  }
  return [...mem.values()].sort((a,b)=>new Date(b.createdAt)-new Date(a.createdAt));
}
async function inventoryAll(){
  if(redis){const x=await redis.hGetAll("afia:stock");return Object.fromEntries(Object.entries(x).map(([k,v])=>[k,Number(v)]));}
  return Object.fromEntries(memStock.entries());
}
async function inventorySet(id,qty){
  id=String(id);qty=Math.max(0,Math.floor(Number(qty)||0));
  if(redis) await redis.hSet("afia:stock",id,String(qty)); else memStock.set(id,qty);
  return qty;
}
async function inventoryReserve(items){
  if(redis){
    const argv=[];for(const x of items){argv.push(String(x.id),String(Math.max(1,Math.floor(Number(x.qty)||1))))}
    const script=`
      local key=KEYS[1]
      for i=1,#ARGV,2 do
        local v=redis.call('HGET',key,ARGV[i])
        if v and tonumber(v)<tonumber(ARGV[i+1]) then return {'ERR',ARGV[i],v} end
      end
      for i=1,#ARGV,2 do
        local v=redis.call('HGET',key,ARGV[i])
        if v then redis.call('HINCRBY',key,ARGV[i],-tonumber(ARGV[i+1])) end
      end
      return {'OK'}
    `;
    const r=await redis.eval(script,{keys:["afia:stock"],arguments:argv});
    if(Array.isArray(r)&&r[0]==="ERR") return {ok:false,id:r[1],available:Number(r[2]||0)};
    return {ok:true};
  }
  for(const x of items){const k=String(x.id);if(memStock.has(k)&&memStock.get(k)<x.qty)return{ok:false,id:k,available:memStock.get(k)}}
  for(const x of items){const k=String(x.id);if(memStock.has(k))memStock.set(k,memStock.get(k)-x.qty)}
  return {ok:true};
}
async function inventoryRelease(items){
  if(redis){
    for(const x of items||[]){const k=String(x.id),q=Math.max(1,Math.floor(Number(x.qty)||1));if(await redis.hExists("afia:stock",k))await redis.hIncrBy("afia:stock",k,q)}
  }else{
    for(const x of items||[]){const k=String(x.id),q=Math.max(1,Math.floor(Number(x.qty)||1));if(memStock.has(k))memStock.set(k,memStock.get(k)+q)}
  }
}
function makeId(){
  const d=new Date();
  const y=d.getFullYear().toString().slice(-2);
  const m=String(d.getMonth()+1).padStart(2,"0");
  const day=String(d.getDate()).padStart(2,"0");
  const r=crypto.randomInt(1000,9999);
  return `AF${y}${m}${day}-${r}`;
}
function cleanPhone(s=""){return String(s).replace(/\D/g,"").slice(-15)}
function publicOrder(o){
  return {id:o.id,createdAt:o.createdAt,status:o.status,statusHistory:o.statusHistory||[],name:o.name,phone:o.phone,locationUrl:o.locationUrl||"",mode:o.mode,total:o.total,items:o.items||[]};
}
async function sendWhatsApp(order){
  if(!WA_TOKEN||!WA_PHONE_ID||!WA_TO) return {sent:false,reason:"not_configured"};
  const itemText=(order.items||[]).map(x=>`• ${x.name} × ${x.qty}`).join("\n");
  const msg=`طلب جديد ${order.id}\nالعميل: ${order.name}\nالجوال: ${order.phone}\nالإجمالي: ${Number(order.total||0).toFixed(2)} ر.س\n${order.locationUrl?("الموقع: "+order.locationUrl+"\n"):""}${itemText}`;
  const resp=await fetch(`https://graph.facebook.com/v21.0/${WA_PHONE_ID}/messages`,{
    method:"POST",
    headers:{"Authorization":"Bearer "+WA_TOKEN,"Content-Type":"application/json"},
    body:JSON.stringify({messaging_product:"whatsapp",to:WA_TO,type:"text",text:{body:msg,preview_url:true}})
  });
  return {sent:resp.ok,status:resp.status};
}
function requireAdmin(req,res,next){
  const token=String(req.headers["x-admin-token"]||"");
  const exp=adminSessions.get(token);
  if(!exp||exp<Date.now()) return res.status(401).json({error:"unauthorized"});
  next();
}
app.post("/api/admin/login",(req,res)=>{
  const raw=String(req.body?.password||"");
  const hash=crypto.createHash("sha256").update(raw).digest("hex");
  if(hash!==ADMIN_PASSWORD_HASH) return res.status(401).json({error:"unauthorized"});
  const token=crypto.randomBytes(24).toString("hex");
  adminSessions.set(token,Date.now()+12*60*60*1000);
  res.json({token,expiresIn:43200});
});

app.get("/health",async(req,res)=>{let orderCount=0,inventoryCount=0,catalogCount=0;try{orderCount=(await allOrders()).length;inventoryCount=Object.keys(await inventoryAll()).length;if(redis){const s=await redis.get("afia:catalog");catalogCount=s?(JSON.parse(s)||[]).length:0}else catalogCount=(memCatalog||[]).length}catch(e){}res.json({ok:true,store:redis?"redis":"memory",orders:orderCount,inventory:inventoryCount,catalog:catalogCount,time:new Date().toISOString()})});
app.get("/api/inventory",async(req,res)=>{
  try{res.json({inventory:await inventoryAll()});}catch(e){res.status(500).json({error:"server_error"});}
});
app.get("/api/catalog",async(req,res)=>{
  try{
    if(redis){const s=await redis.get("afia:catalog");return res.json({catalog:s?JSON.parse(s):null});}
    res.json({catalog:memCatalog});
  }catch(e){res.status(500).json({error:"server_error"});}
});



app.post("/api/orders",async(req,res)=>{
  try{
    const b=req.body||{};
    if(!b.name||!b.phone||!Array.isArray(b.items)||!b.items.length) return res.status(400).json({error:"missing_fields"});
    const reserved=await inventoryReserve(b.items||[]);
    if(!reserved.ok) return res.status(409).json({error:"out_of_stock",productId:reserved.id,available:reserved.available});
    const id=makeId(), now=new Date().toISOString();
    const order={
      id,createdAt:now,updatedAt:now,status:"جديد",
      statusHistory:[{status:"جديد",at:now}],
      name:String(b.name).slice(0,120),
      phone:cleanPhone(b.phone),
      address:String(b.address||"").slice(0,500),
      locationUrl:String(b.locationUrl||"").slice(0,1000),
      notes:String(b.notes||"").slice(0,1000),
      mode:b.mode==="pickup"?"pickup":"delivery",
      subtotal:Number(b.subtotal||0),delivery:Number(b.delivery||0),discount:Number(b.discount||0),total:Number(b.total||0),
      items:b.items.map(x=>({id:x.id,name:String(x.name||"").slice(0,240),qty:Number(x.qty||1),unit:Number(x.unit||0),total:Number(x.total||0)}))
    };
    await saveOrder(order);
    let wa={sent:false};
    try{wa=await sendWhatsApp(order)}catch(e){}
    res.status(201).json({order:publicOrder(order),whatsapp:wa});
  }catch(e){res.status(500).json({error:"server_error"});}
});

app.get("/api/orders/:id",async(req,res)=>{
  try{
    const o=await getOrder(req.params.id);
    if(!o) return res.status(404).json({error:"not_found"});
    const phone=cleanPhone(req.query.phone||"");
    if(phone && phone!==cleanPhone(o.phone)) return res.status(403).json({error:"phone_mismatch"});
    res.json({order:publicOrder(o)});
  }catch(e){res.status(500).json({error:"server_error"});}
});

app.get("/api/admin/orders",requireAdmin,async(req,res)=>{
  try{res.json({orders:await allOrders()});}catch(e){res.status(500).json({error:"server_error"});}
});
app.get("/api/admin/backup",requireAdmin,async(req,res)=>{
  try{
    res.json({generatedAt:new Date().toISOString(),orders:await allOrders(),inventory:await inventoryAll()});
  }catch(e){res.status(500).json({error:"server_error"});}
});


app.put("/api/admin/catalog",requireAdmin,async(req,res)=>{
  try{
    const catalog=req.body?.catalog;
    if(!Array.isArray(catalog)||catalog.length>2000) return res.status(400).json({error:"bad_catalog"});
    const clean=catalog.filter(x=>x&&x.id).map(x=>({
      id:Number(x.id),c:String(x.c||""),family:String(x.family||""),sub:String(x.sub||""),brand:String(x.brand||""),
      e:String(x.e||""),ar:String(x.ar||""),en:String(x.en||""),size:String(x.size||""),p:Number(x.p||0),
      old:x.old===undefined?undefined:Number(x.old),badge:String(x.badge||""),img:String(x.img||""),visible:x.visible!==false
    }));
    if(redis)await redis.set("afia:catalog",JSON.stringify(clean));else memCatalog=clean;
    res.json({ok:true,count:clean.length});
  }catch(e){res.status(500).json({error:"server_error"});}
});

app.get("/api/admin/inventory",requireAdmin,async(req,res)=>{
  try{res.json({inventory:await inventoryAll()});}catch(e){res.status(500).json({error:"server_error"});}
});
app.patch("/api/admin/inventory/:id",requireAdmin,async(req,res)=>{
  try{
    if(req.body?.stock===null||req.body?.stock==="") {
      if(redis) await redis.hDel("afia:stock",String(req.params.id)); else memStock.delete(String(req.params.id));
      return res.json({id:req.params.id,stock:null});
    }
    const stock=await inventorySet(req.params.id,req.body?.stock);
    res.json({id:req.params.id,stock});
  }catch(e){res.status(500).json({error:"server_error"});}
});


app.patch("/api/admin/orders/:id",requireAdmin,async(req,res)=>{
  try{
    const allowed=["جديد","تم التأكيد","جاري التجهيز","خرج للتوصيل","تم التسليم","ملغي"];
    const status=String(req.body?.status||"");
    if(!allowed.includes(status)) return res.status(400).json({error:"bad_status"});
    const o=await getOrder(req.params.id);
    if(!o) return res.status(404).json({error:"not_found"});
    if(o.status===status) return res.json({order:o});
    if(o.status!=="ملغي"&&status==="ملغي"){
      await inventoryRelease(o.items||[]);
      o.stockReleased=true;
    } else if(o.status==="ملغي"&&status!=="ملغي"){
      const reserved=await inventoryReserve(o.items||[]);
      if(!reserved.ok) return res.status(409).json({error:"out_of_stock",productId:reserved.id,available:reserved.available});
      o.stockReleased=false;
    }
    o.status=status;o.updatedAt=new Date().toISOString();
    o.statusHistory=[...(o.statusHistory||[]),{status,at:o.updatedAt}];
    await saveOrder(o);
    res.json({order:o});
  }catch(e){res.status(500).json({error:"server_error"});}
});

app.listen(PORT,()=>console.log("Afia API listening on",PORT));
