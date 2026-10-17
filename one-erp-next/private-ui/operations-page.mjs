import { createOperationsView } from "./operations-view.mjs";
import { mountActivityTimeline } from "./activity-timeline.mjs";

const MODULES = Object.freeze(["sales","purchases","inventory","returns"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const LABELS = Object.freeze({
  ar:{sales:"المبيعات",purchases:"المشتريات",inventory:"المخزون",returns:"المرتجعات",loading:"جارٍ تحميل البيانات…",unauthorized:"لا تملك صلاحية الاطلاع على هذه البيانات",unavailable:"تعذّر تحميل البيانات",empty:"لا توجد سجلات",signout:"إغلاق الجلسة",history:"سجل العمليات"},
  en:{sales:"Sales",purchases:"Purchases",inventory:"Inventory",returns:"Returns",loading:"Loading records…",unauthorized:"Access denied",unavailable:"Records unavailable",empty:"No records",signout:"Clear session",history:"Activity history"}
});
function safeText(v) {
  if (v == null) return "";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : "";
}
/** Caller supplies a trusted DOM element and a short-lived, memory-only token provider.
 * Never embed credentials in HTML or mount this into the public fictional-data preview.
 */
export function mountOperationsPage({root,baseUrl,tenant,getToken,language="ar",timeZone="Asia/Riyadh",fetchImpl,showNavigation=true}) {
  if (!root || !root.ownerDocument || typeof getToken !== "function") throw new TypeError("Private root and token provider required");
  const document = root.ownerDocument;
  const words = LABELS[language === "en" ? "en" : "ar"];
  const view = createOperationsView({baseUrl,tenant,getToken,fetchImpl});
  let generation=0, disposed=false,activity=null;
  function clearActivity(){activity?.dispose();activity=null;}
  root.replaceChildren();
  root.setAttribute("dir",language==="en"?"ltr":"rtl");
  const menu=document.createElement("nav");
  menu.setAttribute("aria-label","ONE ERP operations");
  menu.hidden=!showNavigation;
  const status=document.createElement("p");
  status.setAttribute("role","status");
  const content=document.createElement("section");
  const clear=document.createElement("button");
  clear.type="button";clear.textContent=words.signout;
  clear.addEventListener("click",()=>{generation++;view.clear();clearActivity();content.replaceChildren();status.textContent="";});
  for(const module of MODULES){
    const button=document.createElement("button");
    button.type="button";button.textContent=words[module];
    button.addEventListener("click",()=>load(module));
    menu.append(button);
  }
  root.append(menu,clear,status,content);
  function render(data){
    clearActivity();content.replaceChildren();
    const rows=Array.isArray(data?.items)?data.items:[];
    if(!rows.length){status.textContent=words.empty;return;}
    status.textContent="";
    const table=document.createElement("table");
    const columns=Object.keys(rows[0]??{}).filter(k=>!k.startsWith("_")).slice(0,8);
    const recordType=data.module==="sales"?"sales_order":data.module==="purchases"?"purchase_order":null;
    const allowHistory=Boolean(recordType&&rows.some(row=>
      typeof row?.order_id==="string"&&UUID.test(row.order_id)));
    const historyRoot=document.createElement("aside");
    historyRoot.hidden=true;
    historyRoot.setAttribute("class","one-operations-activity");
    const activeGeneration=generation;
    const head=document.createElement("thead"),header=document.createElement("tr");
    for(const key of columns){const cell=document.createElement("th");cell.textContent=key;header.append(cell);}
    if(allowHistory){const cell=document.createElement("th");cell.textContent=words.history;
      header.append(cell);}
    head.append(header);table.append(head);
    const body=document.createElement("tbody");
    for(const row of rows.slice(0,100)){
      const tr=document.createElement("tr");
      for(const key of columns){const cell=document.createElement("td");cell.textContent=safeText(row?.[key]);tr.append(cell);}
      if(allowHistory){
        const cell=document.createElement("td");
        if(typeof row?.order_id==="string"&&UUID.test(row.order_id)){
          const button=document.createElement("button");
          button.type="button";button.textContent=words.history;
          const orderId=row.order_id;
          button.addEventListener("click",()=>{
            if(disposed||activeGeneration!==generation)return;
            clearActivity();historyRoot.hidden=false;
            activity=mountActivityTimeline({root:historyRoot,baseUrl,tenant,
              getToken,fetchImpl,language,timeZone});
            historyRoot.setAttribute("class","one-activity-panel one-operations-activity");
            void activity.load(recordType,orderId);
          });
          cell.append(button);
        }
        tr.append(cell);
      }
      body.append(tr);
    }
    table.append(body);content.append(table);
    if(allowHistory)content.append(historyRoot);
  }
  async function load(module){
    if(disposed)return false;
    const current=++generation;
    clearActivity();content.replaceChildren();
    status.textContent=words.loading;
    const result=await view.load(module);
    if(disposed||current!==generation)return false;
    if(result.status==="ready"){render(result.data);return true;}
    content.replaceChildren();
    status.textContent=result.error==="unauthorized"?words.unauthorized:words.unavailable;
    return false;
  }
  return {load,clear:()=>{generation++;view.clear();clearActivity();content.replaceChildren();status.textContent="";},
    dispose:()=>{disposed=true;generation++;view.clear();clearActivity();root.replaceChildren();}};
}
