import {approveInternalInvoice,trustedApprovalOrigin,validDocumentRef} from "./invoice-approval-client.mjs";
import {readInternalInvoiceSource,validateInternalInvoiceSource} from "./invoice-review-client.mjs";
const LABELS=Object.freeze({
  ar:{heading:"مراجعة مصدر الفاتورة الداخلية",source:"مرجع الفاتورة",tenant:"الشركة",review:"تفاصيل المصدر المعتمد",loading:"جارٍ تحميل بيانات المراجعة…",confirm:"راجعت تفاصيل المصدر والمستندات الداعمة",approve:"الموافقة كمراجع مستقل",approved:"تمت الموافقة على المصدر الداخلي",unavailable:"لا يمكن عرض مصدر موثوق. الموافقة مقفولة.",denied:"غير مصرح بالموافقة أو تعذر الطلب",clear:"إغلاق المراجعة",notice:"هذه موافقة محاسبية داخلية وليست فاتورة ضريبية أو تصريحًا من زاتكا."},
  en:{heading:"Review internal invoice source",source:"Invoice reference",tenant:"Company",review:"Trusted source details",loading:"Loading reviewer data…",confirm:"I reviewed the source and supporting evidence",approve:"Approve as independent reviewer",approved:"Internal source approved",unavailable:"Trusted source unavailable. Approval blocked.",denied:"Approval denied or unavailable",clear:"Clear review",notice:"Internal accounting approval only, not a tax invoice or ZATCA clearance."}
});
function safeText(v){return typeof v==="string"||typeof v==="number"?String(v):"";}
/** Private source-only UI, not deployed. loadSummary must use a protected server,
 * independently verify tenant/source lineage and show authentic reviewer evidence.
 * Missing loader or unreviewable source FAILS CLOSED. Server is final authority.
 * Never mount on the public preview, persist tokens or enable CORS wildcard.
 */
export function mountInvoiceApprovalPage({root,baseUrl,getToken,loadSummary,fetchImpl,language="ar"}) {
  if(!root?.ownerDocument||typeof getToken!=="function"||
     (loadSummary!=null&&typeof loadSummary!=="function"))
    throw new TypeError("Private root and verified session required");
  const verifiedLoader=loadSummary||((args)=>readInternalInvoiceSource({baseUrl,fetchImpl,...args}));
  trustedApprovalOrigin(baseUrl);
  const doc=root.ownerDocument,words=LABELS[language==="en"?"en":"ar"];
  let revision=0,disposed=false,openRequest=null,approved=false,busy=false;
  let controller=null,reviewAcknowledged=false;
  root.setAttribute("dir",language==="en"?"ltr":"rtl");
  const heading=doc.createElement("h2");heading.textContent=words.heading;
  const refs=doc.createElement("p");
  const notice=doc.createElement("p");notice.textContent=words.notice;
  const status=doc.createElement("p");status.setAttribute("role","status");
  const details=doc.createElement("section");details.setAttribute("aria-label",words.review);
  const confirm=doc.createElement("input");confirm.type="checkbox";confirm.checked=false;
  const confirmLabel=doc.createElement("label");confirmLabel.textContent=words.confirm;
  const approve=doc.createElement("button");approve.type="button";approve.textContent=words.approve;
  const clear=doc.createElement("button");clear.type="button";clear.textContent=words.clear;
  function update(){approve.disabled=disposed||busy||!openRequest||!reviewAcknowledged||approved;confirm.disabled=disposed||busy||!openRequest||approved;}
  function erase(){
    revision++;controller?.abort();controller=null;openRequest=null;approved=false;busy=false;
    reviewAcknowledged=false;confirm.checked=false;refs.textContent="";details.replaceChildren();status.textContent="";update();
  }
  clear.addEventListener("click",erase);
  confirm.addEventListener("change",()=>{
    reviewAcknowledged=Boolean(confirm.checked)&&Boolean(openRequest)&&!approved;update();
  });
  approve.addEventListener("click",async()=>{
    if(disposed||busy||!openRequest||!reviewAcknowledged||approved)return;
    const current=revision,ref=openRequest;busy=true;update();
    try{
      const token=await getToken();
      if(disposed||current!==revision)return;
      const result=await approveInternalInvoice({baseUrl,...ref,token,fetchImpl,signal:controller?.signal});
      if(disposed||current!==revision)return;
      if(result.status==="approved_internal_source"){approved=true;status.textContent=words.approved;}
    }catch{if(!disposed&&current===revision)status.textContent=words.denied;}
    finally{if(!disposed&&current===revision){busy=false;update();}}
  });
  root.replaceChildren(heading,notice,refs,status,details,confirmLabel,confirm,approve,clear);
  update();
  async function open({tenant,document_id}){
    erase();if(disposed)return false;
    if(!validDocumentRef(tenant)||!validDocumentRef(document_id)){status.textContent=words.unavailable;return false;}
    const current=revision;controller=new AbortController();status.textContent=words.loading;
    try{
      const token=await getToken();
      if(disposed||current!==revision)return false;
      const loaded=await verifiedLoader({tenant,document_id,token,signal:controller.signal});
      const summary=validateInternalInvoiceSource(loaded,{tenant,document_id});
      if(disposed||current!==revision)return false;
      // Shared API validation: no UI approval without authentic source marker,
      // complete lines and unique valid line numbers, even with injected loaders.
      refs.textContent=words.tenant+": "+tenant+" | "+words.source+": "+document_id;
      for(const field of summary.fields){
        const row=doc.createElement("p");row.textContent=field.label+": "+safeText(field.value);details.append(row);
      }
      if(Array.isArray(summary.lines)){
        const table=doc.createElement("table");
        const names=["line_number","description","quantity","unit_price",
          "discount_amount","net","tax","gross"];
        const header=doc.createElement("tr");
        for(const name of names){const cell=doc.createElement("th");cell.textContent=name;header.append(cell);}
        table.append(header);
        for(const line of summary.lines){
          const tr=doc.createElement("tr");
          for(const name of names){const cell=doc.createElement("td");cell.textContent=safeText(line[name]);tr.append(cell);}
          table.append(tr);
        }
        details.append(table);
      }
      openRequest={tenant,document_id};status.textContent="";update();return true;
    }catch{if(!disposed&&current===revision){erase();status.textContent=words.unavailable;}return false;}
  }
  return {open,clear:erase,dispose:()=>{erase();disposed=true;root.replaceChildren();},
    snapshot:()=>({ready:Boolean(openRequest),approved,busy})};
}
