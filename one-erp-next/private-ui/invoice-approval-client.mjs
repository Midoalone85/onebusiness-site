/** Private development-only signed approval client. No ZATCA, bank or public preview access. */
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const JWT=/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
export function validDocumentRef(value) {
  return typeof value==="string"&&UUID.test(value)&&value!=="00000000-0000-0000-0000-000000000000";
}
export function trustedApprovalOrigin(baseUrl) {
  let origin;
  try {origin=new URL(baseUrl);} catch {throw new TypeError("Trusted HTTPS origin required");}
  if(origin.protocol!=="https:"||!origin.hostname||origin.username||origin.password||
      origin.pathname!=="/"||origin.search||origin.hash||origin.hostname.endsWith(".")){
    throw new TypeError("Trusted HTTPS origin required");
  }
  return origin;
}
export class InternalApprovalError extends Error {
  constructor(status) {super("Private invoice approval failed");this.name="InternalApprovalError";this.status=status;}
}
/** Only references and signed identity are transmitted. Server enforces maker/checker. */
export async function approveInternalInvoice({baseUrl,tenant,document_id,token,fetchImpl=fetch,signal}) {
  const origin=trustedApprovalOrigin(baseUrl);
  if(!validDocumentRef(tenant)||!validDocumentRef(document_id)||!JWT.test(token||"")){
    throw new TypeError("Signed identity and canonical document references required");
  }
  if(typeof fetchImpl!=="function")throw new TypeError("Trusted request adapter required");
  let response;
  try {
    response=await fetchImpl(new URL("/api/v1/finance/internal-invoice-approvals",origin),{
      method:"POST",mode:"cors",cache:"no-store",credentials:"omit",redirect:"error",signal,
      headers:{Authorization:"Bearer "+token,Accept:"application/json","Content-Type":"application/json"},
      body:JSON.stringify({tenant,document_id})
    });
  }catch{throw new InternalApprovalError(503);}
  if(!response?.ok)throw new InternalApprovalError(Number(response?.status)||503);
  if(!String(response.headers?.get("content-type")||"").toLowerCase().includes("application/json"))
    throw new InternalApprovalError(502);
  let reply;
  try{reply=await response.json();}catch{throw new InternalApprovalError(502);}
  if(!reply||reply.ok!==true||reply.status!=="approved_internal_source"||
      reply.tenant!==tenant||reply.document_id!==document_id)throw new InternalApprovalError(502);
  return {tenant,document_id,status:"approved_internal_source"};
}
