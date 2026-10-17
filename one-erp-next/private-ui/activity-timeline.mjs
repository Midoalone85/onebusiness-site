/** Private append-only source activity display; does not record client clicks. */
import {readActivity} from "./activity-client.mjs";
const ACTIONS=Object.freeze({
  ar:{
    "invoice.issued":"إصدار الفاتورة","payment.applied":"إثبات تخصيص دفعة داخلي",
    "journal.posted":"ترحيل قيد","return.requested":"طلب مرتجع",
    "return.completed":"إكمال المرتجع","return.rejected":"رفض المرتجع",
    "stock.purchase_receipt":"استلام مخزون","stock.sale_dispatch":"صرف مخزون",
    "stock.customer_return":"استلام مرتجع عميل","stock.vendor_return":"إرجاع للمورد",
    "delivery.created":"إنشاء أمر تسليم","delivery.picked":"تجهيز الأصناف",
    "delivery.packed":"تعبئة الطلب","delivery.dispatched":"إرسال التسليم",
    "delivery.delivered":"تأكيد التسليم",
    "receiving.inspection_started":"بدء فحص الوارد",
    "receiving.inspection_completed":"اكتمال فحص الوارد",
  },
  en:{
    "invoice.issued":"Invoice issued","payment.applied":"Internal payment applied",
    "journal.posted":"Journal posted","return.requested":"Return requested",
    "return.completed":"Return completed","return.rejected":"Return rejected",
    "stock.purchase_receipt":"Stock received","stock.sale_dispatch":"Stock dispatched",
    "delivery.created":"Delivery created","delivery.picked":"Goods picked",
    "delivery.packed":"Delivery packed","delivery.dispatched":"Delivery dispatched",
    "delivery.delivered":"Delivery completed",
    "receiving.inspection_started":"Receiving inspection started",
    "receiving.inspection_completed":"Receiving inspection completed",
  },
});
const LABELS={
  ar:{title:"سجل النشاط",note:"عمليات موثقة من سجل قاعدة البيانات. لا يشمل كل تعديل قبل اكتمال الربط.",
    empty:"لا توجد أحداث مسجلة لهذا المستند",loading:"جارٍ تحميل الأحداث الموثقة…",
    error:"تعذر عرض سجل النشاط المصرح به",user:"المستخدم (معرّف موثّق)",
    next:"الأقدم",prev:"الأحدث"},
  en:{title:"Activity history",note:"Database audit facts only; not every edit is connected yet.",
    empty:"No recorded events for this document",loading:"Loading verified events…",
    error:"Authorized activity history unavailable",user:"Verified actor ID",
    next:"Older",prev:"Newer"},
};
export function mountActivityTimeline({root,tenant,baseUrl,getToken,fetchImpl,
    language="ar",timeZone="UTC"}){
  if(!root?.ownerDocument||typeof getToken!=="function")
    throw new TypeError("Authenticated private activity host required");
  const doc=root.ownerDocument,lang=language==="en"?"en":"ar",words=LABELS[lang];
  const formatter=new Intl.DateTimeFormat(lang==="ar"?"ar-EG":"en-GB",{
    timeZone,year:"numeric",month:"2-digit",day:"2-digit",
    hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23",
  });
  let disposed=false,revision=0,controller=null,active=null,offset=0,hasMore=false;
  root.replaceChildren();root.setAttribute("class","one-activity-panel");
  root.setAttribute("dir",lang==="ar"?"rtl":"ltr");
  const title=doc.createElement("h2");title.textContent=words.title;
  const note=doc.createElement("p");note.textContent=words.note;
  const status=doc.createElement("p");status.setAttribute("role","status");
  const events=doc.createElement("ol");events.setAttribute("aria-label",words.title);
  const nav=doc.createElement("nav");
  const prev=doc.createElement("button");prev.type="button";prev.textContent=words.prev;
  const next=doc.createElement("button");next.type="button";next.textContent=words.next;
  nav.append(prev,next);
  root.append(title,note,status,events,nav);
  function buttons(){prev.disabled=disposed||!active||offset===0;next.disabled=disposed||!active||!hasMore;}
  function clear(){
    revision++;controller?.abort();controller=null;active=null;offset=0;hasMore=false;
    events.replaceChildren();status.textContent="";buttons();
  }
  async function load(record_type,record_id,nextOffset=0){
    if(disposed)return false;
    const current=++revision;
    controller?.abort();controller=new AbortController();
    active={record_type,record_id};hasMore=false;
    status.textContent=words.loading;events.replaceChildren();buttons();
    try{
      const token=await getToken();
      if(disposed||current!==revision)return false;
      const data=await readActivity({baseUrl,tenant,record_type,record_id,token,
        limit:20,offset:nextOffset,fetchImpl,signal:controller.signal});
      if(disposed||current!==revision)return false;
      offset=nextOffset;hasMore=data.page.has_more;
      status.textContent=data.items.length?"":words.empty;
      for(const item of data.items){
        const li=doc.createElement("li");
        const action=doc.createElement("strong");
        action.textContent=ACTIONS[lang][item.action]||item.action;
        const at=doc.createElement("time");at.dateTime=item.occurred_at;
        at.textContent=formatter.format(new Date(item.occurred_at));
        const actor=doc.createElement("span");
        actor.textContent=words.user+": "+item.actor_id;
        li.append(action,at,actor);events.append(li);
      }
      buttons();return true;
    }catch{
      if(!disposed&&current===revision){
        active=null;events.replaceChildren();hasMore=false;
        status.textContent=words.error;buttons();
      }
      return false;
    }
  }
  prev.addEventListener("click",()=>{
    if(active&&offset>0)void load(active.record_type,active.record_id,Math.max(0,offset-20));
  });
  next.addEventListener("click",()=>{
    if(active&&hasMore&&offset+20<=5000)void load(active.record_type,active.record_id,offset+20);
  });
  buttons();
  return {load,clear,dispose:()=>{if(disposed)return;clear();disposed=true;root.replaceChildren();},
    snapshot:()=>({active,offset,hasMore,disposed})};
}
