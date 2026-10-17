/** Source-only private finance workspace; no public preview, payment, ZATCA or live deployment. */
import {mountOperationsPage} from "./operations-page.mjs";
import {mountReceivablesPage,} from "./receivables-page.mjs";
import {mountInvoiceApprovalPage} from "./invoice-approval-page.mjs";
import {mountActivityTimeline} from "./activity-timeline.mjs";
import {validReportDate} from "./receivables-client.mjs";

const LABELS=Object.freeze({
  ar:{
    title:"ONE ERP NEXT | مساحة العمل المالية",back:"رجوع للعمليات",clear:"مسح البيانات المعروضة",
    reviewRef:"مرجع الفاتورة الداخلية (UUID)",reviewOpen:"فتح الفاتورة للمراجعة",
    invalid:"مرجع غير صالح، لم يتم فتح مستند",notice:
      "عرض محاسبي داخلي فقط. المبالغ المحصلة داخليًا ليست تأكيدًا بنكيًا. إصدار الفواتير الضريبية غير مفعل.",
    sales:"المبيعات",purchases:"المشتريات",inventory:"المخزون",
    returns:"المرتجعات",receivables:"أعمار الديون",review:"مراجعة الفواتير",
  },
  en:{
    title:"ONE ERP NEXT | Private finance",back:"Back to operations",clear:"Clear displayed data",
    reviewRef:"Internal invoice UUID",reviewOpen:"Load invoice for review",
    invalid:"Invalid reference; no document opened",notice:
      "Internal accounting only. Applied receipts are not bank verified. Fiscal invoice issuance is not enabled.",
    sales:"Sales",purchases:"Purchases",inventory:"Inventory",
    returns:"Returns",receivables:"Receivables aging",review:"Invoice review",
  },
});
const MODES=Object.freeze(["sales","purchases","inventory","returns","receivables","review"]);
const DOC=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TENANT=DOC;

/**
 * Embeddable ONLY in an authenticated, same-origin private application shell.
 * Real backend validates token, tenant membership and SQL roles independently.
 * Browsers never choose a finance SQL role, source amount, login or DSN.
 */
export function mountPrivateFinanceWorkspace({
  root,baseUrl,tenant,as_of,getToken,language="ar",fetchImpl,
}){
  if(!root?.ownerDocument||typeof getToken!=="function"||
     typeof tenant!=="string"||!TENANT.test(tenant)||
     !validReportDate(as_of)){
    throw new TypeError("Trusted private workspace configuration required");
  }
  const doc=root.ownerDocument,lang=language==="en"?"en":"ar",words=LABELS[lang];
  let disposed=false,active=null,child=null,generation=0;
  root.replaceChildren();root.setAttribute("dir",lang==="ar"?"rtl":"ltr");
  root.setAttribute("class","one-private-finance");
  const header=doc.createElement("header"),brand=doc.createElement("h1");
  brand.textContent=words.title;
  const notice=doc.createElement("p");notice.textContent=words.notice;
  header.append(brand,notice);
  const navigation=doc.createElement("nav");
  navigation.setAttribute("class","one-finance-nav");
  navigation.setAttribute("aria-label","ONE ERP private finance sections");
  const buttons=new Map();
  const content=doc.createElement("main");
  content.setAttribute("class","one-finance-content");
  const clear=doc.createElement("button");clear.type="button";clear.textContent=words.clear;
  for(const module of MODES){
    const button=doc.createElement("button");button.type="button";
    button.textContent=words[module];
    button.setAttribute("data-module",module);
    button.addEventListener("click",()=>{void select(module);});
    navigation.append(button);buttons.set(module,button);
  }
  clear.addEventListener("click",()=>reset());
  root.append(header,navigation,clear,content);

  function destroyCurrent(){
    generation++;
    child?.dispose?.();child=null;active=null;
    content.replaceChildren();
    for(const button of buttons.values())button.setAttribute("aria-pressed","false");
  }
  function reset(){if(disposed)return;destroyCurrent();}
  async function select(module){
    if(disposed||!MODES.includes(module))return false;
    destroyCurrent();
    const revision=generation;
    active=module;buttons.get(module).setAttribute("aria-pressed","true");
    const panel=doc.createElement("section");
    panel.setAttribute("aria-label",words[module]);
    content.append(panel);
    if(module==="receivables"){
      child=mountReceivablesPage({root:panel,baseUrl,tenant,as_of,getToken,
        language:lang,fetchImpl});
      return true;
    }
    if(module==="review"){
      const controls=doc.createElement("section");
      const label=doc.createElement("label");label.textContent=words.reviewRef;
      const ref=doc.createElement("input");ref.type="text";ref.autocomplete="off";
      ref.spellcheck=false;ref.maxLength=36;
      const open=doc.createElement("button");open.type="button";
      open.textContent=words.reviewOpen;
      const feedback=doc.createElement("p");feedback.setAttribute("role","status");
      const reviewLayout=doc.createElement("section");
      reviewLayout.setAttribute("class","one-review-with-activity");
      const reviewer=doc.createElement("section");
      const activity=doc.createElement("aside");
      controls.append(label,ref,open,feedback);
      reviewLayout.append(reviewer,activity);
      panel.append(controls,reviewLayout);
      const review=mountInvoiceApprovalPage({
        root:reviewer,baseUrl,getToken,language:lang,fetchImpl,
      });
      const history=mountActivityTimeline({root:activity,tenant,baseUrl,getToken,
        language:lang,fetchImpl,timeZone:"Asia/Riyadh"});
      child={dispose:()=>{review.dispose();history.dispose();}};
      let requestId=0;
      open.addEventListener("click",()=>{
        if(disposed||active!=="review"||revision!==generation)return;
        const value=ref.value;
        const request=++requestId;
        history.clear();
        if(!DOC.test(value)){
          review.clear();feedback.textContent=words.invalid;return;
        }
        feedback.textContent="";
        void review.open({tenant,document_id:value}).then(ok=>{
          if(disposed||active!=="review"||revision!==generation||request!==requestId)return;
          if(ok)void history.load("invoice",value);
        });
      });
      return true;
    }
    child=mountOperationsPage({root:panel,baseUrl,tenant,getToken,language:lang,
      fetchImpl,showNavigation:false});
    const opened=child;
    const loaded=await opened.load(module);
    return Boolean(loaded)&&!disposed&&revision===generation;
  }
  return {
    select,clear:reset,
    dispose:()=>{if(disposed)return;destroyCurrent();disposed=true;root.replaceChildren();},
    snapshot:()=>({active,disposed}),
  };
}
