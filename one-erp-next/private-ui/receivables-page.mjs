/** Source-only bilingual A/R aging view, NEVER public sample data. */
import {readReceivablesPage,currentPageBalances,validReportDate} from "./receivables-client.mjs";
const LABELS={
 ar:{title:"أعمار ديون العملاء",warning:"تحصيل داخلي مسجل، وليس إثبات استلام بنكي.",
    date:"التقرير حتى تاريخ",apply:"عرض التقرير",back:"السابق",next:"التالي",
    clear:"إغلاق التقرير",empty:"لا توجد فواتير في هذه الصفحة",
    loading:"جارٍ تحميل الأرصدة من النظام…",denied:"غير مصرح لك بعرض حسابات العملاء",
    unavailable:"تعذّر تحميل التقرير الموثوق",page:"الصفحة",part:"أرصدة الصفحة الحالية فقط",
    headers:["رقم الفاتورة","العميل","العملة","تاريخ الاستحقاق","إجمالي الفاتورة",
      "تحصيل داخلي","إشعارات دائن","المتبقي","أيام التأخير","شريحة العمر"],
    buckets:{current:"غير متأخر","1_30":"1–30 يوم","31_60":"31–60 يوم",
      "61_90":"61–90 يوم",over_90:"أكثر من 90",internally_cleared:"مصَفّى داخليًا"}},
 en:{title:"Accounts receivable aging",warning:"Internally applied receipts are NOT verified bank deposits.",
    date:"Report as of",apply:"Load report",back:"Previous",next:"Next",
    clear:"Clear report",empty:"No invoices on this page",
    loading:"Loading authenticated receivables…",denied:"You cannot access customer balances",
    unavailable:"Verified report unavailable",page:"Page",part:"Current-page balances only",
    headers:["Invoice","Customer","Currency","Due","Invoice gross",
      "Applied internally","Issued credits","Outstanding","Days overdue","Aging"],
    buckets:{current:"Current","1_30":"1–30 days","31_60":"31–60 days",
      "61_90":"61–90 days",over_90:"Over 90",internally_cleared:"Internally cleared"}}
};
export function mountReceivablesPage({root,baseUrl,tenant,getToken,fetchImpl,as_of,language="ar"}){
  if(!root?.ownerDocument||typeof getToken!=="function"||!validReportDate(as_of))
    throw new TypeError("Private report root, authorized identity and explicit date required");
  const doc=root.ownerDocument,words=LABELS[language==="en"?"en":"ar"];
  let revision=0,controller=null,disposed=false,loading=false;
  let offset=0,hasMore=false,hasLoaded=false;
  root.replaceChildren();root.setAttribute("dir",language==="en"?"ltr":"rtl");
  const title=doc.createElement("h2");title.textContent=words.title;
  const warning=doc.createElement("p");warning.textContent=words.warning;
  const dateLabel=doc.createElement("label");dateLabel.textContent=words.date;
  const dateInput=doc.createElement("input");dateInput.type="date";dateInput.value=as_of;
  const apply=doc.createElement("button");apply.type="button";apply.textContent=words.apply;
  const clear=doc.createElement("button");clear.type="button";clear.textContent=words.clear;
  const status=doc.createElement("p");status.setAttribute("role","status");
  const summary=doc.createElement("section");summary.setAttribute("aria-label",words.part);
  const table=doc.createElement("section");
  const nav=doc.createElement("nav");
  const prev=doc.createElement("button");prev.type="button";prev.textContent=words.back;
  const next=doc.createElement("button");next.type="button";next.textContent=words.next;
  const page=doc.createElement("span");
  nav.append(prev,page,next);
  root.append(title,warning,dateLabel,dateInput,apply,clear,status,summary,table,nav);

  const columns=["number","partner_reference","currency","due_on","gross",
    "applied_receipts","issued_credits","outstanding","days_overdue","aging_bucket"];
  function buttons(){
    apply.disabled=disposed||loading;
    dateInput.disabled=disposed||loading;
    prev.disabled=disposed||loading||!hasLoaded||offset===0;
    next.disabled=disposed||loading||!hasLoaded||!hasMore;
    page.textContent=hasLoaded?words.page+" "+(Math.floor(offset/25)+1):"";
  }
  function wipe(){
    revision++;controller?.abort();controller=null;
    hasLoaded=false;hasMore=false;loading=false;offset=0;
    summary.replaceChildren();table.replaceChildren();status.textContent="";
    buttons();
  }
  function render(result){
    summary.replaceChildren();table.replaceChildren();
    for(const total of currentPageBalances(result.items)){
      const p=doc.createElement("p");
      p.textContent=words.part+": "+total.currency+" "+total.outstanding;
      summary.append(p);
    }
    if(!result.items.length){status.textContent=words.empty;return;}
    status.textContent="";
    const grid=doc.createElement("table"),head=doc.createElement("thead"),
      tr=doc.createElement("tr");
    for(const label of words.headers){const th=doc.createElement("th");th.textContent=label;tr.append(th);}
    head.append(tr);grid.append(head);
    const body=doc.createElement("tbody");
    for(const row of result.items){
      const tr=doc.createElement("tr");
      for(const name of columns){
        const td=doc.createElement("td");
        td.textContent=name==="aging_bucket"?words.buckets[row[name]]:String(row[name]);
        tr.append(td);
      }
      body.append(tr);
    }
    grid.append(body);table.append(grid);
  }
  async function load(nextOffset=0){
    if(disposed)return false;
    const date=dateInput.value;
    if(!validReportDate(date)){wipe();status.textContent=words.unavailable;return false;}
    const current=++revision;
    controller?.abort();controller=new AbortController();
    loading=true;hasLoaded=false;hasMore=false;summary.replaceChildren();
    table.replaceChildren();status.textContent=words.loading;buttons();
    try{
      const token=await getToken();
      if(disposed||current!==revision)return false;
      const result=await readReceivablesPage({
        baseUrl,tenant,as_of:date,token,limit:25,offset:nextOffset,
        signal:controller.signal,fetchImpl,
      });
      if(disposed||current!==revision)return false;
      offset=nextOffset;hasMore=result.page.has_more;hasLoaded=true;
      render(result);return true;
    }catch(error){
      if(!disposed&&current===revision){
        table.replaceChildren();summary.replaceChildren();
        status.textContent=error?.status===401||error?.status===403?
          words.denied:words.unavailable;
      }
      return false;
    }finally{
      if(!disposed&&current===revision){loading=false;buttons();}
    }
  }
  apply.addEventListener("click",()=>load(0));
  clear.addEventListener("click",wipe);
  prev.addEventListener("click",()=>{if(hasLoaded&&!loading&&offset>0)load(Math.max(0,offset-25));});
  next.addEventListener("click",()=>{if(hasLoaded&&!loading&&hasMore&&offset+25<=5000)load(offset+25);});
  buttons();
  return {load,clear:wipe,dispose:()=>{wipe();disposed=true;root.replaceChildren();},
    snapshot:()=>({hasLoaded,offset,hasMore,loading})};
}
