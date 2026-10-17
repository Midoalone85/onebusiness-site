import { createOperationsView } from "./operations-view.mjs";

const MODULES = Object.freeze(["sales","purchases","inventory","returns"]);
const LABELS = Object.freeze({
  ar:{sales:"المبيعات",purchases:"المشتريات",inventory:"المخزون",returns:"المرتجعات",loading:"جارٍ تحميل البيانات…",unauthorized:"لا تملك صلاحية الاطلاع على هذه البيانات",unavailable:"تعذّر تحميل البيانات",empty:"لا توجد سجلات",signout:"إغلاق الجلسة"},
  en:{sales:"Sales",purchases:"Purchases",inventory:"Inventory",returns:"Returns",loading:"Loading records…",unauthorized:"Access denied",unavailable:"Records unavailable",empty:"No records",signout:"Clear session"}
});
function safeText(v) {
  if (v == null) return "";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : "";
}
/** Caller supplies a trusted DOM element and a short-lived, memory-only token provider.
 * Never embed credentials in HTML or mount this into the public fictional-data preview.
 */
export function mountOperationsPage({root,baseUrl,tenant,getToken,language="ar",fetchImpl,showNavigation=true}) {
  if (!root || !root.ownerDocument || typeof getToken !== "function") throw new TypeError("Private root and token provider required");
  const document = root.ownerDocument;
  const words = LABELS[language === "en" ? "en" : "ar"];
  const view = createOperationsView({baseUrl,tenant,getToken,fetchImpl});
  let generation=0, disposed=false;
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
  clear.addEventListener("click",()=>{generation++;view.clear();content.replaceChildren();status.textContent="";});
  for(const module of MODULES){
    const button=document.createElement("button");
    button.type="button";button.textContent=words[module];
    button.addEventListener("click",()=>load(module));
    menu.append(button);
  }
  root.append(menu,clear,status,content);
  function render(data){
    content.replaceChildren();
    const rows=Array.isArray(data?.items)?data.items:[];
    if(!rows.length){status.textContent=words.empty;return;}
    status.textContent="";
    const table=document.createElement("table");
    const columns=Object.keys(rows[0]??{}).filter(k=>!k.startsWith("_")).slice(0,8);
    const head=document.createElement("thead"),header=document.createElement("tr");
    for(const key of columns){const cell=document.createElement("th");cell.textContent=key;header.append(cell);}
    head.append(header);table.append(head);
    const body=document.createElement("tbody");
    for(const row of rows.slice(0,100)){
      const tr=document.createElement("tr");
      for(const key of columns){const cell=document.createElement("td");cell.textContent=safeText(row?.[key]);tr.append(cell);}
      body.append(tr);
    }
    table.append(body);content.append(table);
  }
  async function load(module){
    if(disposed)return;
    const current=++generation;
    content.replaceChildren();
    status.textContent=words.loading;
    const result=await view.load(module);
    if(disposed||current!==generation)return;
    if(result.status==="ready")render(result.data);
    else{content.replaceChildren();status.textContent=result.error==="unauthorized"?words.unauthorized:words.unavailable;}
  }
  return {load,clear:()=>{generation++;view.clear();content.replaceChildren();status.textContent="";},
    dispose:()=>{disposed=true;generation++;view.clear();root.replaceChildren();}};
}
