import express from "express";
import cors from "cors";
import crypto from "crypto";
import {createClient} from "redis";

const app=express();
app.use(cors({origin:true,methods:["GET","POST"]}));
app.use(express.json({limit:"256kb"}));

const PORT=process.env.PORT||10000;
const REDIS_URL=process.env.REDIS_URL||"";
let redis=null;
const memPlayers=new Map(),memTokens=new Map(),memVotes=new Map();

async function init(){
  if(!REDIS_URL)return;
  try{redis=createClient({url:REDIS_URL});redis.on("error",()=>{});await redis.connect()}catch(e){redis=null}
}
await init();

const SEASON_ID="S1";
const SEASON_END=new Date(Date.now()+7*24*3600*1000).toISOString();
const GLOBAL_EVENT={
  id:"human-vote-1",
  title:"صوت البشرية الأول",
  question:"وجد البشر مدينة تحت الأرض تكفي 10% فقط من الناجين. من يدخل؟",
  options:["الأقدم في البرج","الأكثر رحمة","الأقوى","قرعة عادلة"],
  endsAt:new Date(Date.now()+24*3600*1000).toISOString()
};

function token(){return crypto.randomBytes(24).toString("hex")}
function code(){return crypto.randomBytes(4).toString("hex").toUpperCase()}
function cleanName(x){return String(x||"PLAYER").replace(/[<>]/g,"").trim().slice(0,22)||"PLAYER"}
function now(){return new Date().toISOString()}
async function getPlayer(id){
  if(redis){let s=await redis.get("ld:player:"+id);return s?JSON.parse(s):null}
  return memPlayers.get(id)||null;
}
async function savePlayer(p){
  if(redis){
    await redis.set("ld:player:"+p.id,JSON.stringify(p));
    await redis.zAdd("ld:rank:"+SEASON_ID,[{score:Number(p.score||0),value:p.id}]);
    await redis.hSet("ld:invite",p.inviteCode,p.id);
  }else{memPlayers.set(p.id,p)}
}
async function playerByInvite(inv){
  if(redis){let id=await redis.hGet("ld:invite",inv);return id?getPlayer(id):null}
  for(const p of memPlayers.values())if(p.inviteCode===inv)return p;return null;
}
async function auth(req){
  const t=String(req.headers["x-player-token"]||"");
  if(!t)return null;
  if(redis){const id=await redis.get("ld:token:"+t);return id?getPlayer(id):null}
  const id=memTokens.get(t);return id?getPlayer(id):null;
}
async function leaderboard(limit=50){
  if(redis){
    const rows=await redis.zRangeWithScores("ld:rank:"+SEASON_ID,0,Math.max(0,limit-1),{REV:true});
    const ps=await Promise.all(rows.map(x=>getPlayer(x.value)));
    return ps.filter(Boolean).map((p,i)=>({rank:i+1,id:p.id,name:p.name,floor:p.floor,level:p.level,score:p.score,trait:p.topTrait||"curiosity"}));
  }
  return [...memPlayers.values()].sort((a,b)=>b.score-a.score).slice(0,limit).map((p,i)=>({rank:i+1,id:p.id,name:p.name,floor:p.floor,level:p.level,score:p.score,trait:p.topTrait||"curiosity"}));
}
async function votes(){
  let counts=Array(GLOBAL_EVENT.options.length).fill(0);
  if(redis){
    const h=await redis.hGetAll("ld:votes:"+GLOBAL_EVENT.id);
    Object.values(h).forEach(v=>{let i=Number(v);if(Number.isInteger(i)&&counts[i]!==undefined)counts[i]++});
  }else{
    for(const v of memVotes.values()){let i=Number(v);if(counts[i]!==undefined)counts[i]++}
  }
  return counts;
}

app.get("/health",async(req,res)=>res.json({ok:true,store:redis?"redis":"memory",season:SEASON_ID,time:now()}));

app.post("/api/register",async(req,res)=>{
  try{
    const id="P"+crypto.randomBytes(6).toString("hex").toUpperCase();
    const t=token();
    const p={id,name:cleanName(req.body?.name),inviteCode:code(),floor:1,level:1,xp:0,coins:250,gems:5,score:100,topTrait:"curiosity",createdAt:now(),updatedAt:now(),referredBy:null};
    const inv=String(req.body?.invite||"").trim().toUpperCase();
    if(inv){const ref=await playerByInvite(inv);if(ref){p.referredBy=ref.id;p.coins+=50;ref.coins=(ref.coins||0)+50;ref.score=(ref.score||0)+25;ref.updatedAt=now();await savePlayer(ref)}}
    await savePlayer(p);
    if(redis)await redis.setEx("ld:token:"+t,60*60*24*365,p.id);else memTokens.set(t,p.id);
    res.json({player:p,token:t,season:{id:SEASON_ID,endsAt:SEASON_END}});
  }catch(e){res.status(500).json({error:"server_error"})}
});

app.get("/api/me",async(req,res)=>{const p=await auth(req);if(!p)return res.status(401).json({error:"unauthorized"});res.json({player:p,season:{id:SEASON_ID,endsAt:SEASON_END}})});

app.post("/api/sync",async(req,res)=>{
  try{
    const p=await auth(req);if(!p)return res.status(401).json({error:"unauthorized"});
    const b=req.body||{};
    const floor=Math.max(1,Math.min(100,Math.floor(Number(b.floor)||1)));
    const level=Math.max(1,Math.min(100,Math.floor(Number(b.level)||1)));
    // basic anti-cheat sanity: one sync cannot jump more than 5 floors above server state
    if(floor>Number(p.floor||1)+5)return res.status(409).json({error:"progress_jump_rejected",serverFloor:p.floor});
    p.floor=Math.max(Number(p.floor||1),floor);
    p.level=Math.max(Number(p.level||1),level);
    p.xp=Math.max(0,Math.min(100000,Math.floor(Number(b.xp)||0)));
    p.coins=Math.max(0,Math.min(1000000,Math.floor(Number(b.coins)||0)));
    p.gems=Math.max(0,Math.min(10000,Math.floor(Number(b.gems)||0)));
    p.topTrait=String(b.topTrait||p.topTrait||"curiosity").slice(0,20);
    p.score=p.floor*100+p.level*25+Math.floor(p.xp/5)+(Number(b.streak)||0)*10;
    p.updatedAt=now();
    await savePlayer(p);
    res.json({player:p});
  }catch(e){res.status(500).json({error:"server_error"})}
});

app.get("/api/leaderboard",async(req,res)=>{
  try{res.json({season:{id:SEASON_ID,endsAt:SEASON_END},players:await leaderboard(Math.min(100,Number(req.query.limit)||50))})}
  catch(e){res.status(500).json({error:"server_error"})}
});

app.get("/api/global-event",async(req,res)=>{
  try{res.json({event:GLOBAL_EVENT,counts:await votes()})}catch(e){res.status(500).json({error:"server_error"})}
});

app.post("/api/global-event/vote",async(req,res)=>{
  try{
    const p=await auth(req);if(!p)return res.status(401).json({error:"unauthorized"});
    const option=Number(req.body?.option);
    if(!Number.isInteger(option)||option<0||option>=GLOBAL_EVENT.options.length)return res.status(400).json({error:"bad_option"});
    if(redis)await redis.hSet("ld:votes:"+GLOBAL_EVENT.id,p.id,String(option));else memVotes.set(p.id,option);
    res.json({ok:true,counts:await votes()});
  }catch(e){res.status(500).json({error:"server_error"})}
});

app.get("/api/invite/:code",async(req,res)=>{
  const p=await playerByInvite(String(req.params.code||"").toUpperCase());
  if(!p)return res.status(404).json({error:"not_found"});
  res.json({inviter:{name:p.name,floor:p.floor,level:p.level},inviteCode:p.inviteCode});
});

app.listen(PORT,()=>console.log("Last Door API listening on",PORT));
