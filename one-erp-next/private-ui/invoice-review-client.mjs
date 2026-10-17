/** Private, read-only immutable invoice evidence GET; NOT a fiscal invoice. */
import {trustedApprovalOrigin,validDocumentRef,InternalApprovalError} from "./invoice-approval-client.mjs";
const JWT=/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const LABEL_MAX=100,VALUE_MAX=500;
const LINE_KEYS=["line_number","description","quantity","unit_price",
  "discount_amount","tax_category","tax_rate","tax_code","net","tax","gross"];
function validFields(fields) {
  return Array.isArray(fields)&&fields.length>=1&&fields.length<=30&&fields.every(f=>
    f&&typeof f.label==="string"&&f.label.trim()&&f.label.length<=LABEL_MAX&&
    (typeof f.value==="string"||typeof f.value==="number")&&
    String(f.value).length<=VALUE_MAX);
}
function validLine(line){
  return line&&typeof line==="object"&&!Array.isArray(line)&&
    LINE_KEYS.every(key=>Object.hasOwn(line,key))&&
    Number.isInteger(line.line_number)&&line.line_number>0&&
    LINE_KEYS.filter(key=>key!=="line_number").every(key=>
      typeof line[key]==="string"&&line[key].length<=500)&&
    line.description.trim().length>0;
}
export async function readInternalInvoiceSource({baseUrl,tenant,document_id,token,signal,fetchImpl=fetch}) {
  const origin=trustedApprovalOrigin(baseUrl);
  if(!validDocumentRef(tenant)||!validDocumentRef(document_id)||!JWT.test(token||"")){
    throw new TypeError("Signed reviewer and canonical source references required");
  }
  if(typeof fetchImpl!=="function")throw new TypeError("Trusted request adapter required");
  const query=new URLSearchParams({tenant,document_id});
  let reply;
  try{
    const url=new URL("/api/v1/finance/internal-invoice-source?"+query,origin);
    reply=await fetchImpl(url,{
      method:"GET",mode:"cors",cache:"no-store",credentials:"omit",
      redirect:"error",signal,
      headers:{Authorization:"Bearer "+token,Accept:"application/json"},
    });
  }catch{throw new InternalApprovalError(503);}
  if(!reply?.ok)throw new InternalApprovalError(Number(reply?.status)||503);
  if(!String(reply.headers?.get("content-type")||"").toLowerCase().includes("application/json")){
    throw new InternalApprovalError(502);
  }
  let body;
  try{body=await reply.json();}catch{throw new InternalApprovalError(502);}
  if(!body||body.tenant!==tenant||body.document_id!==document_id||
     body.reviewable!==true||body.source!=="verified_internal_postgresql"||
     !validFields(body.fields)||!Array.isArray(body.lines)||
     body.lines.length<1||body.lines.length>100||
     !body.lines.every(validLine)||
     new Set(body.lines.map(line=>line.line_number)).size!==body.lines.length){
    throw new InternalApprovalError(502);
  }
  return {tenant,document_id,reviewable:true,source:body.source,
          fields:body.fields,lines:body.lines};
}
