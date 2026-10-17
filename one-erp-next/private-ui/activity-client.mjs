/** Authenticated source-backed audit facts; no browser-generated audit entries. */
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JWT=/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const TYPES=new Set(["invoice","payment","journal","return","stock","delivery","receiving","sales_order","purchase_order"]);
const ACTION=/^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;
const INSTANT=/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/;
const CHANGE_KEYS=new Set(["status","line_no","product_id","warehouse_id",
  "ordered_qty","unit_price"]);
function validChangeValues(obj){
  return obj&&typeof obj==="object"&&!Array.isArray(obj)&&
    Object.keys(obj).every(k=>CHANGE_KEYS.has(k)&&
      (typeof obj[k]==="string"&&obj[k].length<=80||
       k==="line_no"&&Number.isInteger(obj[k])&&obj[k]>0));
}
function validDiff(item,include_changes){
  if(!Object.hasOwn(item,"changes"))return true;
  if(!include_changes||!item.event_id?.startsWith("change:"))return false;
  const diff=item.changes;
  return diff&&typeof diff==="object"&&!Array.isArray(diff)&&
    Object.keys(diff).length===2&&Object.hasOwn(diff,"before")&&
    Object.hasOwn(diff,"after")&&validChangeValues(diff.before)&&
    validChangeValues(diff.after);
}
export class ActivityReadError extends Error{
  constructor(status){super("Private activity history unavailable");this.name="ActivityReadError";this.status=status;}
}
export function validateActivityPage(data,{tenant,record_type,record_id,limit,offset,include_changes=false}){
  if(!data||data.module!=="activity"||data.tenant_id!==tenant||
     data.record_type!==record_type||data.record_id!==record_id||
     data.source!=="one_erp_next_postgresql"||data.read_only!==true||
     !Array.isArray(data.items)||data.items.length>limit||
     !data.page||data.page.limit!==limit||data.page.offset!==offset||
     typeof data.page.has_more!=="boolean"||
     data.page.next_offset!==(data.page.has_more?offset+limit:null)||
     data.items.some(item=>!item||typeof item!=="object"||Array.isArray(item)||
       typeof item.event_id!=="string"||item.event_id.length<1||item.event_id.length>64||
       typeof item.action!=="string"||!ACTION.test(item.action)||
       typeof item.actor_id!=="string"||!UUID.test(item.actor_id)||
       typeof item.occurred_at!=="string"||!INSTANT.test(item.occurred_at)||
       Number.isNaN(Date.parse(item.occurred_at))||
       !validDiff(item,include_changes))){
    throw new ActivityReadError(502);
  }
  return data;
}
export async function readActivity({baseUrl,tenant,record_type,record_id,token,
    limit=20,offset=0,include_changes=false,signal,fetchImpl=fetch}){
  if(!UUID.test(tenant||"")||!UUID.test(record_id||"")||!TYPES.has(record_type)||
     !JWT.test(token||"")||!Number.isInteger(limit)||limit<1||limit>50||
     !Number.isInteger(offset)||offset<0||offset>5000||
     typeof include_changes!=="boolean"||
     (include_changes&&!["sales_order","purchase_order"].includes(record_type))||
     typeof fetchImpl!=="function"){
    throw new TypeError("Signed tenant and record references required");
  }
  let origin;
  try{origin=new URL(baseUrl);}catch{throw new TypeError("Trusted HTTPS activity origin required");}
  if(origin.protocol!=="https:"||!origin.hostname||origin.hostname.endsWith(".")||
     origin.username||origin.password||origin.pathname!=="/"||origin.search||origin.hash){
    throw new TypeError("Trusted HTTPS activity origin required");
  }
  const query=new URLSearchParams({tenant,record_type,record_id,limit:String(limit),offset:String(offset)});
  if(include_changes)query.set("include_changes","1");
  let response;
  try{
    response=await fetchImpl(new URL("/api/v1/activity/timeline?"+query,origin),{
      method:"GET",mode:"cors",cache:"no-store",credentials:"omit",
      redirect:"error",signal,
      headers:{Authorization:"Bearer "+token,Accept:"application/json"},
    });
  }catch{throw new ActivityReadError(503);}
  if(!response?.ok)throw new ActivityReadError(Number(response?.status)||503);
  if(!String(response.headers?.get("content-type")||"").toLowerCase().includes("application/json")){
    throw new ActivityReadError(502);
  }
  let body;try{body=await response.json();}catch{throw new ActivityReadError(502);}
  return validateActivityPage(body,{tenant,record_type,record_id,limit,offset,include_changes});
}
