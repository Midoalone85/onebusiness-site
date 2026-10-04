import express from "express";
import cors from "cors";
import { createClient } from "redis";
import crypto from "crypto";

const app = express();
app.use(cors({origin:true,credentials:false}));
app.use(express.json({limit:"1mb"}));

const PORT = process.env.PORT || 10000;
const ADMIN_KEY = process.env.ADMIN_KEY || "Admin@Afia2026!";
const REDIS_URL = process.env.REDIS_URL || "";
const WA_TOKEN = process.env.WHATSAPP_TOKEN || "";
const WA_PHONE_ID = process.env.WHATSAPP_PHONE_NUMBER_ID || "";
const WA_TO = process.env.WHATSAPP_ADMIN_TO || "";

let redis = null;
let mem = new Map();

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
  if(req.headers["x-admin-key"]!==ADMIN_KEY) return res.status(401).json({error:"unauthorized"});
  next();
}

app.get("/health",(req,res)=>res.json({ok:true,store:redis?"redis":"memory",time:new Date().toISOString()}));

app.post("/api/orders",async(req,res)=>{
  try{
    const b=req.body||{};
    if(!b.name||!b.phone||!Array.isArray(b.items)||!b.items.length) return res.status(400).json({error:"missing_fields"});
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

app.patch("/api/admin/orders/:id",requireAdmin,async(req,res)=>{
  try{
    const allowed=["جديد","تم التأكيد","جاري التجهيز","خرج للتوصيل","تم التسليم","ملغي"];
    const status=String(req.body?.status||"");
    if(!allowed.includes(status)) return res.status(400).json({error:"bad_status"});
    const o=await getOrder(req.params.id);
    if(!o) return res.status(404).json({error:"not_found"});
    o.status=status;o.updatedAt=new Date().toISOString();
    o.statusHistory=[...(o.statusHistory||[]),{status,at:o.updatedAt}];
    await saveOrder(o);
    res.json({order:o});
  }catch(e){res.status(500).json({error:"server_error"});}
});

app.listen(PORT,()=>console.log("Afia API listening on",PORT));
