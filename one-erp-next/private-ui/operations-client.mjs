/** Authenticated private operations client matching PostgreSQL 056 API exactly. */
const ALLOWED=new Set(["sales","purchases","inventory","returns"]);
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JWT=/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
export class OperationsApiError extends Error{
  constructor(status){super("Private operations unavailable");this.name="OperationsApiError";this.status=status;}
}
export function validateOperationsPage(data,{module,tenant,limit,offset}){
  if(!data||data.module!==module||data.tenant_id!==tenant||
     data.read_only!==true||data.source!=="one_erp_next_postgresql"||
     !Array.isArray(data.items)||data.items.length>limit||
     !data.page||data.page.limit!==limit||data.page.offset!==offset||
     typeof data.page.has_more!=="boolean"||
     data.page.next_offset!==(data.page.has_more?offset+limit:null)||
     data.items.some(item=>!item||Array.isArray(item)||typeof item!=="object"||
       Object.entries(item).some(([key,value])=>
         typeof key!=="string"||key.length>80||
         (value!==null&&!["string","number","boolean"].includes(typeof value))||
         (typeof value==="string"&&value.length>1000))))throw new OperationsApiError(502);
  return data;
}
export async function readOperations({baseUrl,module,tenant,token,limit=25,offset=0,fetchImpl=fetch,signal}){
  if(!ALLOWED.has(module)||typeof tenant!=="string"||!UUID.test(tenant)||
     !JWT.test(token||"")||!Number.isInteger(limit)||limit<1||limit>50||
     !Number.isInteger(offset)||offset<0||offset>5000||
     typeof fetchImpl!=="function")throw new TypeError("Signed tenant-scoped operations request required");
  let origin;
  try{origin=new URL(baseUrl);}catch{throw new TypeError("Trusted HTTPS API origin required");}
  if(origin.protocol!=="https:"||origin.username||origin.password||origin.search||
     origin.hash||origin.pathname!=="/"||!origin.hostname||origin.hostname.endsWith(".")){
    throw new TypeError("Trusted HTTPS API origin required");
  }
  const query=new URLSearchParams({tenant,limit:String(limit),offset:String(offset)});
  const url=new URL("/api/v1/operations/"+module+"?"+query,origin);
  let response;
  try{
    response=await fetchImpl(url,{
      method:"GET",mode:"cors",cache:"no-store",credentials:"omit",
      redirect:"error",signal,
      headers:{Authorization:"Bearer "+token,Accept:"application/json"},
    });
  }catch{throw new OperationsApiError(503);}
  if(!response?.ok)throw new OperationsApiError(Number(response?.status)||503);
  if(!String(response.headers?.get("content-type")||"").toLowerCase().includes("application/json")){
    throw new OperationsApiError(502);
  }
  let data;try{data=await response.json();}catch{throw new OperationsApiError(502);}
  return validateOperationsPage(data,{module,tenant,limit,offset});
}
