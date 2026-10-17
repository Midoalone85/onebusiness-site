/** ONE ERP NEXT private receivables reader. Source-only, NOT bank confirmation. */
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JWT=/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const MONEY=/^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?$/;
const CURRENCY=/^[A-Z]{3}$/;
const DATE=/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
const BUCKETS=new Set(["current","1_30","31_60","61_90","over_90","internally_cleared"]);
const FIELDS=["gross","applied_receipts","issued_credits","outstanding"];
export class ReceivablesReadError extends Error{
  constructor(status){super("Private receivables unavailable");this.status=status;this.name="ReceivablesReadError";}
}
export function validReportDate(value){
  if(typeof value!=="string"||!DATE.test(value))return false;
  const date=new Date(value+"T00:00:00Z");
  return !Number.isNaN(date.getTime())&&date.toISOString().slice(0,10)===value;
}
function trustedOrigin(value){
  let url;
  try{url=new URL(value);}catch{throw new TypeError("Trusted private HTTPS origin required");}
  if(url.protocol!=="https:"||!url.hostname||url.hostname.endsWith(".")||
     url.username||url.password||url.pathname!=="/"||url.search||url.hash){
    throw new TypeError("Trusted private HTTPS origin required");
  }
  return url;
}
/** Immutable integer millionths, so no floating point rounding or mixed currency. */
export function toMillionths(value){
  if(typeof value!=="string"||!MONEY.test(value))throw new TypeError("Exact nonnegative decimal required");
  const parts=value.split(".");
  return BigInt(parts[0])*1000000n+BigInt((parts[1]||"").padEnd(6,"0"));
}
export function fromMillionths(value){
  if(typeof value!=="bigint"||value<0n)throw new TypeError("Nonnegative exact amount required");
  const major=value/1000000n;
  const fraction=(value%1000000n).toString().padStart(6,"0").replace(/0+$/,"");
  return major.toString()+"."+(fraction||"00").padEnd(2,"0");
}
function validRow(row){
  if(!row||typeof row!=="object"||
     typeof row.invoice_id!=="string"||!UUID.test(row.invoice_id)||
     typeof row.number!=="string"||!row.number||row.number.length>150||
     typeof row.partner_reference!=="string"||row.partner_reference.length>200||
     typeof row.currency!=="string"||!CURRENCY.test(row.currency)||
     !validReportDate(row.issued_on)||!validReportDate(row.due_on)||
     !BUCKETS.has(row.aging_bucket)||!Number.isInteger(row.days_overdue)||
     row.days_overdue<0||row.days_overdue>100000||
     !["open","no_open_balance_internal"].includes(row.status))return false;
  try{
    const values=Object.fromEntries(FIELDS.map(k=>[k,toMillionths(row[k])]));
    if(values.gross-values.applied_receipts-values.issued_credits!==values.outstanding)return false;
    if(values.outstanding===0n)return row.aging_bucket==="internally_cleared"&&
      row.status==="no_open_balance_internal"&&row.days_overdue===0;
    const age=row.days_overdue;
    const bucket=age===0?"current":age<=30?"1_30":age<=60?"31_60":
      age<=90?"61_90":"over_90";
    return values.outstanding>0n&&row.aging_bucket===bucket&&row.status==="open";
  }catch{return false;}
}
export function validateReceivablesPage(body,{tenant,as_of,limit,offset}){
  if(!body||typeof body!=="object"||body.module!=="receivables"||
     body.tenant_id!==tenant||body.as_of!==as_of||
     body.source!=="one_erp_next_postgresql"||body.read_only!==true||
     !Array.isArray(body.items)||body.items.length>limit||
     !body.items.every(validRow)||!body.page||
     body.page.limit!==limit||body.page.offset!==offset||
     typeof body.page.has_more!=="boolean"||
     (body.page.has_more?body.page.next_offset!==offset+limit:
       body.page.next_offset!==null)){
    throw new ReceivablesReadError(502);
  }
  return body;
}
export async function readReceivablesPage({baseUrl,tenant,as_of,token,
    limit=25,offset=0,signal,fetchImpl=fetch}){
  const origin=trustedOrigin(baseUrl);
  if(typeof tenant!=="string"||!UUID.test(tenant)||
     !validReportDate(as_of)||!JWT.test(token||"")||
     !Number.isInteger(limit)||limit<1||limit>50||
     !Number.isInteger(offset)||offset<0||offset>5000||
     typeof fetchImpl!=="function"){
    throw new TypeError("Signed, tenant-scoped bounded report required");
  }
  const query=new URLSearchParams({tenant,as_of,limit:String(limit),offset:String(offset)});
  let reply;
  try{
    reply=await fetchImpl(new URL("/api/v1/operations/receivables?"+query,origin),{
      method:"GET",mode:"cors",cache:"no-store",credentials:"omit",redirect:"error",
      signal,headers:{"Accept":"application/json","Authorization":"Bearer "+token},
    });
  }catch{throw new ReceivablesReadError(503);}
  if(!reply?.ok)throw new ReceivablesReadError(Number(reply?.status)||503);
  if(!String(reply.headers?.get("content-type")||"").toLowerCase().includes("application/json")){
    throw new ReceivablesReadError(502);
  }
  let body;try{body=await reply.json();}catch{throw new ReceivablesReadError(502);}
  return validateReceivablesPage(body,{tenant,as_of,limit,offset});
}
/** These totals apply ONLY to the fetched page, never the whole ledger. */
export function currentPageBalances(rows){
  const totals=new Map();
  for(const row of rows){
    if(!validRow(row))throw new ReceivablesReadError(502);
    totals.set(row.currency,(totals.get(row.currency)||0n)+toMillionths(row.outstanding));
  }
  return [...totals].sort(([a],[b])=>a.localeCompare(b))
    .map(([currency,total])=>({currency,outstanding:fromMillionths(total)}));
}
